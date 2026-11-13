"""Localization regressions; Linux also exercises real curses in a PTY."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

# The application targets Linux. On Windows only the rendering-independent
# checks use a minimal stand-in; the real curses integration runs on Linux.
try:
    import curses
except ModuleNotFoundError:
    if sys.platform != "win32":
        raise
    curses = types.ModuleType("curses")
    curses.error = RuntimeError
    for i, name in enumerate(("A_BOLD", "A_DIM", "A_REVERSE", "KEY_ENTER",
                              "KEY_BACKSPACE", "KEY_LEFT", "KEY_RIGHT",
                              "KEY_UP", "KEY_DOWN"), 1):
        setattr(curses, name, 1 << (i + 8))
    curses.color_pair = lambda value: value
    sys.modules["curses"] = curses

import datameter as dm

ROOT = Path(__file__).resolve().parents[1]


class CellTests(unittest.TestCase):
    def test_cell_width_and_clipping(self):
        self.assertEqual(dm.display_width("中文 ABC"), 8)
        self.assertEqual(dm.display_width("e\u0301"), 1)
        for width, expected in ((0, ""), (1, ""), (2, "中"),
                                (3, "中"), (4, "中文"), (5, "中文A")):
            with self.subTest(width=width):
                self.assertEqual(dm.clip_text("中文ABC", width), expected)
        self.assertEqual(dm.clip_text("e\u0301中", 1), "e\u0301")
        self.assertEqual(dm.pad_text("网卡", 8), "网卡    ")
        self.assertEqual(dm.pad_text("周一", 5, center=True), "周一 ")

    def test_put_never_passes_a_partial_wide_character(self):
        screen = types.SimpleNamespace(getmaxyx=lambda: (4, 7))
        calls = []
        screen.addstr = lambda *args: calls.append(args)
        app = dm.App(screen, dict(dm.DEFAULT_CONFIG))
        app.put(0, 2, "中文AB")
        self.assertEqual(calls[0][2], "中文A")
        app.put(0, 6, "中")
        self.assertEqual(calls[-1][2], "")
        app.put(0, -1, "中")
        app.put(4, 0, "中")
        self.assertEqual(len(calls), 2)

    @unittest.skipUnless(sys.platform == "linux", "PTY replay requires Linux")
    def test_preview_fallback_preserves_chinese_cells(self):
        import preview
        with patch.dict(sys.modules, {"pyte": None}):
            result = preview.replay("中文\x1b[1;7HX", 10, 2)
            self.assertEqual(result.splitlines()[0], "中文  X")
            result = preview.replay("中文\x1b[1;2HX", 10, 2)
            self.assertEqual(result.splitlines()[0], " X文")


class LocalizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = dict(dm.DEFAULT_CONFIG, interface="eth0", sound_enabled=False,
                        notifications=False, cycle_limit_gb=300, daily_limit_gb=10)
        for name, filename in (("CONFIG_PATH", "config.json"), ("STATE_PATH", "state.json")):
            patcher = patch.object(dm, name, Path(self.tmp.name) / filename)
            patcher.start()
            self.addCleanup(patcher.stop)

    def stats(self):
        from datetime import date, datetime, timedelta
        today = date.today()
        days = []
        for offset in range(6, -1, -1):
            day = today - timedelta(days=offset)
            days.append({"date": {"year": day.year, "month": day.month, "day": day.day},
                         "rx": 9 * 1024 ** 3, "tx": 0})
        hour = dict(days[-1], time={"hour": datetime.now().hour})
        with patch.object(dm, "vnstat_snapshot", return_value={"day": days, "hour": [hour]}):
            return dm.compute_stats("eth0", self.cfg)

    def test_weekdays_are_chinese_independent_of_locale(self):
        stats = self.stats()
        self.assertEqual(len(stats["day_labels"]), 7)
        self.assertEqual(set(stats["day_labels"]),
                         {"周一", "周二", "周三", "周四", "周五", "周六", "周日"})

    def test_alert_messages_and_deduplication_preserve_state_keys(self):
        stats = self.stats()
        stats["cycle_used"] = 250 * 1024 ** 3
        with patch.object(dm, "fire_alert") as fire:
            messages = dm.check_alerts(stats, self.cfg)
            self.assertEqual(len(messages), 2)
            self.assertIn("本周期已用 83%", messages[0])
            self.assertIn("今日已用 90%", messages[1])
            self.assertIn("周期用量达到 80%", fire.call_args_list[0][0][0])
            self.assertIn("今日用量达到 80%", fire.call_args_list[1][0][0])
            self.assertEqual(dm.check_alerts(stats, self.cfg), [])
            state = json.loads(dm.STATE_PATH.read_text())
            self.assertEqual(state["cycle"], ["cycle:80"])
            self.assertEqual(state["day"], ["day:80"])

    def test_chinese_help_and_argument_errors(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exc:
            dm.main(["--help"])
        self.assertEqual(exc.exception.code, 0)
        self.assertIn("用法：", output.getvalue())
        self.assertIn("选项:", output.getvalue())
        self.assertIn("指定要监控的网卡", output.getvalue())
        for args, expected in ((["--invalid"], "无法识别的参数"),
                               (["--interface"], "需要一个参数")):
            output = io.StringIO()
            with contextlib.redirect_stderr(output), self.assertRaises(SystemExit) as exc:
                dm.main(args)
            self.assertEqual(exc.exception.code, 2)
            self.assertIn(expected, output.getvalue())

    def test_summary_and_missing_vnstat(self):
        output = io.StringIO()
        with patch.object(dm, "compute_stats", return_value=self.stats()), \
                patch.object(dm, "daemon_status", return_value=("active", "enabled")), \
                contextlib.redirect_stdout(output):
            self.assertEqual(dm.print_summary(self.cfg), 0)
        text = output.getvalue()
        for label in ("网卡 eth0", "运行中/开机启动", "周期已用", "近 24 小时", "每在线小时"):
            self.assertIn(label, text)
        dm.save_config(self.cfg)
        with patch.object(dm.shutil, "which", return_value=None), \
                contextlib.redirect_stderr(output):
            self.assertEqual(dm.main(["--once"]), 1)
        self.assertIn("未安装 vnstat", output.getvalue())

    def test_wizard_round_trip_and_invalid_input(self):
        app = dm.App(None, self.cfg)
        with patch.object(dm, "list_interfaces", return_value=["eth0", "wlan0"]):
            app.start_wizard()
        self.assertEqual(app.wiz_steps[1]["title"], "网卡")
        app.handle_wizard_key(10)
        app.handle_wizard_key(curses.KEY_RIGHT)
        app.handle_wizard_key(10)
        self.assertEqual(app.wiz_step, 2)
        app.wiz_buf = "bad"
        app.handle_wizard_key(10)
        self.assertEqual(app.message, "输入值无效")
        app.wiz_buf = "18"
        app.handle_wizard_key(10)
        while app.wiz_step < len(app.wiz_steps) - 1:
            app.handle_wizard_key(10)
        with patch.object(app, "refresh"):
            app.handle_wizard_key(10)
        saved = dm.load_config()
        self.assertEqual(saved["interface"], "wlan0")
        self.assertEqual(saved["cycle_start_day"], 18)
        self.assertTrue(saved["setup_done"])
        self.assertEqual(app.message, "设置已保存")
        app.start_wizard()
        app.handle_wizard_key(27)
        self.assertIn("已跳过设置", app.message)

    def test_daemon_control_keeps_machine_readable_statuses(self):
        with patch.object(dm, "run", return_value=subprocess.CompletedProcess([], 0, "active\n")):
            self.assertEqual(dm.daemon_status(), ("active", "active"))
            self.assertEqual(dm.daemon_toggle("stop"), (True, "vnstat 已停止"))
        with patch.object(dm, "run", return_value=subprocess.CompletedProcess([], 1, "", "polkit")):
            self.assertIn("权限不足", dm.daemon_toggle("start")[1])

    def test_rendered_labels_do_not_overlap_values_or_projection(self):
        app = dm.App(None, self.cfg)
        app.stats = self.stats()
        calls = []
        app.put = lambda y, x, text, attr=0: calls.append((y, x, text))
        with patch.object(curses, "color_pair", return_value=0), \
                patch.object(dm, "daemon_status", return_value=("active", "enabled")):
            app.draw_dashboard(40, 110)
            readout = next(c for c in calls if "/ 300 GB" in c[2])
            projected = next(c for c in calls if c[2].startswith("预计 "))
            pace = next(c for c in calls if "预算" in c[2] and c[0] == projected[0])
            self.assertLessEqual(readout[1] + dm.display_width(readout[2]), projected[1])
            self.assertLessEqual(projected[1] + dm.display_width(projected[2]), pace[1])
            calls.clear()
            app.draw_settings(40, 110)
            for y, x, text in calls:
                if x == 5:
                    self.assertLessEqual(x + dm.display_width(text), 28)
            calls.clear()
            app.start_wizard = lambda: None
            app.wiz_values = self.cfg
            app.wiz_steps = [{"kind": "summary"}]
            app.draw_wizard(40, 110)
            for y, x, text in calls:
                if x == 4:
                    self.assertLessEqual(x + dm.display_width(text), 24)


@unittest.skipUnless(sys.platform == "linux", "Real curses requires Linux")
class TerminalIntegrationTests(unittest.TestCase):
    def test_real_curses_screens_and_cli(self):
        import fcntl
        import pty
        import select
        import struct
        import termios
        import preview

        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            # A private PATH prevents tests from touching real network/services.
            helper = folder / "fake_command.py"
            helper.write_text(
                "import json, os, sys\n"
                "from datetime import datetime, timedelta\n"
                "if os.path.basename(sys.argv[0]) == 'systemctl':\n"
                "    print('enabled' if '--is-enabled' in sys.argv or 'is-enabled' in sys.argv else 'active')\n"
                "else:\n"
                "    now = datetime.now()\n"
                "    days = []\n"
                "    for n in range(6, -1, -1):\n"
                "        d = now - timedelta(days=n)\n"
                "        days.append({'date': {'year': d.year, 'month': d.month, 'day': d.day}, 'rx': 1024**3, 'tx': 0})\n"
                "    hour = dict(days[-1], time={'hour': now.hour})\n"
                "    print(json.dumps({'interfaces': [{'name': 'eth0', 'traffic': {'day': days, 'hour': [hour]}}]}))\n",
                encoding="utf-8")
            for name in ("vnstat", "systemctl"):
                executable = folder / name
                executable.write_text("#!" + sys.executable + "\n" + helper.read_text(), encoding="utf-8")
                executable.chmod(0o755)
            env = dict(os.environ, PATH=str(folder), TERM="xterm-256color", LC_ALL="C.UTF-8",
                       DATAMETER_CONFIG=str(folder / "config.json"), DATAMETER_STATE=str(folder / "state.json"))
            config = dict(dm.DEFAULT_CONFIG, interface="eth0", setup_done=True,
                          notifications=False, sound_enabled=False)
            (folder / "config.json").write_text(json.dumps(config))
            for option, label in (("--help", "用法："), ("--once", "周期已用"), ("--check", None)):
                result = subprocess.run([sys.executable, str(ROOT / "datameter.py"), option],
                                        env=env, capture_output=True, encoding="utf-8", timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                if label:
                    self.assertIn(label, result.stdout)

            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 110, 0, 0))
            process = subprocess.Popen([sys.executable, str(ROOT / "datameter.py")],
                                       stdin=slave, stdout=slave, stderr=slave, env=env)
            os.close(slave)
            raw = bytearray()

            def read_until(label):
                deadline = time.monotonic() + 8
                found = False
                while time.monotonic() < deadline:
                    ready, _, _ = select.select([master], [], [], 0.05 if found else 0.1)
                    if ready:
                        try:
                            raw.extend(os.read(master, 65536))
                        except OSError:
                            break
                        found = label.encode("utf-8") in raw
                    elif found:
                        # Drain the complete redraw, not just its first label.
                        return
                    if process.poll() is not None:
                        break
                self.fail("Missing terminal label {!r}: {}".format(label, raw.decode("utf-8", errors="replace")))

            try:
                read_until("平均用量")
                screen = preview.replay(raw.decode("utf-8"), 110, 40)
                for label in ("用量提醒", "近 7 天", "近 24 小时", "[q] 退出"):
                    self.assertIn(label, screen)
                for line in screen.splitlines():
                    self.assertLessEqual(dm.display_width(line), 110)
                print("\nLinux 终端预览：\n" + screen)
                for key, label in ((b"s", "声音与通知"), (b"\x1b", "平均用量"),
                                   (b"?", "快捷键"), (b"x", "平均用量"),
                                   (b"w", "欢迎使用 datameter"), (b"\n", "←/→ 切换"),
                                   (b"\n", "套餐重置日"), (b"\n", "套餐流量额度"),
                                   (b"\n", "每日预算"), (b"\n", "提醒阈值"),
                                   (b"\n", "提示音"), (b"\n", "桌面通知"),
                                   (b"\n", "确认并保存"), (b"\n", "设置已保存"),
                                   (b"w", "欢迎使用 datameter"), (b"\x1b", "已跳过设置")):
                    raw.clear()
                    os.write(master, key)
                    read_until(label)
                os.write(master, b"q")
                process.wait(timeout=5)
                self.assertEqual(process.returncode, 0)
                self.assertNotIn(b"Traceback", raw)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(master)


if __name__ == "__main__":
    unittest.main()
