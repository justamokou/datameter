"""Calendar, direction, rate, archive and graph regression tests."""
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from traffic_history import (HistoryStore, LiveRates, build_report, format_rate,
                             midnight, normalize_snapshot, parse_anchor,
                             period_bounds, shift_month)
from terminal_charts import bar_cells, braille_line_grid, line_grid


def entry(day=13, hour=0, minute=0, rx=600, tx=300):
    return {"date": {"year": 2024, "month": 11, "day": day},
            "time": {"hour": hour, "minute": minute},
            "timestamp": datetime(2024, 11, day, hour, minute).timestamp(), "rx": rx, "tx": tx}


def samples(**traffic):
    return normalize_snapshot(traffic, datetime(2025, 1, 1).timestamp())


class CalendarTests(unittest.TestCase):
    def test_month_week_and_leap_day(self):
        self.assertEqual(period_bounds("month", date(2024, 2, 29)), (date(2024, 2, 1), date(2024, 3, 1)))
        self.assertEqual(period_bounds("week", date(2025, 1, 1)), (date(2024, 12, 30), date(2025, 1, 6)))
        self.assertEqual(shift_month(date(2024, 1, 31), 1), date(2024, 2, 29))
        self.assertEqual(shift_month(date(2024, 1, 31), -1), date(2023, 12, 31))
        with self.assertRaises(ValueError):
            period_bounds("invalid", date.today())

    def test_date_input_and_iso_week(self):
        self.assertEqual(parse_anchor("2024-02", "month"), date(2024, 2, 1))
        self.assertEqual(parse_anchor("2025-W01", "week"), date(2024, 12, 30))
        self.assertEqual(parse_anchor("2024-02-29", "day"), date(2024, 2, 29))
        for value in ("2023-02-29", "2024-13", "2025-W54"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_anchor(value, "week" if "W" in value else "month")

    @unittest.skipUnless(hasattr(time, "tzset"), "Timezone changes require Linux")
    def test_dst_days_use_23_or_25_actual_hours(self):
        try:
            with patch.dict(os.environ, {"TZ": "America/New_York"}):
                time.tzset()
                spring = build_report([], "day", date(2024, 3, 10))
                autumn = build_report([], "day", date(2024, 11, 3))
                self.assertEqual(len(spring["buckets"]), 23)
                self.assertEqual(len(autumn["buckets"]), 25)
                repeated = [b for b in autumn["buckets"] if b["label"] == "01:00"]
                self.assertEqual(len(repeated), 2)
                self.assertNotEqual(repeated[0]["title"], repeated[1]["title"])
        finally:
            time.tzset()


class ReportTests(unittest.TestCase):
    def test_authoritative_totals_are_not_double_counted(self):
        data = samples(day=[entry(rx=9000, tx=4000)],
                       hour=[entry(rx=3600, tx=1800)],
                       fiveminute=[entry(rx=600, tx=300)])
        month = build_report(data, "month", date(2024, 11, 13))
        day = build_report(data, "day", date(2024, 11, 13))
        self.assertEqual((month["summary"]["rx"], month["summary"]["tx"]), (9000, 4000))
        self.assertEqual(day["summary"]["rx"], 9000)
        self.assertEqual(day["buckets"][0]["rx"], 3600)
        self.assertEqual(day["buckets"][0]["rx_rate"], 1)
        fine = build_report(data, "day", date(2024, 11, 13), fine=True)
        self.assertEqual(fine["buckets"][0]["rx"], 600)
        self.assertIsNone(fine["buckets"][1]["rx"])

    def test_fine_records_fill_only_uncovered_hours(self):
        data = samples(hour=[entry(rx=3600, tx=1800)],
                       fiveminute=[entry(rx=600), entry(hour=1, rx=1200, tx=300)])
        report = build_report(data, "day", date(2024, 11, 13))
        self.assertEqual(report["summary"]["rx"], 4800)
        self.assertEqual(report["summary"]["seconds"], 3900)
        self.assertEqual(report["buckets"][1]["rx_rate"], 4)

    def test_current_interval_uses_database_update_time_not_poll_time(self):
        at = datetime(2024, 11, 13, 0, 2).timestamp()
        data = normalize_snapshot({"fiveminute": [entry()], "_updated": {"timestamp": at}}, now=at + 90)
        report = build_report(data, "day", date(2024, 11, 13), fine=True, now=at + 90)
        self.assertEqual(report["buckets"][0]["seconds"], 120)
        self.assertEqual(report["buckets"][0]["rx_rate"], 5)
        self.assertEqual(report["peak"]["tx"], 2.5)
        self.assertTrue(report["buckets"][1]["future"])

    def test_created_mid_interval_is_respected(self):
        begin = datetime(2024, 11, 13, 0, 1).timestamp()
        data = normalize_snapshot({"fiveminute": [entry()], "_created": {"timestamp": begin}},
                                  now=datetime(2024, 11, 13, 0, 5).timestamp())
        self.assertEqual(data[0]["stop"] - data[0]["begin"], 240)

    def test_peaks_use_finest_available_fixed_resolution_and_both_directions(self):
        data = samples(day=[entry(rx=90000000)], hour=[entry(rx=9000000)],
                       fiveminute=[entry(rx=600, tx=1200), entry(minute=5, rx=1500, tx=300)])
        report = build_report(data, "day", date(2024, 11, 13))
        self.assertEqual(report["peak_resolution"], "fiveminute")
        self.assertEqual(report["peak"]["rx"], 5)
        self.assertEqual(report["peak"]["tx"], 4)
        self.assertNotEqual(report["peak"]["rx_at"], report["peak"]["tx_at"])
        self.assertAlmostEqual(report["peak_coverage"], 600 / 86400)
        fallback = build_report(samples(hour=[entry(rx=3600)]), "day", date(2024, 11, 13))
        self.assertEqual(fallback["peak_resolution"], "hour")
        self.assertEqual(fallback["peak"]["rx"], 1)

    def test_missing_zero_future_and_old_month_total_are_distinct(self):
        data = samples(day=[entry(rx=0, tx=0)])
        report = build_report(data, "month", date(2024, 11, 1), now=midnight(date(2024, 11, 15)))
        self.assertIsNone(report["buckets"][11]["rx"])
        self.assertEqual(report["buckets"][12]["rx"], 0)
        self.assertTrue(report["buckets"][14]["future"])
        month = dict(entry(day=1, rx=12345), date={"year": 2024, "month": 11})
        report = build_report(samples(month=[month]), "month", date(2024, 11, 5))
        self.assertEqual(report["summary"]["rx"], 12345)
        self.assertTrue(all(b["rx"] is None for b in report["buckets"]))
        self.assertEqual(report["peak_resolution"], "month")

    def test_monthrotate_does_not_leak_into_calendar_months(self):
        data = samples(month=[entry(day=18, rx=12345)])
        report = build_report(data, "month", date(2024, 11, 1))
        self.assertIsNone(report["summary"]["rx"])

    def test_rate_units_bits_and_bytes(self):
        self.assertEqual(format_rate(1024), "1.0 KiB/s")
        self.assertEqual(format_rate(125000, bits=True), "1.0 Mbit/s")
        self.assertEqual(format_rate(None), "暂无数据")
        self.assertEqual(format_rate(0), "0.0 B/s")


class ArchiveTests(unittest.TestCase):
    def test_upsert_stale_read_isolation_and_retention(self):
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.sqlite3")
            begin, end = midnight(date(2024, 11, 13)), midnight(date(2024, 11, 14))
            self.assertEqual(store.read("eth0", begin, end), [])
            store.ingest("eth0", {"hour": [entry(rx=10)]}, now=begin + 120)
            store.ingest("eth0", {"hour": [entry(rx=30)]}, now=begin + 180)
            store.ingest("eth0", {"hour": [entry(rx=5)]}, now=begin + 60)
            store.ingest("wlan0", {"hour": [entry(rx=90)]}, now=begin + 180)
            store.ingest("eth0", {"hour": [entry(day=14, rx=50)]}, now=end + 180)
            # vnstat can prune its older records; archived records stay queryable.
            records = store.read("eth0", begin, end)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["rx"], 30)
            self.assertEqual(store.read("wlan0", begin, end)[0]["rx"], 90)
            self.assertEqual(store.read("eth0", end, end + 86400)[0]["rx"], 50)

    def test_missing_parent_is_reported_not_created_silently(self):
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "missing" / "history.sqlite3")
            with self.assertRaises(sqlite3.OperationalError):
                store.ingest("eth0", {"day": [entry()]})


