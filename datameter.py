#!/usr/bin/env python3
"""
datameter — a friendly vnstat TUI dashboard.

Zero third-party dependencies (stdlib curses only).

  * plan gauge for the billing cycle (configurable start day, e.g. the 18th)
  * today's usage gauge vs a daily limit
  * averages: per "on" hour, per day (7 d), per week, projected cycle
  * vertical bar charts for the last 24 hours and the last 7 days
  * user-settable limits and alert thresholds (settings screen)
  * non-obnoxious "tock" sound (car turn-signal style) + desktop notification
  * start/stop the vnstat daemon from inside the UI

Usage
  datameter              # launch the TUI   (wrapper in ~/.local/bin)
  datameter --once       # print a text summary and exit (cron/waybar friendly)
  datameter --help
"""

from __future__ import annotations

import argparse
import curses
import json
import locale
import math
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import unicodedata
import wave
from datetime import date, datetime, timedelta
from pathlib import Path

from history_view import HistoryView
from terminal_charts import bar_cells
from traffic_history import HistoryStore, LiveRates, format_rate, parse_anchor

APP = "datameter"
HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("DATAMETER_CONFIG") or (HERE / "config.json"))
STATE_PATH = Path(os.environ.get("DATAMETER_STATE") or (HERE / ".alert-state.json"))
SOUND_PATH = HERE / "tock.wav"
HISTORY_PATH = Path(os.environ.get("DATAMETER_HISTORY") or (HERE / "history.sqlite3"))

DEFAULT_CONFIG = {
    "interface": "wlp4s0",
    "cycle_start_day": 1,
    "cycle_limit_gb": 1000.0,
    "daily_limit_gb": 10.0,
    "alert_thresholds_pct": [80, 100],
    "sound_enabled": True,
    "sound_volume": 0.35,
    "notifications": True,
    "refresh_seconds": 10,
    "setup_done": False,
}

# --------------------------------------------------------------------------- #
# config / state
# --------------------------------------------------------------------------- #


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]


def fmt_bytes(n: float, decimals: int = 1) -> str:
    n = float(n)
    for u in UNITS:
        if abs(n) < 1024 or u == UNITS[-1]:
            return f"{n:.{decimals}f} {u}"
        n /= 1024
    return f"{n:.{decimals}f} PiB"


def gb(n: float) -> float:
    return n / (1024 ** 3)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def char_width(char: str) -> int:
    """Terminal cells occupied by a character (Chinese uses two cells)."""
    if unicodedata.combining(char) or unicodedata.category(char) == "Cf":
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text: str) -> int:
    return sum(char_width(char) for char in text)


def clip_text(text: str, width: int) -> str:
    """Clip to terminal cells without cutting a wide character in half."""
    used = 0
    result = []
    for char in text:
        cells = char_width(char)
        if used + cells > max(0, width):
            break
        result.append(char)
        used += cells
    return "".join(result)


def pad_text(text: str, width: int, center: bool = False) -> str:
    padding = max(0, width - display_width(text))
    left = padding // 2 if center else 0
    return " " * left + text + " " * (padding - left)


def daemon_label(status: str) -> str:
    return {
        "active": "运行中", "inactive": "已停止", "failed": "失败",
        "activating": "启动中", "deactivating": "停止中",
        "enabled": "开机启动", "enabled-runtime": "临时启用",
        "disabled": "未启用", "static": "静态", "masked": "已屏蔽",
        "unknown": "未知",
    }.get(status, status)


