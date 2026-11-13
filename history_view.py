"""Interactive calendar traffic charts for the existing curses application."""
from __future__ import annotations

import curses
from datetime import date, datetime, timedelta
import math
import sqlite3

from terminal_charts import line_grid
from traffic_history import (HistoryStore, RESOLUTION_NAMES, build_report,
                             format_rate, midnight, normalize_snapshot,
                             parse_anchor, period_bounds, rate_unit, shift_month)


class HistoryView:
    def __init__(self, app, path, volume, pad, colors):
        self.app, self.store, self.volume, self.pad = app, HistoryStore(path), volume, pad
        self.colors = colors
        self.mode, self.anchor, self.fine, self.bits = "month", date.today(), False, False
        self.selected, self.report, self.traffic = 0, None, {}
        self.input_active, self.input_buffer, self.warning = False, "", ""
        self.interface = app.cfg["interface"]

    def reload(self, traffic=None):
        initial = self.report is None
        if self.interface != self.app.cfg["interface"]:
            self.interface = self.app.cfg["interface"]
            self.traffic = {}
        if traffic is not None:
            self.traffic = traffic
        self.warning = self.traffic.get("_history_error", "")
        first, last = period_bounds(self.mode, self.anchor)
        try:
            samples = self.store.read(self.app.cfg["interface"], midnight(first), midnight(last))
        except (OSError, sqlite3.Error) as exc:
            samples = []
            self.warning = f"历史归档不可用：{exc}"
        # Live data wins over an older archive copy, and remains usable if the
        # archive directory is read-only or a disk write fails.
        merged = {(s["resolution"], s["stamp"]): s for s in samples}
        merged.update({(s["resolution"], s["stamp"]): s for s in normalize_snapshot(self.traffic)})
        self.report = build_report(list(merged.values()), self.mode, self.anchor, self.fine)
        self.selected = min(self.selected, len(self.report["buckets"]) - 1)
        if initial:
            self.select_anchor()

    def select_anchor(self):
        if self.report:
            target = datetime.now().timestamp() if self.mode == "day" and self.anchor == date.today() else midnight(self.anchor)
            self.selected = next((i for i, b in enumerate(self.report["buckets"])
                                  if b["start"] <= target < b["end"]), 0)

    def change_range(self, mode=None, anchor=None):
        self.mode = mode or self.mode
        self.anchor = anchor or self.anchor
        self.reload()
        self.select_anchor()
        self.app.message = ""

    def handle_key(self, ch):
        app = self.app
        if self.input_active:
            if ch == 27:
                self.input_active = False
            elif ch in (10, 13, curses.KEY_ENTER):
                try:
                    anchor = parse_anchor(self.input_buffer, self.mode) if self.input_buffer.strip() else self.anchor
                    if not 1970 <= anchor.year <= 9998:
                        raise ValueError
                    self.change_range(anchor=anchor)
                except (ValueError, OverflowError, OSError):
                    app.message = "日期无效：输入 YYYY-MM-DD；月视图也可用 YYYY-MM，周视图可用 YYYY-Www"
                else:
                    self.input_active = False
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                self.input_buffer = self.input_buffer[:-1]
            elif ch in (ord("-"), ord("W"), ord("w")) or ord("0") <= ch <= ord("9"):
                self.input_buffer += chr(ch)
            return
        if ch == ord("q"):
            app.running = False
        elif ch in (27, ord("g")):
            app.view = "dash"
        elif ch in (ord("m"), ord("w"), ord("d")):
            self.change_range(mode={ord("m"): "month", ord("w"): "week", ord("d"): "day"}[ch])
        elif ch in (ord("["), ord("]")):
            delta = -1 if ch == ord("[") else 1
            try:
                anchor = shift_month(self.anchor, delta) if self.mode == "month" else self.anchor + timedelta(days=delta * (7 if self.mode == "week" else 1))
                if not 1970 <= anchor.year <= 9998:
                    raise ValueError
                self.change_range(anchor=anchor)
            except (ValueError, OverflowError):
                app.message = "已到支持的日期边界（1970–9998 年）"
        elif ch in (curses.KEY_LEFT, ord("h")):
            self.selected = max(0, self.selected - 1)
        elif ch in (curses.KEY_RIGHT, ord("l")):
            self.selected = min(len(self.report["buckets"]) - 1, self.selected + 1)
        elif ch == ord("/"):
            self.input_active, self.input_buffer = True, ""
            app.message = ""
        elif ch == ord("n"):
            self.change_range(anchor=date.today())
        elif ch == ord("u"):
            self.bits = not self.bits
        elif ch == ord("r"):
            app.refresh()
            app.message = "已刷新历史流量"
        elif ch == ord("f") and self.mode == "day":
            self.fine = not self.fine
            self.change_range()
        elif ch in (10, 13, curses.KEY_ENTER):
            bucket = self.report["buckets"][self.selected]
            if self.mode in ("month", "week"):
                self.change_range(mode="day", anchor=datetime.fromtimestamp(bucket["start"]).date())
            elif not self.fine:
                self.fine = True
                self.reload()
                self.selected = next((i for i, b in enumerate(self.report["buckets"])
                                      if b["start"] == bucket["start"]), 0)
            else:
                self.fine = False
                self.reload()
                self.selected = next((i for i, b in enumerate(self.report["buckets"])
                                      if b["start"] <= bucket["start"] < b["end"]), 0)

    def _attr(self, series):
        return curses.color_pair(self.colors[0 if series == 1 else 1 if series == 2 else 2])

    def draw_chart(self, y, w, height, buckets, line=False):
        put = self.app.put
        values = [(b["rx_rate"], b["tx_rate"]) if line else (b["rx"], b["tx"]) for b in buckets]
        maximum = max((v for pair in values for v in pair if v is not None), default=0) or 1
        if line:
            divisor, unit = rate_unit(maximum, self.bits)
            title = "速率折线图"
        else:
            divisor, unit = 1, "B"
            for name in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
                unit = name
                if maximum / divisor < 1024 or name == "PiB":
                    break
                divisor *= 1024
            title = "流量柱状图"
        granularity = "5分钟" if self.mode == "day" and self.fine else "小时" if self.mode == "day" else "日"
        put(y, 2, f"{title}（{unit}，每{granularity}）", curses.A_BOLD)
        put(y, max(40, w - 24), "● 下载" if line else "█ 下载", self._attr(1))
        put(y, max(58, w - 12), "◆ 上传" if line else "▓ 上传", self._attr(2))
        y += 1
        cell_width = min(8, max(3, (w - 14) // len(buckets)))
        plot_width = len(buckets) * cell_width
        for row in range(height):
            level = (height - 1 - row) / (height - 1)
            put(y + row, 1, f"{maximum * level / divisor:>8.1f} │", curses.A_DIM)
        if line:
            grid = line_grid([b["rx_rate"] for b in buckets], [b["tx_rate"] for b in buckets],
                             cell_width, height, maximum)
            for (row, column), (char, series) in grid.items():
                put(y + row, 11 + column, char, self._attr(series))
        else:
            bar_width = max(1, (cell_width - 1) // 2)
            for index, (rx, tx) in enumerate(values):
                for series, value in enumerate((rx, tx), 1):
                    if value is None:
                        continue
                    bar_height = min(height, math.ceil(value / maximum * height))
                    x = 11 + index * cell_width + (series - 1) * bar_width
                    for row in range(height - bar_height, height):
                        put(y + row, x, ("█" if series == 1 else "▓") * bar_width, self._attr(series))
        for index, bucket in enumerate(buckets):
            if bucket["rx"] is None:
                put(y + height - 1, 11 + index * cell_width + cell_width // 2,
                    "·" if bucket["future"] else "?", curses.A_DIM)
        put(y + height, 10, "└" + "─" * plot_width, curses.A_DIM)
        stride = max(1, math.ceil((len(buckets[0]["label"]) + 1) / cell_width))
        page_start = (self.selected // max(1, (w - 14) // 3)) * max(1, (w - 14) // 3)
        for index, bucket in enumerate(buckets):
            selected = page_start + index == self.selected
            if selected:
                # Selection uses a marker above the label; neighbouring labels
                # are omitted if they would collide with the selected label.
                put(y + height, 11 + index * cell_width + cell_width // 2, "▲", curses.A_BOLD)
            if selected or (index % stride == 0 and abs(index - (self.selected - page_start)) >= stride):
                put(y + height + 1, 11 + index * cell_width, bucket["label"],
                    curses.A_REVERSE if selected else curses.A_DIM)
        return y + height + 3

    def draw(self, h, w):
        if self.report is None:
            self.reload()
            self.select_anchor()
        app, put, report = self.app, self.app.put, self.report
        put(0, 2, f"datameter · 历史流量 · {app.cfg['interface']}", curses.A_BOLD | self._attr(1))
        footer = " m 月 · w 周 · d 日 · [ ] 前后 · / 日期 · n 今天 · u 单位 · r 刷新 · Esc 返回 · q 退出 "
        if w < 110:
            footer = " m月 w周 d日 [ ]前后 /日期 n今天 u单位 r刷新 Esc返回 q退出 "
        put(h - 1, 0, self.pad(footer, w - 1), curses.A_REVERSE)
        if h < 30 or w < 80:
            put(3, 2, "历史图表需要至少 80 列 × 30 行，请放大终端。", self._attr(3))
            return
        names = {"month": "月", "week": "周（周一至周日）", "day": "日"}
        put(2, 2, f"{names[self.mode]}视图：{report['start']} → {report['end'] - timedelta(days=1)}")
        live = app.live_rates
        put(3, 2, f"实时：↓ {format_rate(live.rx, self.bits)}  ↑ {format_rate(live.tx, self.bits)}"
            f"  会话峰值：↓ {format_rate(live.peak_rx, self.bits)}  ↑ {format_rate(live.peak_tx, self.bits)}")
        summary = report["summary"]
        volume = lambda v: self.volume(v) if v is not None else "暂无数据"
        put(5, 2, f"下载 {volume(summary['rx'])}    上传 {volume(summary['tx'])}    "
            f"合计 {volume(summary['rx'] + summary['tx']) if summary['rx'] is not None else '暂无数据'}", curses.A_BOLD)
        put(6, 2, f"均速（已记录时段）：↓ {format_rate(summary['rx_rate'], self.bits)}  ↑ {format_rate(summary['tx_rate'], self.bits)}")
        resolution = RESOLUTION_NAMES.get(report["peak_resolution"], "无记录")
        put(7, 2, f"历史峰值（{resolution}均速）：↓ {format_rate(report['peak']['rx'], self.bits)}  ↑ {format_rate(report['peak']['tx'], self.bits)}")
        put(8, 2, f"流量时段覆盖 {report['coverage']:.0%} · 峰值采样覆盖 {report['peak_coverage']:.0%} · ? 缺失，· 尚未发生，× 曲线重合", curses.A_DIM)
        capacity = max(1, (w - 14) // 3)
        start = self.selected // capacity * capacity
        buckets = report["buckets"][start:start + capacity]
        height = min(6, max(3, (h - 24) // 2))
        y = self.draw_chart(10, w, height, buckets)
        y = self.draw_chart(y, w, height, buckets, line=True)
        current = report["buckets"][self.selected]
        put(y, 2, f"选中：{current['title']}  [{self.selected + 1}/{len(report['buckets'])}]  "
            f"下载 {volume(current['rx'])}  上传 {volume(current['tx'])}")
        put(y + 1, 2, f"时段均速：↓ {format_rate(current['rx_rate'], self.bits)}  ↑ {format_rate(current['tx_rate'], self.bits)}"
            f"  已记录 {current['seconds'] / 60:.0f} 分钟", curses.A_DIM)
        if self.warning or app.error:
            put(h - 4, 2, self.warning or f"vnstat 更新失败，显示归档：{app.error}", self._attr(3))
        elif summary["rx"] is None:
            put(h - 4, 2, "此范围没有记录；已被 vnstat 清理的历史明细无法恢复。", self._attr(3))
        if self.input_active:
            put(h - 3, 2, f"跳转日期：{self.input_buffer}█  Enter 确认 / Esc 取消", self._attr(1))
        else:
            put(h - 3, 2, "←/→ 或 h/l 选点 · Enter 下钻/返回小时 · f 切换5分钟（日视图）", curses.A_DIM)
        if app.message:
            put(h - 2, 2, app.message, self._attr(1))