class LiveAndChartTests(unittest.TestCase):
    def test_live_speed_resets_baseline_after_counter_reset_or_interface_change(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "eth0" / "statistics"
            folder.mkdir(parents=True)
            live = LiveRates(root)
            def counters(rx, tx):
                (folder / "rx_bytes").write_text(str(rx))
                (folder / "tx_bytes").write_text(str(tx))
            counters(1000, 2000)
            live.poll("eth0", now=1)
            self.assertIsNone(live.rx)
            counters(3000, 3000)
            live.poll("eth0", now=3)
            self.assertEqual((live.rx, live.tx), (1000, 500))
            counters(20, 30)
            live.poll("eth0", now=4)
            self.assertIsNone(live.rx)
            counters(120, 230)
            live.poll("eth0", now=5)
            self.assertEqual((live.rx, live.tx), (100, 200))
            self.assertEqual(live.peak_rx, 1000)
            live.poll("other", now=6)
            self.assertIsNone(live.rx)
            self.assertIsNone(live.peak_rx)

    def test_line_graph_connects_samples_but_never_bridges_missing_data(self):
        connected = line_grid([0, 5, 10], [None, None, None], 3, 5, 10)
        self.assertTrue(any(x == 3 for y, x in connected))
        self.assertEqual(sum(char == "●" for char, series in connected.values()), 3)
        missing = line_grid([0, None, 10], [None, None, None], 3, 5, 10)
        self.assertEqual({x for y, x in missing}, {1, 7})
        crossed = line_grid([0, 5], [5, 0], 3, 5, 5)
        self.assertTrue(any(series == 3 for char, series in crossed.values()))

    def test_braille_line_and_eighth_block_bars(self):
        connected = braille_line_grid([0, 5, 10], [None, None, None], 3, 5, 10)
        self.assertTrue(any(x == 3 for y, x in connected))
        self.assertTrue(all("\u2800" <= ch <= "\u28ff" for ch, _ in connected.values()))
        missing = braille_line_grid([0, None, 10], [None, None, None], 3, 5, 10)
        self.assertEqual({x for y, x in missing}, {1, 7})
        crossed = braille_line_grid([0, 5], [5, 0], 3, 5, 5)
        self.assertTrue(any(series == 3 for _, series in crossed.values()))
        self.assertEqual(bar_cells(None, 10, 4), {})
        self.assertEqual(bar_cells(0, 10, 4), {})
        self.assertEqual(bar_cells(0.1, 100, 4), {3: "▁"})
        self.assertEqual(bar_cells(100, 100, 4), {0: "█", 1: "█", 2: "█", 3: "█"})


class HistoryViewTests(unittest.TestCase):
    def setUp(self):
        # Reuse the platform-specific curses shim only on Windows.
        from test_localization import dm
        self.dm = dm
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(dm, "HISTORY_PATH", Path(self.tmp.name) / "history.sqlite3")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = dm.App(None, dict(dm.DEFAULT_CONFIG, interface="eth0"))
        self.app.view = "history"
        self.view = self.app.history
        self.view.anchor = date(2024, 11, 13)
        self.view.reload({"day": [entry()], "hour": [entry()], "fiveminute": [entry()]})

    def test_navigation_jump_drill_and_speed_units(self):
        view = self.view
        view.selected = 12
        view.handle_key(10)
        self.assertEqual((view.mode, view.anchor), ("day", date(2024, 11, 13)))
        view.selected = 3
        view.handle_key(10)
        self.assertTrue(view.fine)
        self.assertEqual(view.selected, 36)
        view.handle_key(10)
        self.assertFalse(view.fine)
        self.assertEqual(view.selected, 3)
        view.handle_key(ord("u"))
        self.assertTrue(view.bits)
        view.handle_key(ord("["))
        self.assertEqual(view.anchor, date(2024, 11, 12))
        view.handle_key(ord("w"))
        view.handle_key(ord("/"))
        for char in "2025-W01":
            view.handle_key(ord(char))
        view.handle_key(10)
        self.assertEqual(view.report["start"], date(2024, 12, 30))
        self.assertFalse(view.input_active)

    def test_invalid_date_and_cancel_preserve_selection(self):
        view = self.view
        view.handle_key(ord("/"))
        for char in "2023-02-29":
            view.handle_key(ord(char))
        view.handle_key(10)
        self.assertTrue(view.input_active)
        self.assertIn("日期无效", self.app.message)
        self.assertEqual(view.anchor, date(2024, 11, 13))
        view.handle_key(27)
        self.assertFalse(view.input_active)
        self.assertEqual(self.app.view, "history")
        view.handle_key(27)
        self.assertEqual(self.app.view, "dash")

    def test_render_month_day_and_paged_fine_charts_within_terminal(self):
        for mode, fine, width, height in (("month", False, 110, 40), ("week", False, 80, 30),
                                          ("day", False, 110, 40), ("day", True, 110, 40),
                                          ("day", True, 80, 30)):
            with self.subTest(mode=mode, fine=fine, width=width):
                self.view.mode, self.view.fine = mode, fine
                self.view.reload()
                self.view.selected = len(self.view.report["buckets"]) - 1
                calls = []
                self.app.put = lambda y, x, text, attr=0: calls.append((y, x, text))
                with patch.object(self.dm.curses, "color_pair", return_value=0):
                    self.view.draw(height, width)
                self.assertTrue(any("流量柱状图" in text for y, x, text in calls))
                self.assertTrue(any("速率折线图" in text for y, x, text in calls))
                for y, x, text in calls:
                    self.assertTrue(0 <= y < height, (y, text))
                    self.assertLessEqual(x + self.dm.display_width(text), width, (x, text))

    def test_archive_failure_falls_back_to_live_snapshot(self):
        with patch.object(self.view.store, "read", side_effect=sqlite3.OperationalError("read-only")):
            self.view.reload()
        self.assertEqual(self.view.report["summary"]["rx"], 600)
        self.assertIn("归档不可用", self.view.warning)

    def test_legends_follow_the_heading_instead_of_terminal_right_edge(self):
        self.view.mode = "week"
        self.view.reload()
        for width in (80, 110, 132, 200):
            with self.subTest(width=width):
                calls = []
                self.app.put = lambda y, x, text, attr=0: calls.append((y, x, text))
                with patch.object(self.dm.curses, "color_pair", return_value=0):
                    self.view.draw(40, width)
                for name, download, upload in (("流量柱状图", "█ ↓下载", "█ ↑上传"),
                                                ("速率折线图", "● 下载", "◆ 上传")):
                    heading = next(c for c in calls if c[2].startswith(name))
                    rx = next(c for c in calls if c[2] == download)
                    tx = next(c for c in calls if c[2] == upload)
                    self.assertEqual(rx[:2], (heading[0], heading[1] + self.dm.display_width(heading[2]) + 2))
                    self.assertEqual(tx[:2], (rx[0], rx[1] + self.dm.display_width(rx[2]) + 2))
                    self.assertLess(rx[1], 55)

    def test_small_terminals_show_charts_and_allow_switching_without_blocking(self):
        for width, height in ((80, 24), (60, 20), (48, 18), (40, 14), (32, 12), (24, 10)):
            for mode, fine in (("month", False), ("week", False), ("day", False), ("day", True)):
                with self.subTest(width=width, height=height, mode=mode, fine=fine):
                    self.view.mode, self.view.fine, self.view.chart_line = mode, fine, False
                    self.view.show_table = False
                    self.view.reload()
                    self.view.selected = len(self.view.report["buckets"]) - 1
                    calls = []
                    self.app.put = lambda y, x, text, attr=0: calls.append((y, x, text))
                    with patch.object(self.dm.curses, "color_pair", return_value=0):
                        self.view.draw(height, width)
                        self.assertTrue(any(text.startswith("流量") and ("图" in text or "(" in text)
                                            for y, x, text in calls))
                        self.assertFalse(any("需要至少" in text for y, x, text in calls))
                        self.assertTrue(any(text == "█ ↓下载" for y, x, text in calls))
                        self.assertTrue(any(text == "█ ↑上传" for y, x, text in calls))
                        calls.clear()
                        self.view.handle_key(9)
                        self.view.draw(height, width)
                        self.assertTrue(any(text.startswith("速率") for y, x, text in calls))
                    for y, x, text in calls:
                        self.assertTrue(0 <= y < height)
                        self.assertLessEqual(x + self.dm.display_width(text), width)

    def test_table_view_toggle_and_navigation_within_bounds(self):
        self.view.mode = "month"
        self.view.reload()
        for width, height in ((110, 40), (80, 24), (48, 18)):
            with self.subTest(width=width, height=height):
                self.view.show_table = False
                self.view.handle_key(ord("t"))
                self.assertTrue(self.view.show_table)
                self.view.handle_key(self.dm.curses.KEY_DOWN)
                calls = []
                self.app.put = lambda y, x, text, attr=0: calls.append((y, x, text))
                with patch.object(self.dm.curses, "color_pair", return_value=0):
                    self.view.draw(height, width)
                self.assertTrue(any("时段明细表" in text for _, _, text in calls))
                for y, x, text in calls:
                    self.assertTrue(0 <= y < height, (y, text))
                    self.assertLessEqual(x + self.dm.display_width(text), width, (x, text))
                self.view.handle_key(ord("t"))
                self.assertFalse(self.view.show_table)

    def test_compact_statistics_can_cycle_through_peak_live_and_coverage(self):
        calls = []
        self.app.put = lambda y, x, text, attr=0: calls.append((y, x, text))
        for expected in ("均速", "峰/", "实时", "会话峰", "流量覆盖", "合计"):
            calls.clear()
            with patch.object(self.dm.curses, "color_pair", return_value=0):
                self.view.draw(18, 48)
            self.assertTrue(any(text.startswith(expected) for y, x, text in calls), expected)
            self.view.handle_key(ord("i"))

    def test_extremely_tiny_terminal_remains_safe(self):
        calls = []
        self.app.put = lambda y, x, text, attr=0: calls.append((y, x, text))
        with patch.object(self.dm.curses, "color_pair", return_value=0):
            self.view.draw(5, 12)
        for y, x, text in calls:
            self.assertTrue(0 <= y < 5)
            self.assertLessEqual(x + self.dm.display_width(text), 12)

    def test_interface_switch_does_not_reuse_old_live_traffic(self):
        self.app.cfg["interface"] = "wlan0"
        self.view.reload()
        self.assertIsNone(self.view.report["summary"]["rx"])

    def test_vnstat_requests_all_entries_and_archives_metadata(self):
        traffic = {"day": [entry()]}
        output = json.dumps({"interfaces": [{"name": "eth0", "traffic": traffic,
                                             "updated": {"timestamp": midnight(date(2024, 11, 14))}}]})
        with patch.object(self.dm, "run", return_value=subprocess.CompletedProcess([], 0, output)) as run:
            snapshot = self.dm.vnstat_snapshot("eth0")
        self.assertEqual(run.call_args[0][0], ["vnstat", "--json", "a", "0", "-i", "eth0"])
        records = self.view.store.read("eth0", midnight(date(2024, 11, 13)), midnight(date(2024, 11, 14)))
        self.assertEqual(records[0]["rx"], 600)
        self.assertIn("_updated", snapshot)


if __name__ == "__main__":
    unittest.main()