def run(cmd: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# --- segmented bar (chunky blocks, clearly separated) ---------------------- #

EIGHTHS = "░▏▎▍▌▋▊▉█"          # index 0..8, partial blocks for smooth tips
BAR_CELL_W = 2                   # each segment is 2 chars wide


def _seg_cell(f: float, width: int = BAR_CELL_W) -> str:
    """One 2-char segment, partially filled at the tip."""
    f = clamp(f, 0.0, 1.0)
    total = f * width
    full = int(total)
    rem = total - full
    out = "█" * full
    if full < width:
        out += EIGHTHS[int(round(rem * 8))]
    return out.ljust(width, "░")[:width]


def seg_bar(pct: float, cells: int) -> str:
    """A bar made of `cells` separated segments, e.g.  ██ ██ ░░ ░░"""
    if cells <= 0:
        return ""
    parts = []
    for i in range(cells):
        lo, hi = 100 * i / cells, 100 * (i + 1) / cells
        parts.append(_seg_cell((pct - lo) / (hi - lo)))
    return " ".join(parts)


def bar_cells_for_width(width: int) -> int:
    # each cell occupies BAR_CELL_W chars + 1 space
    return max(6, (width - 4) // (BAR_CELL_W + 1))


# --------------------------------------------------------------------------- #
# vnstat access
# --------------------------------------------------------------------------- #


def list_interfaces() -> list[str]:
    try:
        j = json.loads(run(["vnstat", "--json"]).stdout)
        return [i["name"] for i in j.get("interfaces", [])]
    except Exception:
        return []


def default_interface(fallback: str = "wlp4s0") -> str:
    """Interface with the most recorded traffic (avoids idle NICs)."""
    try:
        j = json.loads(run(["vnstat", "--json"]).stdout)
        best, best_bytes = None, -1
        for i in j.get("interfaces", []):
            total = i.get("traffic", {}).get("total", {})
            n = int(total.get("rx", 0)) + int(total.get("tx", 0))
            if n > best_bytes:
                best, best_bytes = i["name"], n
        if best:
            return best
    except Exception:
        pass
    found = list_interfaces()
    return found[0] if found else fallback


def vnstat_snapshot(iface: str) -> dict:
    # Explicitly request all retained entries rather than vnstat's list limits.
    cp = run(["vnstat", "--json", "a", "0", "-i", iface])
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or "vnstat 执行失败")
    j = json.loads(cp.stdout)
    for i in j.get("interfaces", []):
        if i["name"] == iface:
            traffic = dict(i["traffic"], _updated=i.get("updated", {}), _created=i.get("created", {}))
            try:
                HistoryStore(HISTORY_PATH).ingest(iface, traffic)
            except (OSError, sqlite3.Error) as exc:
                traffic["_history_error"] = f"历史归档写入失败：{exc}"
            return traffic
    raise RuntimeError(f"vnstat 数据库中没有网卡 {iface!r}")


def _d(entry: dict) -> date:
    dd = entry["date"]
    return date(dd["year"], dd["month"], dd["day"])


def _bytes(entry: dict) -> int:
    return int(entry.get("rx", 0)) + int(entry.get("tx", 0))


def cycle_window(cfg: dict, today: date | None = None) -> tuple[date, date, int, int]:
    """Return (start, end, total_days, elapsed_days) for the billing cycle."""
    today = today or date.today()
    day = clamp(int(cfg["cycle_start_day"]), 1, 28)

    def make(y, m, d):
        return date(y, m, min(d, 28))

    start = make(today.year, today.month, day)
    if start > today:
        py, pm = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        start = make(py, pm, day)
    ey, em = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    end = make(ey, em, day)
    total_days = (end - start).days
    elapsed = (today - start).days + 1
    return start, end, total_days, elapsed


def compute_stats(iface: str, cfg: dict) -> dict:
    t = vnstat_snapshot(iface)
    today = date.today()
    now = time.time()

    hours = t.get("hour", [])
    hour_vals = [_bytes(e) for e in hours]
    on_hours = len(hours)
    avg_hour = (sum(hour_vals) / on_hours) if on_hours else 0.0

    days = t.get("day", [])
    day_vals = [_bytes(e) for e in days]
    avg_day = (sum(day_vals) / len(day_vals)) if day_vals else 0.0
    avg_week = avg_day * 7

    today_total = 0
    for e in days:
        if _d(e) == today:
            today_total = _bytes(e)
            break

    five = t.get("fiveminute", [])
    last24 = sum(_bytes(e) for e in five if now - int(e.get("timestamp", 0)) <= 86400)

    start, end, total_days, elapsed = cycle_window(cfg)
    cycle_used = sum(_bytes(e) for e in days if _d(e) >= start)
    projected_cycle = avg_day * total_days

    # series for the charts
    # hourly: last 24 wall-clock hours (0 for hours the PC was off)
    hours_map = {(_d(e), int(e["time"]["hour"])): _bytes(e) for e in hours}
    now_dt = datetime.now().replace(minute=0, second=0, microsecond=0)
    hour_values, hour_labels = [], []
    for i in range(23, -1, -1):
        hh = now_dt - timedelta(hours=i)
        hour_values.append(hours_map.get((hh.date(), hh.hour), 0))
        hour_labels.append(f"{hh.hour:02d}")

    dseries = days[-7:]
    day_values = [_bytes(e) for e in dseries]
    weekdays = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    day_labels = [weekdays[_d(e).weekday()] for e in dseries]

    return {
        "today_total": today_total,
        "last24": last24,
        "avg_hour": avg_hour,
        "avg_day": avg_day,
        "avg_week": avg_week,
        "on_hours": on_hours,
        "projected_cycle": projected_cycle,
        "cycle_used": cycle_used,
        "cycle_start": start,
        "cycle_end": end,
        "cycle_days": total_days,
        "cycle_elapsed": elapsed,
        "hour_values": hour_values,
        "hour_labels": hour_labels,
        "day_values": day_values,
        "day_labels": day_labels,
        "total": int(t.get("total", {}).get("rx", 0)) + int(t.get("total", {}).get("tx", 0)),
        "traffic": t,
        "interface": iface,
    }


# --------------------------------------------------------------------------- #
# sound + notifications
# --------------------------------------------------------------------------- #


def generate_tock(path: Path = SOUND_PATH) -> None:
    """Synthesise a short, quiet 'tock' (modern car turn-signal style)."""
    sr = 44100
    n = int(sr * 0.055)
    frames = bytearray()
    for k in range(n):
        ts = k / sr
        tone = (math.sin(2 * math.pi * 1050 * ts) * 0.65
                + math.sin(2 * math.pi * 520 * ts) * 0.35)
        env = math.exp(-ts * 85)
        click = math.sin(2 * math.pi * 2600 * ts) * math.exp(-ts * 400) * 0.35
        attack = min(1.0, ts / 0.0012)
        s = clamp((tone * env + click) * attack * 0.6, -1.0, 1.0)
        frames += struct.pack("<h", int(s * 32767))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))


def _player_cmd(volume: float) -> list[str] | None:
    volume = clamp(volume, 0.0, 1.0)
    if shutil.which("pw-play"):
        return ["pw-play", f"--volume={volume:.2f}", str(SOUND_PATH)]
    if shutil.which("paplay"):
        return ["paplay", f"--volume={int(volume * 65536)}", str(SOUND_PATH)]
    if shutil.which("ffplay"):
        return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
                "-volume", str(int(volume * 100)), str(SOUND_PATH)]
    if shutil.which("aplay"):
        return ["aplay", "-q", str(SOUND_PATH)]
    return None


def play_tock(times: int = 3, volume: float = 0.35) -> None:
    if not SOUND_PATH.exists():
        try:
            generate_tock()
        except OSError:
            return
    cmd = _player_cmd(volume)
    if cmd is None:
        return

    def _worker():
        for i in range(times):
            try:
                subprocess.run(cmd, capture_output=True, timeout=5)
            except Exception:
                return
            if i < times - 1:
                time.sleep(0.26)

    threading.Thread(target=_worker, daemon=True).start()


