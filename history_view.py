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
    def __init__(self, app, path, volume, pad, colors, text_width, clip):
        self.app, self.store, self.volume, self.pad = app, HistoryStore(path), volume, pad
        self.text_width, self.clip = text_width, clip
        self.colors = colors
        self.mode, self.anchor, self.fine, self.bits = "month", date.today(), False, False
        self.selected, self.report, self.traffic = 0, None, {}
        self.input_active, self.input_buffer, self.warning = False, "", ""
        self.interface = app.cfg["interface"]
        self.chart_line, self.summary_page = False, 0
        self.size = (40, 110)

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
        elif ch == 9:
            self.chart_line = not self.chart_line
        elif ch == ord("i"):
            self.summary_page = (self.summary_page + 1) % 6
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

    def _put(self, y, x, text, attr=0):
        h, w = self.size
        if 0 <= y < h and 0 <= x < w:
            text = self.clip(text, w - x)
            if text:
                self.app.put(y, x, text, attr)

    def _plot_geometry(self, w):
        plot_x = 11 if w >= 60 else 8
        capacity = max(1, (w - plot_x - 1) // 3)
        return plot_x, capacity

    def _chart_spec(self, w, buckets, line):
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
        title = f"{title}（{unit}，每{granularity}）"
        if w < 64:
            grain = "5分" if self.mode == "day" and self.fine else "时" if self.mode == "day" else "日"
            title = f"{'速率' if line else '流量'}({unit}/{grain})"
        legends = ("● 下载", "◆ 上传") if line else ("█ 下载", "▓ 上传")
        left = 1 if w < 64 else 2
        legend_x = left + self.text_width(title) + 2
        rows = 1
        if legend_x + sum(self.text_width(t) for t in legends) + 2 > w - 1:
            # Keep both labels together next to the chart, never at the far
            # edge of the terminal. Narrow headings can use a second line.
            rows, legend_x = 2, left
        return maximum, divisor, title, legends, left, legend_x, rows

    def draw_chart(self, y, w, height, buckets, line=False, page_start=0, gap=1):
        put = self._put
        maximum, divisor, title, legends, left, legend_x, heading_rows = self._chart_spec(w, buckets, line)
        put(y, left, title, curses.A_BOLD)
        legend_y = y + heading_rows - 1
        put(legend_y, legend_x, legends[0], self._attr(1))
        put(legend_y, legend_x + self.text_width(legends[0]) + 2, legends[1], self._attr(2))
        y += heading_rows
        plot_x, _ = self._plot_geometry(w)
        cell_width = min(8, max(3, (w - plot_x - 1) // len(buckets)))
        plot_width = len(buckets) * cell_width
        for row in range(height):
            level = (height - 1 - row) / (height - 1) if height > 1 else 1
            label_width = 8 if w >= 60 else 6
            put(y + row, 1 if w >= 60 else 0, f"{maximum * level / divisor:>{label_width}.1f} │", curses.A_DIM)
        if line:
            grid = line_grid([b["rx_rate"] for b in buckets], [b["tx_rate"] for b in buckets],
                             cell_width, height, maximum)
            for (row, column), (char, series) in grid.items():
                put(y + row, plot_x + column, char, self._attr(series))
        else:
            bar_width = max(1, (cell_width - 1) // 2)
            for index, bucket in enumerate(buckets):
                rx, tx = bucket["rx"], bucket["tx"]
                for series, value in enumerate((rx, tx), 1):
                    if value is None:
                        continue
                    bar_height = min(height, math.ceil(value / maximum * height))
                    x = plot_x + index * cell_width + (series - 1) * bar_width
                    for row in range(height - bar_height, height):
                        put(y + row, x, ("█" if series == 1 else "▓") * bar_width, self._attr(series))
        for index, bucket in enumerate(buckets):
            if bucket["rx"] is None:
                put(y + height - 1, plot_x + index * cell_width + cell_width // 2,
                    "·" if bucket["future"] else "?", curses.A_DIM)
        put(y + height, plot_x - 1, "└" + "─" * plot_width, curses.A_DIM)
        stride = max(1, math.ceil((len(buckets[0]["label"]) + 1) / cell_width))
        selected_index = self.selected - page_start
        def label_x(index, label):
            center = plot_x + index * cell_width + cell_width // 2
            return max(plot_x, min(w - self.text_width(label) - 1, center - self.text_width(label) // 2))
        selected_label = buckets[selected_index]["label"]
        selected_x = label_x(selected_index, selected_label)
        for index, bucket in enumerate(buckets):
            selected = page_start + index == self.selected
            if selected:
                # Selection uses a marker above the label; neighbouring labels
                # are omitted if they would collide with the selected label.
                put(y + height, plot_x + index * cell_width + cell_width // 2, "▲", curses.A_BOLD)
            x = label_x(index, bucket["label"])
            overlaps = x < selected_x + self.text_width(selected_label) + 1 and x + self.text_width(bucket["label"]) + 1 > selected_x
            if selected or (index % stride == 0 and not overlaps):
                put(y + height + 1, x, bucket["label"],
                    curses.A_REVERSE if selected else curses.A_DIM)
        return y + height + 2 + gap

    def draw(self, h, w):
        self.size = (h, w)
        if self.report is None:
            self.reload()
            self.select_anchor()
        app, put, report = self.app, self._put, self.report
        full = h >= 30 and w >= 80
        left = 2 if full else 1
        names = {"month": "月", "week": "周（周一至周日）" if full else "周", "day": "日"}
        period = f"{names[self.mode]}视图：{report['start']} → {report['end'] - timedelta(days=1)}"
        live = app.live_rates
        summary = report["summary"]
        volume = lambda v: self.volume(v) if v is not None else "暂无数据"
        resolution = RESOLUTION_NAMES.get(report["peak_resolution"], "无记录")
        total = summary['rx'] + summary['tx'] if summary['rx'] is not None else None
        if full:
            put(0, left, f"datameter · 历史流量 · {app.cfg['interface']}", curses.A_BOLD | self._attr(1))
            put(1, left, period)
            put(2, left, f"实时：↓ {format_rate(live.rx, self.bits)}  ↑ {format_rate(live.tx, self.bits)}"
                f"  会话峰值：↓ {format_rate(live.peak_rx, self.bits)}  ↑ {format_rate(live.peak_tx, self.bits)}")
            put(3, left, f"下载 {volume(summary['rx'])}    上传 {volume(summary['tx'])}    合计 {volume(total)}", curses.A_BOLD)
            put(4, left, f"均速（已记录时段）：↓ {format_rate(summary['rx_rate'], self.bits)}  ↑ {format_rate(summary['tx_rate'], self.bits)}")
            put(5, left, f"历史峰值（{resolution}均速）：↓ {format_rate(report['peak']['rx'], self.bits)}  ↑ {format_rate(report['peak']['tx'], self.bits)}")
            put(6, left, f"流量时段覆盖 {report['coverage']:.0%} · 峰值采样覆盖 {report['peak_coverage']:.0%} · ? 缺失，· 尚未发生，× 曲线重合", curses.A_DIM)
            chart_start = 8
        else:
            chart_start = 5 if h >= 18 else 4 if h >= 14 else 3 if h >= 11 else 2
            rate = lambda v: format_rate(v, self.bits).replace("暂无数据", "—").replace(" ", "")
            vol = lambda v: volume(v).replace("暂无数据", "—").replace(" ", "")
            summaries = [
                f"均速 ↓{rate(summary['rx_rate'])} ↑{rate(summary['tx_rate'])}",
                f"峰/{resolution} ↓{rate(report['peak']['rx'])} ↑{rate(report['peak']['tx'])}",
                f"实时 ↓{rate(live.rx)} ↑{rate(live.tx)}",
                f"会话峰 ↓{rate(live.peak_rx)} ↑{rate(live.peak_tx)}",
                f"流量覆盖{report['coverage']:.0%} 峰值覆盖{report['peak_coverage']:.0%} ?缺失",
                f"合计 {vol(total)} · ×曲线重合 · ·尚未发生",
            ]
            period = f"{names[self.mode]} {report['start']}~{report['end'] - timedelta(days=1)}"
            if chart_start >= 3:
                put(0, left, f"datameter · 历史流量 · {app.cfg['interface']}", curses.A_BOLD | self._attr(1))
                put(1, left, period)
                totals = f"↓{vol(summary['rx'])} ↑{vol(summary['tx'])}  " + (f"合计{vol(total)}" if w >= 60 else "")
                put(2, left, summaries[self.summary_page] if chart_start < 4 and self.summary_page else totals,
                    curses.A_DIM if chart_start < 4 and self.summary_page else curses.A_BOLD)
                if chart_start >= 4:
                    put(3, left, summaries[self.summary_page], curses.A_DIM)
            else:
                put(0, left, period)
                put(1, left, summaries[self.summary_page], curses.A_DIM)
        if h < 10 or w < 24:
            put(h - 1, 0, "Tab切图 i详情 Esc q退出", curses.A_REVERSE)
            return
        _, capacity = self._plot_geometry(w)
        start = self.selected // capacity * capacity
        buckets = report["buckets"][start:start + capacity]
        gap = 1 if full else 0
        status_y = h - 3
        detail_rows = 2 if h >= 18 else 1
        overheads = [self._chart_spec(w, buckets, line)[-1] + 2 + gap for line in (False, True)]
        plot_budget = status_y - chart_start - detail_rows - sum(overheads)
        dual = plot_budget >= 4
        if dual:
            height = min(10, plot_budget // 2)
            y = self.draw_chart(chart_start, w, height, buckets, page_start=start, gap=gap)
            y = self.draw_chart(y, w, height, buckets, line=True, page_start=start, gap=gap)
        else:
            overhead = overheads[int(self.chart_line)]
            if status_y - chart_start - detail_rows - overhead < 1:
                detail_rows = 0
            height = max(1, status_y - chart_start - detail_rows - overhead)
            y = self.draw_chart(chart_start, w, height, buckets, line=self.chart_line, page_start=start, gap=gap)
        current = report["buckets"][self.selected]
        if full:
            selected = f"选中：{current['title']}  [{self.selected + 1}/{len(report['buckets'])}]  下载 {volume(current['rx'])}  上传 {volume(current['tx'])}"
        else:
            label = current['title'] if w >= 60 else current['label']
            selected = f"{label} [{self.selected + 1}/{len(report['buckets'])}] ↓{volume(current['rx'])} ↑{volume(current['tx'])}"
        if detail_rows:
            put(y, left, selected)
        if detail_rows == 2:
            put(y + 1, left, f"时段均速：↓ {format_rate(current['rx_rate'], self.bits)}  ↑ {format_rate(current['tx_rate'], self.bits)}"
                + (f"  已记录 {current['seconds'] / 60:.0f} 分钟" if w >= 80 else ""), curses.A_DIM)
        if self.input_active:
            put(status_y, left, f"跳转日期：{self.input_buffer}█  Enter确认/Esc取消", self._attr(1))
        elif app.message or self.warning or app.error:
            put(status_y, left, app.message or self.warning or f"vnstat 更新失败：{app.error}", self._attr(3))
        elif summary["rx"] is None:
            put(status_y, left, "此范围没有记录；已清理的明细无法恢复。", self._attr(3))
        else:
            put(status_y, left, "←/→选点 Enter下钻 f切换5分钟" if dual else "Tab切换流量/速率 i切换统计", curses.A_DIM)
        if w >= 80:
            footer = " m/w/d月周日 [ ]前后 /日期 n今天 u单位 r刷新 Esc返回 q退出 "
            hints = " ←/→选点 Enter下钻 f 5分钟 " + ("i详情" if not full else "") + (" Tab切图" if not dual else "")
        elif w >= 48:
            footer = " m/w/d范围 [ ]前后 /日期 Esc返回 q退出 "
            hints = " ←/→选点 Enter下钻 f 5分钟 i详情" + (" Tab切图" if not dual else "")
        else:
            footer = "m/w/d [] /日期 Esc q退出"
            hints = "←→选点 Enter下钻 i详情" + (" Tab切图" if not dual else "")
        put(h - 2, 0, hints, curses.A_DIM)
        put(h - 1, 0, self.pad(footer, w - 1), curses.A_REVERSE)