def notify(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        try:
            run(["notify-send", "-a", APP, "-u", "normal", title, body], timeout=5)
        except Exception:
            pass


def fire_alert(title: str, body: str, cfg: dict) -> None:
    if cfg.get("sound_enabled", True):
        play_tock(3, float(cfg.get("sound_volume", 0.35)))
    if cfg.get("notifications", True):
        notify(title, body)


def check_alerts(stats: dict, cfg: dict) -> list[str]:
    """Fire any newly crossed thresholds. Returns a list of messages."""
    state = load_state()
    start = stats["cycle_start"]
    today = date.today()
    messages: list[str] = []

    if state.get("cycle_key") != start.isoformat():
        state = {"cycle_key": start.isoformat(), "cycle": [],
                 "day_key": today.isoformat(), "day": []}
    if state.get("day_key") != today.isoformat():
        state["day_key"] = today.isoformat()
        state["day"] = []

    thresholds = sorted(float(x) for x in cfg.get("alert_thresholds_pct", [80, 100]))

    cycle_limit = float(cfg["cycle_limit_gb"]) * 1024 ** 3
    cycle_pct = (stats["cycle_used"] / cycle_limit * 100) if cycle_limit else 0
    new = [th for th in thresholds if cycle_limit and cycle_pct >= th
           and f"cycle:{th:g}" not in state["cycle"]]
    if new:
        state["cycle"].extend(f"cycle:{th:g}" for th in new)
        msg = (f"本周期已用 {cycle_pct:.0f}%（≥ {max(new):g}%）— "
               f"{gb(stats['cycle_used']):.1f} / {cfg['cycle_limit_gb']:.0f} GB")
        fire_alert(f"{APP}：周期用量达到 {max(new):g}%", msg, cfg)
        messages.append(msg)

    daily_limit = float(cfg["daily_limit_gb"]) * 1024 ** 3
    day_pct = (stats["today_total"] / daily_limit * 100) if daily_limit else 0
    new = [th for th in thresholds if daily_limit and day_pct >= th
           and f"day:{th:g}" not in state["day"]]
    if new:
        state["day"].extend(f"day:{th:g}" for th in new)
        msg = (f"今日已用 {day_pct:.0f}%（≥ {max(new):g}%）— "
               f"{gb(stats['today_total']):.1f} / {cfg['daily_limit_gb']:.0f} GB")
        fire_alert(f"{APP}：今日用量达到 {max(new):g}%", msg, cfg)
        messages.append(msg)

    save_state(state)
    return messages


# --------------------------------------------------------------------------- #
# vnstat daemon control
# --------------------------------------------------------------------------- #


def daemon_status() -> tuple[str, str]:
    try:
        active = run(["systemctl", "is-active", "vnstat"]).stdout.strip() or "unknown"
    except Exception:
        active = "unknown"
    try:
        enabled = run(["systemctl", "is-enabled", "vnstat"]).stdout.strip() or "unknown"
    except Exception:
        enabled = "unknown"
    return active, enabled


def daemon_toggle(action: str) -> tuple[bool, str]:
    try:
        cp = run(["systemctl", action, "vnstat"], timeout=20)
    except Exception as exc:
        return False, str(exc)
    if cp.returncode == 0:
        return True, f"vnstat {'已停止' if action == 'stop' else '已启动'}"
    err = (cp.stderr or cp.stdout).strip()
    if any(k in err.lower() for k in ("authentication", "interactive", "polkit")):
        return False, "权限不足，请安装 polkit 规则（参见 README.md）"
    return False, err or f"systemctl {action} 执行失败"


# --------------------------------------------------------------------------- #
# TUI
# --------------------------------------------------------------------------- #

C_DEFAULT, C_OK, C_WARN, C_BAD, C_ACCENT, C_DIM = 1, 2, 3, 4, 5, 6

SECTIONS = [
    ("流量套餐", ["interface", "cycle_start_day", "cycle_limit_gb", "daily_limit_gb"]),
    ("用量提醒", ["alert_thresholds_pct"]),
    ("声音与通知", ["sound_enabled", "sound_volume", "notifications"]),
    ("高级设置", ["refresh_seconds"]),
]

FIELD_META = {
    "interface": ("str", "网卡", "读取此网卡的 vnstat 数据"),
    "cycle_start_day": ("int", "周期起始日", "每月套餐重置日期（1–28）"),
    "cycle_limit_gb": ("float", "周期限额 (GB)", "每个计费周期的流量额度"),
    "daily_limit_gb": ("float", "每日限额 (GB)", "每日流量参考预算"),
    "alert_thresholds_pct": ("list", "提醒阈值 (%)", "用量达到限额的此百分比时提醒"),
    "sound_enabled": ("bool", "提示音", "提醒时播放短促提示音"),
    "sound_volume": ("float", "音量", "0.0–1.0"),
    "notifications": ("bool", "桌面通知", "提醒时发送桌面通知"),
    "refresh_seconds": ("int", "刷新间隔 (秒)", "自动刷新间隔"),
}
FIELD_ORDER = [k for _, keys in SECTIONS for k in keys]


def axis_unit(mx: float) -> tuple[float, str]:
    if mx >= 1024 ** 3:
        return 1024 ** 3, "GiB"
    if mx >= 1024 ** 2:
        return 1024 ** 2, "MiB"
    return 1024, "KiB"


class App:
    def __init__(self, stdscr, cfg: dict):
        self.stdscr = stdscr
        self.cfg = cfg
        self.stats: dict | None = None
        self.error: str | None = None
        self.message = ""
        self.alerts: list[str] = []
        self.last_refresh = 0.0
        self.settings_sel = 0
        self.editing = False
        self.edit_buf = ""
        self.running = True
        self.view = "dash"          # dash | settings | help | wizard
        self._dirty = True
        self._last_min = None
        self.wiz_values: dict = {}
        self.wiz_steps: list = []
        self.wiz_step = 0
        self.wiz_buf = ""
        self.live_rates = LiveRates()
        self.last_live_poll = 0.0
        self.history = HistoryView(self, HISTORY_PATH, fmt_bytes, pad_text,
                                   (C_ACCENT, C_OK, C_WARN), display_width, clip_text)

    # -- data ------------------------------------------------------------- #

    def refresh(self):
        try:
            self.stats = compute_stats(self.cfg["interface"], self.cfg)
            self.error = None
            new = check_alerts(self.stats, self.cfg)
            if new:
                self.alerts = (new + self.alerts)[:4]
        except Exception as exc:
            self.error = str(exc)
            if self.stats and self.stats.get("interface") != self.cfg["interface"]:
                self.stats = None
        if self.view == "history":
            self.history.reload(self.stats.get("traffic", {}) if self.stats else {})
        self.last_refresh = time.time()
        self._dirty = True

    # -- primitives ------------------------------------------------------- #

    def put(self, y, x, text, attr=0):
        h, w = self.stdscr.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w or not text:
            return
        try:
            self.stdscr.addstr(y, x, clip_text(text, w - x), attr)
        except curses.error:
            pass

    def bar_attr(self, pct: float) -> int:
        if pct >= 100:
            return curses.color_pair(C_BAD) | curses.A_BOLD
        if pct >= 80:
            return curses.color_pair(C_WARN) | curses.A_BOLD
        return curses.color_pair(C_OK)

    def init_colors(self):
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        curses.init_pair(C_OK, curses.COLOR_GREEN, bg)
        curses.init_pair(C_WARN, curses.COLOR_YELLOW, bg)
        curses.init_pair(C_BAD, curses.COLOR_RED, bg)
        curses.init_pair(C_ACCENT, curses.COLOR_CYAN, bg)
        curses.init_pair(C_DIM, curses.COLOR_BLUE, bg)

    # -- main loop -------------------------------------------------------- #

    def run(self):
        curses.curs_set(0)
        self.stdscr.keypad(True)
        self.stdscr.timeout(100)          # blocks up to 100 ms, then returns -1
        try:
            curses.set_escdelay(50)       # parse arrow-key escape sequences
        except (AttributeError, curses.error):
            pass
        self.init_colors()
        self.refresh()
        while self.running:
            if time.monotonic() - self.last_live_poll >= 1:
                self.live_rates.poll(self.cfg["interface"])
                self.last_live_poll = time.monotonic()
                if self.view in ("dash", "history"):
                    self._dirty = True
            if (time.time() - self.last_refresh) >= float(self.cfg["refresh_seconds"]):
                self.refresh()
            cur_min = time.strftime("%H:%M")
            if self._dirty or cur_min != self._last_min:
                self.draw()
                self._dirty = False
                self._last_min = cur_min
            try:
                ch = self.stdscr.getch()
            except curses.error:
                ch = -1
            if ch != -1:
                self.handle_key(ch)
                self._dirty = True

    def draw(self):
        self.stdscr.clear()
        h, w = self.stdscr.getmaxyx()
        if self.view == "settings":
            self.draw_settings(h, w)
        elif self.view == "help":
            self.draw_help(h, w)
        elif self.view == "wizard":
            self.draw_wizard(h, w)
        elif self.view == "history":
            self.history.draw(h, w)
        else:
            self.draw_dashboard(h, w)
            self.draw_footer(h, w)
        self.stdscr.refresh()

    # -- header ----------------------------------------------------------- #

    def draw_header(self, w, subtitle=""):
        active, enabled = daemon_status()
        if active == "active":
            dot, dot_attr = "●", curses.color_pair(C_OK) | curses.A_BOLD
            hint = "[d] 停止服务"
        else:
            dot, dot_attr = "○", curses.color_pair(C_BAD) | curses.A_BOLD
            hint = "[d] 启动服务"
        clock = time.strftime("%H:%M")
        inner = max(10, w - 2)
        self.put(0, 0, "╭" + "─" * inner + "╮", curses.color_pair(C_ACCENT))
        title = f" {APP} " + (f"· {subtitle} " if subtitle else "")
        right = f" 时间：{clock} "
        self.put(0, 2, title, curses.color_pair(C_ACCENT) | curses.A_BOLD)
        self.put(0, max(2, w - display_width(right) - 3), right, curses.A_DIM)
        self.put(1, 0, "│", curses.color_pair(C_ACCENT))
        self.put(1, 2, f"vnstat ", curses.A_DIM)
        self.put(1, 9, dot, dot_attr)
        self.put(1, 11, f"{daemon_label(active)}/{daemon_label(enabled)}", curses.A_DIM)
        self.put(1, max(2, w - display_width(hint) - 3), hint, curses.A_BOLD)
        self.put(1, w - 1, "│", curses.color_pair(C_ACCENT))
        self.put(2, 0, "╰" + "─" * inner + "╯", curses.color_pair(C_ACCENT))
        return 3

    # -- dashboard -------------------------------------------------------- #

    def draw_dashboard(self, h, w):
        y = self.draw_header(w)
        self.put(y, 2, f"实时：↓ {format_rate(self.live_rates.rx)}  ↑ {format_rate(self.live_rates.tx)}  · [g] 历史图表", curses.A_DIM)
        if self.stats is None:
            self.put(y + 1, 2, f"暂无数据：{self.error}", curses.color_pair(C_BAD))
            return
        s, cfg = self.stats, self.cfg

        y += 1
        cells = bar_cells_for_width(w - 4)
        cycle_limit = float(cfg["cycle_limit_gb"]) * 1024 ** 3
        cycle_pct = (s["cycle_used"] / cycle_limit * 100) if cycle_limit else 0
        daily_limit = float(cfg["daily_limit_gb"]) * 1024 ** 3
        day_pct = (s["today_total"] / daily_limit * 100) if daily_limit else 0

        # ---- CYCLE ----
        self.put(y, 2, "周期", curses.A_BOLD | curses.color_pair(C_ACCENT))
        self.put(y, 9, f"{s['cycle_start']} → {s['cycle_end']}   第 {s['cycle_elapsed']}/{s['cycle_days']} 天")
        y += 1
        self.put(y, 2, seg_bar(cycle_pct, cells), self.bar_attr(cycle_pct))
        y += 1
        expected = cycle_limit * s["cycle_elapsed"] / max(1, s["cycle_days"])
        on_track = s["cycle_used"] <= expected * 1.05
        pace = "● 预算内" if on_track else "▲ 超出预算进度"
        pace_attr = curses.color_pair(C_OK) if on_track else curses.color_pair(C_WARN)
        readout = f"{gb(s['cycle_used']):.1f} / {cfg['cycle_limit_gb']:.0f} GB   {cycle_pct:5.1f}%"
        self.put(y, 2, readout, self.bar_attr(cycle_pct))
        proj = f"预计 {gb(s['projected_cycle']):.1f} GB"
        self.put(y, 2 + display_width(readout) + 4, proj, 0)
        self.put(y, 2 + display_width(readout) + 4 + display_width(proj) + 3, pace, pace_attr | curses.A_BOLD)
        y += 2

        # ---- TODAY ----
        self.put(y, 2, "今日", curses.A_BOLD | curses.color_pair(C_ACCENT))
        y += 1
        self.put(y, 2, seg_bar(day_pct, cells), self.bar_attr(day_pct))
        y += 1
        self.put(y, 2, f"{gb(s['today_total']):.2f} / {cfg['daily_limit_gb']:.0f} GB   {day_pct:5.1f}%",
                 self.bar_attr(day_pct))
        y += 2

        # ---- AVERAGES | ALERTS (two columns) ----
        left_x, right_x = 2, max(46, w // 2 + 2)
        self.put(y, left_x, "平均用量", curses.A_BOLD | curses.color_pair(C_ACCENT))
        self.put(y, right_x, "用量提醒", curses.A_BOLD | curses.color_pair(C_ACCENT))
        y += 1
        rows = [
            (f"每在线小时  {fmt_bytes(s['avg_hour']):>10}", f"（共 {s['on_hours']} 小时）"),
            (f"每日        {fmt_bytes(s['avg_day']):>10}", "（近 7 天）"),
            (f"每周        {fmt_bytes(s['avg_week']):>10}", ""),
            (f"周期预计    {fmt_bytes(s['projected_cycle']):>10}", "（本周期）"),
        ]
        th = ",".join(f"{float(x):g}" for x in cfg.get("alert_thresholds_pct", []))
        alert_rows = [
            f"每日 {cfg['daily_limit_gb']:.0f} GB  阈值 {th}%",
            f"周期 {cfg['cycle_limit_gb']:.0f} GB  阈值 {th}%",
            f"声音：{'开' if cfg['sound_enabled'] else '关'}"
            f"   通知：{'开' if cfg['notifications'] else '关'}",
            "按 s 修改限额",
        ]
        for i in range(len(rows)):
            txt, dim = rows[i]
            self.put(y + i, left_x, txt, 0)
            if dim:
                self.put(y + i, left_x + display_width(txt) + 1, dim, curses.A_DIM)
            self.put(y + i, right_x, alert_rows[i],
                     curses.A_DIM if i == 3 else 0)
        y += len(rows) + 1

        # ---- charts (only if the terminal is tall enough) ----
        if h - y - 2 >= 8:
            y = self.draw_days_chart(y, w, s, daily_limit)
        if h - y - 2 >= 7:
            y = self.draw_hours_chart(y, w, s)

        if self.alerts and y < h - 2:
            self.put(y, 2, "• " + self.alerts[0], curses.color_pair(C_WARN))

    def _draw_dash_bars(self, y, vals, labels, height, barw, colw, x0, mx, div,
                        attr_for, label_step=1):
        plot_w = len(vals) * colw
        cols_by_row = [{} for _ in range(height)]
        for ci, v in enumerate(vals):
            attr = attr_for(v)
            for row, ch in bar_cells(v, mx, height).items():
                cols_by_row[row][ci] = (ch * barw, attr)
        grid_rows = {0, height // 2} if height >= 3 else {0}
        for row in range(height):
            level = height - row
            self.put(y + row, 2, f"{mx * level / height / div:>7.1f}", curses.A_DIM)
            self.put(y + row, 10, "│", curses.A_DIM)
            if row in grid_rows:
                self.put(y + row, x0, "┄" * plot_w, curses.A_DIM)
            for ci, (bar_str, attr) in cols_by_row[row].items():
                self.put(y + row, x0 + ci * colw, bar_str, attr)
        yr = y + height
        self.put(yr, 10, "└" + "─" * plot_w, curses.A_DIM)
        for ci, lab in enumerate(labels):
            if ci % label_step == 0 or ci == len(labels) - 1:
                self.put(yr + 1, x0 + ci * colw, pad_text(lab, barw, center=True), curses.A_DIM)
        return yr + 2

    def draw_days_chart(self, y, w, s, daily_limit):
        vals, labels = s["day_values"], s["day_labels"]
        if not vals:
            return y
        mx = max(vals) or 1
        div, unit = axis_unit(mx)
        title = f"近 {len(vals)} 天 ({unit})"
        self.put(y, 2, title, curses.A_BOLD | curses.color_pair(C_ACCENT))
        self.put(y, 2 + display_width(title) + 3, f"峰值 {mx / div:.1f} {unit}", curses.A_DIM)
        return self._draw_dash_bars(
            y + 1, vals, labels, height=5, barw=5, colw=7, x0=11, mx=mx, div=div,
            attr_for=lambda v: (curses.color_pair(C_BAD) if daily_limit and v > daily_limit
                                else curses.color_pair(C_OK)) | curses.A_BOLD)

    def draw_hours_chart(self, y, w, s):
        vals, labels = s["hour_values"], s["hour_labels"]
        if not vals:
            return y
        vals, labels = vals[-min(len(vals), 24):], labels[-min(len(labels), 24):]
        mx = max(vals) or 1
        div, unit = axis_unit(mx)
        self.put(y, 2, "近 24 小时", curses.A_BOLD | curses.color_pair(C_ACCENT))
        self.put(y, 24, f"峰值 {mx / div:.1f} {unit}", curses.A_DIM)
        return self._draw_dash_bars(
            y + 1, vals, labels, height=4, barw=2, colw=3, x0=11, mx=mx, div=div,
            attr_for=lambda v: curses.color_pair(C_ACCENT) | curses.A_BOLD,
            label_step=3)

    # -- first-run wizard -------------------------------------------------- #

    def start_wizard(self):
        """Begin the first-time setup wizard (also reachable with 'w')."""
        self.wiz_values = dict(self.cfg)
        found = list_interfaces()
        cur = self.cfg.get("interface")
        ifaces = ([cur] if cur else []) + [x for x in found if x != cur]
        if not ifaces:
            ifaces = ["wlp4s0"]
        self.wiz_values["interface"] = cur or ifaces[0]
        self.wiz_steps = [
            {"kind": "welcome", "title": "欢迎"},
            {"kind": "choice", "key": "interface", "title": "网卡",
             "choices": ifaces, "hint": "选择要读取的 vnstat 网卡（←/→ 切换）"},
            {"kind": "int", "key": "cycle_start_day", "title": "套餐重置日", "min": 1, "max": 28,
             "hint": "每月套餐流量重置的日期（1–28）"},
            {"kind": "float", "key": "cycle_limit_gb", "title": "套餐流量额度",
             "hint": "每个计费周期的总流量额度，单位 GB"},
            {"kind": "float", "key": "daily_limit_gb", "title": "每日预算",
             "hint": "每日流量参考预算，单位 GB"},
            {"kind": "list", "key": "alert_thresholds_pct", "title": "提醒阈值",
             "hint": "达到限额的此百分比时提醒，例如 80,100（英文逗号分隔）"},
            {"kind": "bool", "key": "sound_enabled", "title": "提示音",
             "hint": "触发提醒时播放短促提示音"},
            {"kind": "bool", "key": "notifications", "title": "桌面通知",
             "hint": "触发提醒时发送桌面通知"},
            {"kind": "summary", "title": "设置完成"},
        ]
        self.wiz_step = 0
        self.wiz_buf = ""
        self.view = "wizard"
        self._dirty = True

    def _wiz_step(self):
        return self.wiz_steps[self.wiz_step]

    def wiz_next(self):
        if self.wiz_step >= len(self.wiz_steps) - 1:
            self._wiz_finish()
            return
        self.wiz_step += 1
        self.wiz_buf = ""
        self.message = ""
        self._dirty = True

    def wiz_prev(self):
        if self.wiz_step > 0:
            self.wiz_step -= 1
            self.wiz_buf = ""
            self.message = ""
            self._dirty = True

    def _wiz_commit(self) -> bool:
        st = self._wiz_step()
        kind, key = st["kind"], st["key"]
        buf = self.wiz_buf.strip()
        if not buf:                      # empty input keeps the current value
            return True
        try:
            if kind == "int":
                v = clamp(int(float(buf)), st.get("min", 0), st.get("max", 10 ** 9))
            elif kind == "float":
                v = max(0.0, float(buf))
            else:                        # list
                v = sorted({float(x) for x in buf.replace(" ", "").split(",") if x})
                if not v:
                    raise ValueError
        except ValueError:
            self.message = "输入值无效"
            return False
        self.wiz_values[key] = v
        self.message = ""
        return True

    def _wiz_finish(self):
        self.cfg.update(self.wiz_values)
        self.cfg["setup_done"] = True
        save_config(self.cfg)
        self.view = "dash"
        self.message = "设置已保存"
        self.refresh()

    def _wiz_cancel(self):
        self.cfg["setup_done"] = True
        save_config(self.cfg)
        self.view = "dash"
        self.message = "已跳过设置（按 w 可重新运行向导）"
        self._dirty = True

    def handle_wizard_key(self, ch):
        st = self._wiz_step()
        kind = st["kind"]
        if ch == 27:
            self._wiz_cancel()
            return
        if kind == "welcome":
            if ch in (10, 13, curses.KEY_ENTER, ord(" ")):
                self.wiz_next()
            return
        if kind == "summary":
            if ch in (10, 13, curses.KEY_ENTER, ord(" ")):
                self._wiz_finish()
            elif ch in (curses.KEY_BACKSPACE, 127, 8, curses.KEY_LEFT):
                self.wiz_prev()
            return
        if kind == "choice":
            choices = st["choices"]
            key = st["key"]
            cur = self.wiz_values.get(key)
            idx = choices.index(cur) if cur in choices else 0
            if ch in (curses.KEY_LEFT, ord("h"), ord("-")):
                self.wiz_values[key] = choices[(idx - 1) % len(choices)]
            elif ch in (curses.KEY_RIGHT, ord("l"), ord("+"), ord("="), ord(" ")):
                self.wiz_values[key] = choices[(idx + 1) % len(choices)]
            elif ch in (10, 13, curses.KEY_ENTER):
                self.wiz_next()
            self._dirty = True
            return
        if kind == "bool":
            key = st["key"]
            if ch in (curses.KEY_LEFT, ord("h"), ord("-"), curses.KEY_RIGHT,
                      ord("l"), ord("+"), ord("="), ord(" ")):
                self.wiz_values[key] = not bool(self.wiz_values.get(key))
            elif ch in (10, 13, curses.KEY_ENTER):
                self.wiz_next()
            self._dirty = True
            return
        # int / float / list: text entry
        if ch in (10, 13, curses.KEY_ENTER):
            if self._wiz_commit():
                self.wiz_next()
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            self.wiz_buf = self.wiz_buf[:-1]
        elif 32 <= ch < 127:
            self.wiz_buf += chr(ch)
        self._dirty = True

    def draw_wizard(self, h, w):
        self.draw_header(w, subtitle="设置向导")
        st = self._wiz_step()
        kind = st["kind"]
        y = 4
        self.put(y, 2, f"第 {self.wiz_step + 1} 步，共 {len(self.wiz_steps)} 步", curses.A_DIM)
        y += 2

        if kind == "welcome":
            self.put(y, 2, "欢迎使用 datameter", curses.A_BOLD | curses.color_pair(C_ACCENT))
            y += 2
            for line in [
                "只需几步，即可设置流量套餐和用量提醒。",
                "之后可以按 s 在设置中修改所有选项，",
                "也可以随时按 w 重新运行此向导。",
            ]:
                self.put(y, 2, line)
                y += 1
            y += 1
            self.put(y, 2, "按 Enter 开始。", curses.A_BOLD)

        elif kind == "summary":
            self.put(y, 2, "确认并保存", curses.A_BOLD | curses.color_pair(C_ACCENT))
            y += 2
            v = self.wiz_values
            rows = [
                ("网卡", v.get("interface")),
                ("套餐重置日", v.get("cycle_start_day")),
                ("套餐流量额度", f"{float(v.get('cycle_limit_gb', 0)):g} GB"),
                ("每日预算", f"{float(v.get('daily_limit_gb', 0)):g} GB"),
                ("提醒阈值", ",".join(f"{float(x):g}" for x in v.get("alert_thresholds_pct", [])) + " %"),
                ("提示音", "开启" if v.get("sound_enabled") else "关闭"),
                ("桌面通知", "开启" if v.get("notifications") else "关闭"),
            ]
            for label, val in rows:
                self.put(y, 4, pad_text(label, 18), curses.A_DIM)
                self.put(y, 24, str(val))
                y += 1
            y += 1
            self.put(y, 2, "按 Enter 保存。", curses.A_BOLD)

        else:
            self.put(y, 2, st["title"], curses.A_BOLD | curses.color_pair(C_ACCENT))
            y += 2
            key = st["key"]
            cur = self.wiz_values.get(key)
            if kind == "choice":
                choices = st["choices"]
                idx = choices.index(cur) if cur in choices else 0
                self.put(y, 4, "◀  ")
                self.put(y, 7, str(cur), curses.A_BOLD)
                self.put(y, 7 + display_width(str(cur)) + 2, "▶")
                self.put(y, 7 + display_width(str(cur)) + 4, f"({idx + 1}/{len(choices)})", curses.A_DIM)
            elif kind == "bool":
                on = bool(cur)
                self.put(y, 4, f"[ {'开' if on else '关'} ]",
                         (curses.color_pair(C_OK) if on else curses.A_DIM) | curses.A_BOLD)
                self.put(y, 12, "已启用" if on else "已禁用", curses.A_DIM)
            else:
                if kind == "list":
                    shown = ",".join(f"{float(x):g}" for x in cur)
                elif kind == "float":
                    shown = f"{float(cur):g}"
                else:
                    shown = str(cur)
                if self.wiz_buf:
                    self.put(y, 4, self.wiz_buf + "█", curses.A_BOLD | curses.color_pair(C_ACCENT))
                    self.put(y, 4 + display_width(self.wiz_buf) + 3, f"（原值 {shown}）", curses.A_DIM)
                else:
                    self.put(y, 4, shown, curses.A_BOLD)
                    self.put(y, 4 + display_width(shown) + 3, "按 Enter 保留，或输入新值", curses.A_DIM)
            y += 2
            self.put(y, 2, st.get("hint", ""), curses.A_DIM)

        if kind == "welcome":
            keys = " Enter 开始 · Esc 跳过 "
        elif kind == "summary":
            keys = " Enter 保存 · Backspace 上一步 · Esc 跳过 "
        elif kind in ("choice", "bool"):
            keys = " ←/→ 切换 · Enter 下一步 · Esc 跳过 "
        else:
            keys = " 输入数值 · Enter 下一步 · Esc 跳过 "
        self.put(h - 1, 0, pad_text(keys, w - 1), curses.A_REVERSE)
        if self.message:
            self.put(h - 2, 2, self.message, curses.color_pair(C_ACCENT))

    # -- settings --------------------------------------------------------- #

    def draw_settings(self, h, w):
        self.put(0, 0, "╭" + "─" * max(10, w - 2) + "╮", curses.color_pair(C_ACCENT))
        self.put(0, 2, f" {APP} · 设置 ", curses.color_pair(C_ACCENT) | curses.A_BOLD)
        self.put(1, 0, "│", curses.color_pair(C_ACCENT))
        self.put(1, 2, f"配置文件：{CONFIG_PATH}", curses.A_DIM)
        self.put(2, 0, "├" + "─" * max(10, w - 2) + "┤", curses.color_pair(C_ACCENT))

        val_x, help_x = 28, 48
        y = 4
        sel_key = FIELD_ORDER[self.settings_sel]
        for section, keys in SECTIONS:
            self.put(y, 3, section, curses.A_BOLD | curses.color_pair(C_ACCENT))
            y += 1
            for key in keys:
                typ, label, help_ = FIELD_META[key]
                val = self.cfg.get(key)
                if typ == "list":
                    vstr = ",".join(str(x) for x in val)
                elif typ == "bool":
                    vstr = "开启" if val else "关闭"
                elif typ == "float":
                    vstr = f"{float(val):g}"
                else:
                    vstr = str(val)
                selected = key == sel_key
                attr = curses.A_REVERSE if selected else 0
                marker = "▸" if selected else " "
                self.put(y, 3, marker, curses.color_pair(C_ACCENT) if selected else curses.A_DIM)
                self.put(y, 5, pad_text(label, 20), attr)
                if selected and self.editing:
                    self.put(y, val_x, f"[{self.edit_buf}█]", curses.color_pair(C_ACCENT) | curses.A_BOLD)
                else:
                    self.put(y, val_x, pad_text(vstr, 16), attr)
                self.put(y, help_x, help_, curses.A_DIM)
                y += 1
            y += 1
        keys = " ↑/↓ 选择 · ←/→ 修改 · Enter 编辑 · t 试听 · s 保存返回 · Esc 返回 "
        self.put(h - 1, 0, pad_text(keys, w - 1), curses.A_REVERSE)
        if self.message:
            self.put(h - 2, 3, self.message, curses.color_pair(C_ACCENT))

    # -- help ------------------------------------------------------------- #

    def draw_help(self, h, w):
        self.put(0, 0, "╭" + "─" * max(10, w - 2) + "╮", curses.color_pair(C_ACCENT))
        self.put(0, 2, f" {APP} · 帮助 ", curses.color_pair(C_ACCENT) | curses.A_BOLD)
        y = 2
        lines = [
            ("快捷键", ""),
            ("  s", "打开设置：流量限额、提醒阈值和声音"),
            ("  g", "历史图表：按月/周/日查看上下行流量和速率"),
            ("  w", "重新运行首次设置向导"),
            ("  d", "启动或停止 vnstat 服务"),
            ("  t", "试听提示音"),
            ("  r", "立即刷新"),
            ("  ?", "显示本帮助"),
            ("  q", "退出"),
            ("", ""),
            ("用量提醒", ""),
            ("", "用量达到每日或周期限额的指定百分比时触发提醒。"),
            ("", "阈值使用百分比表示，可设置多个值。"),
            ("", "例如：每日 10 GB、每周期 300 GB，阈值为 80,100 时，"),
            ("", "当日达到 8 / 10 GB、周期达到 240 / 300 GB 时会提醒。"),
            ("", ""),
            ("", "每个阈值在每日和每个周期内分别只提醒一次。"),
            ("", "提示音由 tock.wav 生成，桌面通知通过 notify-send 发送。"),
            ("", ""),
            ("", "按 s → 用量提醒，设置提醒阈值 (%)。"),
            ("", "在上方的「流量套餐」中设置每日和周期限额。"),
        ]
        for i, (a, b) in enumerate(lines):
            if i > h - 3:
                break
            if a and not b:
                self.put(y, 3, a, curses.A_BOLD | curses.color_pair(C_ACCENT))
            elif a:
                self.put(y, 5, a, curses.A_BOLD | curses.color_pair(C_ACCENT))
                self.put(y, 9, b, 0)
            else:
                self.put(y, 5, b, 0)
            y += 1
        self.put(h - 1, 0, pad_text(" 按任意键返回 ", w - 1), curses.A_REVERSE)

    # -- footer ----------------------------------------------------------- #

    def draw_footer(self, h, w):
        active, _ = daemon_status()
        daemon_key = "[d] 停止服务" if active == "active" else "[d] 启动服务"
        keys = (f" [g] 图表 │ [s] 设置 │ [w] 向导 │ {daemon_key} │ [t] 试听 │ "
                f"[r] 刷新 │ [?] 帮助 │ [q] 退出 ")
        self.put(h - 1, 0, pad_text(keys, w - 1), curses.A_REVERSE)
        if self.message:
            self.put(h - 2, 2, self.message, curses.color_pair(C_ACCENT))

    # -- input ------------------------------------------------------------ #

    def handle_key(self, ch):
        if self.view == "help":
            self.view = "dash"
            return
        if self.view == "settings":
            self.handle_settings_key(ch)
            return
        if self.view == "wizard":
            self.handle_wizard_key(ch)
            return
        if self.view == "history":
            self.history.handle_key(ch)
            return
        if ch == ord("q"):
            self.running = False
        elif ch == ord("g"):
            self.view = "history"
            self.history.reload(self.stats.get("traffic", {}) if self.stats and self.stats.get("interface") == self.cfg["interface"] else {})
            self.history.select_anchor()
        elif ch == ord("w"):
            self.start_wizard()
        elif ch == ord("?"):
            self.view = "help"
        elif ch == ord("r"):
            self.refresh()
            self.message = "已刷新"
        elif ch == ord("s"):
            self.view = "settings"
            self.settings_sel = 0
            self.editing = False
        elif ch == ord("t"):
            self.message = "♪ 提示音试听"
            play_tock(3, float(self.cfg.get("sound_volume", 0.35)))
        elif ch == ord("d"):
            active, _ = daemon_status()
            action = "stop" if active == "active" else "start"
            ok, msg = daemon_toggle(action)
            self.message = ("✓ " if ok else "✗ ") + msg
            self.refresh()

    def handle_settings_key(self, ch):
        if self.editing:
            if ch in (10, 13, curses.KEY_ENTER):
                self.commit_edit()
            elif ch == 27:
                self.editing = False
                self.edit_buf = ""
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                self.edit_buf = self.edit_buf[:-1]
            elif 32 <= ch < 127:
                self.edit_buf += chr(ch)
            return

        if ch == 27:
            self.view = "dash"
        elif ch in (ord("q"),):
            self.running = False
        elif ch in (curses.KEY_UP, ord("k")):
            self.settings_sel = (self.settings_sel - 1) % len(FIELD_ORDER)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.settings_sel = (self.settings_sel + 1) % len(FIELD_ORDER)
        elif ch in (curses.KEY_LEFT, ord("-")):
            self.adjust(-1)
        elif ch in (curses.KEY_RIGHT, ord("+"), ord("=")):
            self.adjust(1)
        elif ch in (10, 13, curses.KEY_ENTER):
            key = FIELD_ORDER[self.settings_sel]
            typ = FIELD_META[key][0]
            if typ in ("str", "list"):
                self.editing = True
                if typ == "list":
                    self.edit_buf = ",".join(str(x) for x in self.cfg.get(key, []))
                else:
                    self.edit_buf = str(self.cfg.get(key, ""))
            else:
                self.adjust(1)
        elif ch == ord("t"):
            self.message = "♪ 提示音试听"
            play_tock(3, float(self.cfg.get("sound_volume", 0.35)))
        elif ch == ord("s"):
            save_config(self.cfg)
            self.message = "设置已保存"
            self.view = "dash"

    def adjust(self, delta):
        key = FIELD_ORDER[self.settings_sel]
        typ = FIELD_META[key][0]
        if typ == "bool":
            self.cfg[key] = not bool(self.cfg.get(key))
        elif typ == "int":
            self.cfg[key] = int(self.cfg.get(key, 0)) + delta
        elif typ == "float":
            step = 0.05 if key == "sound_volume" else 1.0
            self.cfg[key] = round(float(self.cfg.get(key, 0)) + delta * step, 3)
        if key == "cycle_start_day":
            self.cfg[key] = clamp(int(self.cfg[key]), 1, 28)
        if key == "refresh_seconds":
            self.cfg[key] = clamp(int(self.cfg[key]), 2, 3600)
        if key == "sound_volume":
            self.cfg[key] = clamp(float(self.cfg[key]), 0.0, 1.0)
        if key in ("cycle_limit_gb", "daily_limit_gb"):
            self.cfg[key] = max(0.0, float(self.cfg[key]))
        if typ in ("bool", "int", "float"):
            save_config(self.cfg)

    def commit_edit(self):
        key = FIELD_ORDER[self.settings_sel]
        typ = FIELD_META[key][0]
        buf = self.edit_buf.strip()
        try:
            if typ == "str":
                self.cfg[key] = buf
            elif typ == "list":
                self.cfg[key] = sorted({float(x) for x in buf.replace(" ", "").split(",") if x})
        except ValueError:
            self.message = "输入值无效"
        else:
            save_config(self.cfg)
            self.message = "已保存"
        self.editing = False
        self.edit_buf = ""


# --------------------------------------------------------------------------- #
# --once summary
# --------------------------------------------------------------------------- #


def print_summary(cfg: dict) -> int:
    try:
        s = compute_stats(cfg["interface"], cfg)
    except Exception as exc:
        print(f"{APP}：错误：{exc}")
        return 1
    active, enabled = daemon_status()
    limit = float(cfg["cycle_limit_gb"]) * 1024 ** 3
    pct = (s["cycle_used"] / limit * 100) if limit else 0
    print(f"{APP} — 网卡 {cfg['interface']}（vnstat 服务：{daemon_label(active)}/{daemon_label(enabled)}）")
    print(f"计费周期   : {s['cycle_start']} → {s['cycle_end']} "
          f"（第 {s['cycle_elapsed']}/{s['cycle_days']} 天）")
    print(f"周期已用   : {gb(s['cycle_used']):.2f} / {cfg['cycle_limit_gb']:.0f} GB ({pct:.1f}%)")
    print(f"周期预计   : {gb(s['projected_cycle']):.2f} GB")
    print(f"今日已用   : {gb(s['today_total']):.2f} / {cfg['daily_limit_gb']:.0f} GB")
    print(f"近 24 小时 : {fmt_bytes(s['last24'])}")
    print(f"每在线小时 : {fmt_bytes(s['avg_hour'])}")
    print(f"日均用量   : {fmt_bytes(s['avg_day'])}")
    print(f"周均用量   : {fmt_bytes(s['avg_week'])}")
    return 0


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


class ChineseArgumentParser(argparse.ArgumentParser):
    def format_usage(self):
        return super().format_usage().replace("usage: ", "用法：", 1)

    def format_help(self):
        return super().format_help().replace("usage: ", "用法：", 1)

    def error(self, message):
        message = message.replace("unrecognized arguments:", "无法识别的参数：")
        message = message.replace("expected one argument", "需要一个参数")
        message = message.replace("argument ", "参数 ")
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}：错误：{message}\n")


def main(argv=None) -> int:
    ap = ChineseArgumentParser(description="vnstat 流量监控终端面板（简体中文版）",
                               usage="%(prog)s [选项]", add_help=False)
    options = ap.add_argument_group("选项")
    options.add_argument("-h", "--help", action="help", help="显示帮助信息并退出")
    options.add_argument("--once", action="store_true", help="输出用量摘要后退出")
    options.add_argument("--check", action="store_true",
                    help="检查提醒阈值、发送通知后退出（用于定时任务）")
    options.add_argument("--interface", metavar="网卡", help="指定要监控的网卡")
    options.add_argument("--generate-sound", action="store_true",
                    help="生成或重新生成 tock.wav 后退出")
    options.add_argument("--history", action="store_true", help="直接打开历史流量柱状图与速率折线图")
    options.add_argument("--period", choices=("month", "week", "day"), help="历史统计范围：month 月 / week 周 / day 日")
    options.add_argument("--date", metavar="日期", help="跳转到 YYYY-MM-DD（月份可用 YYYY-MM，周可用 YYYY-Www）")
    args = ap.parse_args(argv)
    history_requested = args.history or args.period or args.date
    anchor = date.today()
    if args.date:
        try:
            anchor = parse_anchor(args.date, args.period or "month")
            if not 1970 <= anchor.year <= 9998:
                raise ValueError
        except ValueError:
            ap.error("日期无效，请使用 YYYY-MM-DD / YYYY-MM / YYYY-Www（1970–9998 年）")
    if history_requested and (args.once or args.check or args.generate_sound):
        ap.error("历史图表选项不能与 --once、--check 或 --generate-sound 同时使用")

    if not CONFIG_PATH.exists():
        base = dict(DEFAULT_CONFIG)
        base["interface"] = default_interface()
        save_config(base)
    first_run = not load_config().get("setup_done")
    cfg = load_config()
    if args.interface:
        cfg["interface"] = args.interface

    if args.generate_sound:
        generate_tock()
        print(f"已生成 {SOUND_PATH}")
        return 0

    if shutil.which("vnstat") is None and not (history_requested and HISTORY_PATH.exists()):
        print(f"{APP}：未安装 vnstat，请先安装并启动 vnstat 服务", file=sys.stderr)
        return 1

    if args.once:
        return print_summary(cfg)

    if args.check:
        try:
            stats = compute_stats(cfg["interface"], cfg)
        except Exception as exc:
            print(f"{APP}：错误：{exc}", file=sys.stderr)
            return 1
        for msg in check_alerts(stats, cfg):
            print(msg)
        return 0

    def _run(scr):
        app = App(scr, cfg)
        if history_requested:
            app.view = "history"
            app.history.mode = args.period or "month"
            app.history.anchor = anchor
        elif first_run:
            app.start_wizard()
        app.run()

    locale.setlocale(locale.LC_ALL, "")
    curses.wrapper(_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
