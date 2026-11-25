"""Interactive calendar traffic charts and tabular breakdown for the curses app."""
from __future__ import annotations

import curses
from datetime import date, datetime, timedelta
import math
import sqlite3

from terminal_charts import bar_cells, braille_line_grid
from traffic_history import (HistoryStore, RESOLUTION_NAMES, build_report,
                             format_rate, midnight, normalize_snapshot,
                             parse_anchor, period_bounds, rate_unit, shift_month)

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


class HistoryView:
    def __init__(self, app, path, volume, pad, colors, text_width, clip):
        self.app, self.store, self.volume, self.pad = app, HistoryStore(path), volume, pad
        self.text_width, self.clip = text_width, clip
        self.colors = colors
        self.mode, self.anchor, self.fine, self.bits = "month", date.today(), False, False
        self.selected, self.report, self.traffic = 0, None, {}
        self.input_active, self.input_buffer, self.warning = False, "", ""
        self.interface = app.cfg["interface"]
        self.chart_line, self.show_table, self.summary_page = False, False, 0
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

    def _is_dual_budget(self, h, w):
        if h < 10 or w < 24 or not self.report:
            return False
        card_mode = h >= 22 and w >= 76
        if card_mode:
            header_h = 4 if (h >= 28 and w >= 80) else 3
            bottom_reserve = 6 if (h >= 34 and w >= 80) else 3
        else:
            header_h = 5 if h >= 18 else 4 if h >= 14 else 3 if h >= 11 else 2
            bottom_reserve = 2 if h >= 18 else 1
        has_status = self.input_active or self.app.message or self.warning or self.app.error or self.report["summary"]["rx"] is None
        status_y = h - 3 if has_status else h - 2
        gap = 1 if h >= 26 and w >= 76 else 0
        _, _, _, _, capacity = self._plot_layout(w, len(self.report["buckets"]), 5)
        start = self.selected // capacity * capacity
        buckets = self.report["buckets"][start:start + capacity]
        overheads = [self._chart_spec(w, buckets, line)[-1] + 2 + gap for line in (False, True)]
        plot_budget = status_y - header_h - bottom_reserve - sum(overheads)
        return plot_budget >= 4

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
        elif ch in (curses.KEY_LEFT, ord("h")) or (self.show_table and ch in (curses.KEY_UP, ord("k"))):
            self.selected = max(0, self.selected - 1)
        elif ch in (curses.KEY_RIGHT, ord("l")) or (self.show_table and ch in (curses.KEY_DOWN, ord("j"))):
            self.selected = min(len(self.report["buckets"]) - 1, self.selected + 1)
        elif ch == ord("/"):
            self.input_active, self.input_buffer = True, ""
            app.message = ""
        elif ch == ord("n"):
            self.change_range(anchor=date.today())
        elif ch == ord("u"):
            self.bits = not self.bits
        elif ch == ord("t"):
            self.show_table = not self.show_table
        elif ch == 9:
            self.show_table = False
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

    def _draw_box_top(self, y, left, width, title, right_badge=""):
        if width < 6:
            return
        inner = width - 2
        self._put(y, left, "╭" + "─" * inner + "╮", curses.A_DIM)
        if title:
            t = f" {title} "
            max_t = max(0, inner - 2)
            t = self.clip(t, max_t)
            self._put(y, left + 2, t, curses.A_BOLD | self._attr(1))
            title_end = left + 2 + self.text_width(t)
        else:
            title_end = left + 2
        if right_badge:
            b = f" {right_badge} "
            bw = self.text_width(b)
            bx = left + width - 2 - bw
            if bx > title_end + 1:
                self._put(y, bx, b, curses.A_DIM)

    def _draw_box_bottom(self, y, left, width):
        if width < 6:
            return
        self._put(y, left, "╰" + "─" * (width - 2) + "╯", curses.A_DIM)

    def _draw_box_row(self, y, left, width, items):
        """Render │-delimited styled segments inside a rounded card row."""
        if width < 6:
            return
        self._put(y, left, "│", curses.A_DIM)
        self._put(y, left + width - 1, "│", curses.A_DIM)
        x = left + 2
        limit = left + width - 2
        for idx, spans in enumerate(items):
            if x >= limit:
                break
            if idx > 0:
                if x + 3 >= limit:
                    break
                self._put(y, x, " │ ", curses.A_DIM)
                x += 3
            for text, attr in spans:
                if x >= limit:
                    break
                piece = self.clip(text, limit - x)
                if piece:
                    self._put(y, x, piece, attr)
                    x += self.text_width(piece)

    def _compact_rate(self, value):
        if value is None:
            return "—"
        divisor, unit = rate_unit(value, self.bits)
        short = unit.replace("iB/s", "/s").replace("B/s", "B/s").replace("bit/s", "b/s")
        return f"{value / divisor:.1f}{short}"

    def _compact_vol(self, value):
        if value is None:
            return "—"
        return self.volume(value).replace(" ", "")

    def _tick_format(self, maximum, divisor):
        return "{:.0f}" if maximum / divisor >= 100 else "{:.1f}"

    def _plot_layout(self, w, total_buckets, tick_w):
        left = 2 if w >= 60 else 0
        plot_x = left + tick_w + 2
        avail = max(3, w - plot_x - 1)
        if total_buckets * 3 <= avail:
            cell_width = min(8, avail // max(1, total_buckets))
            capacity = max(1, total_buckets)
        elif total_buckets <= 31 and total_buckets * 2 <= avail:
            cell_width = 2
            capacity = max(1, total_buckets)
        else:
            cell_width = 3
            capacity = max(1, avail // cell_width)
        return left, tick_w, plot_x, cell_width, capacity

    def _chart_spec(self, w, buckets, line, page_info=""):
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
        title = f"{title}（{unit}，每{granularity}{page_info}）"
        if w < 64:
            grain = "5分" if self.mode == "day" and self.fine else "时" if self.mode == "day" else "日"
            title = f"{'速率' if line else '流量'}({unit}/{grain}{page_info})"
        legends = ("● 下载", "◆ 上传") if line else ("↓█ 下载", "↑█ 上传")
        left = 1 if w < 64 else 2
        legend_x = left + self.text_width(title) + 2
        rows = 1
        if legend_x + sum(self.text_width(t) for t in legends) + 2 > w - 1:
            # Keep both labels together next to the chart, never at the far
            # edge of the terminal. Narrow headings can use a second line.
            rows, legend_x = 2, left
        fmt = self._tick_format(maximum, divisor)
        tick_w = max(4, len(fmt.format(maximum / divisor)))
        return maximum, divisor, fmt, tick_w, title, legends, left, legend_x, rows

    def _bucket_axis_label(self, bucket, cell_width):
        if self.mode == "week" and cell_width >= 8:
            day = datetime.fromtimestamp(bucket["start"]).date()
            return f"{WEEKDAYS[day.weekday()]}{day.strftime('%d')}"
        return bucket["label"]

    def draw_chart(self, y, w, height, buckets, line=False, page_start=0,
                   gap=1, layout=None, page_info=""):
        put = self._put
        maximum, divisor, fmt, tick_w, title, legends, left, legend_x, heading_rows = self._chart_spec(
            w, buckets, line, page_info=page_info)
        put(y, left, title, curses.A_BOLD)
        legend_y = y + heading_rows - 1
        put(legend_y, legend_x, legends[0], self._attr(1) | curses.A_BOLD)
        put(legend_y, legend_x + self.text_width(legends[0]) + 2, legends[1], self._attr(2))
        y += heading_rows

        if layout is None:
            axis_left, axis_w, plot_x, cell_width, _ = self._plot_layout(w, len(buckets), tick_w)
        else:
            axis_left, axis_w, plot_x, cell_width, _ = layout
        plot_width = len(buckets) * cell_width

        # Decide which rows get Y-axis labels and horizontal reference gridlines.
        tick_rows = {0, height - 1}
        if height >= 3:
            tick_rows.add(height // 2)
        if height >= 7:
            tick_rows.update({height // 4, (height * 3) // 4})
        grid_rows = {r for r in tick_rows if r < height - 1}

        for row in range(height):
            level = (height - 1 - row) / (height - 1) if height > 1 else 1
            if row in tick_rows or height <= 5:
                tick_str = fmt.format(maximum * level / divisor)
                put(y + row, axis_left, f"{tick_str:>{axis_w}} │", curses.A_DIM)
            else:
                put(y + row, axis_left + axis_w, " │", curses.A_DIM)

        # Build sparse plot canvas so gridlines never overwrite active data cells.
        occupied = {}
        if line:
            grid = braille_line_grid([b["rx_rate"] for b in buckets],
                                     [b["tx_rate"] for b in buckets],
                                     cell_width, height, maximum)
            for (row, column), (char, series) in grid.items():
                attr = self._attr(series) | (curses.A_BOLD if series == 1 else 0)
                occupied[(row, column)] = (char, attr)
        else:
            for index, bucket in enumerate(buckets):
                center = index * cell_width + cell_width // 2
                if cell_width >= 5:
                    bar_w = min(3, (cell_width - 2) // 2)
                    xs = (center - bar_w, center + 1)
                elif cell_width >= 3:
                    bar_w = 1
                    xs = (center - 1, center)
                else:
                    bar_w = 1
                    xs = (index * cell_width, index * cell_width + 1)
                for series, value in enumerate((bucket["rx"], bucket["tx"]), 1):
                    attr = self._attr(series) | (curses.A_BOLD if series == 1 else 0)
                    for row, ch in bar_cells(value, maximum, height).items():
                        for dx in range(bar_w):
                            occupied[(row, xs[series - 1] + dx)] = (ch, attr)

        for row in range(height):
            if row in grid_rows:
                col = 0
                while col < plot_width:
                    if (row, col) in occupied:
                        col += 1
                        continue
                    end = col
                    while end < plot_width and (row, end) not in occupied:
                        end += 1
                    put(y + row, plot_x + col, "┄" * (end - col), curses.A_DIM)
                    col = end
            for col in range(plot_width):
                cell = occupied.get((row, col))
                if cell:
                    put(y + row, plot_x + col, cell[0], cell[1])

        # Baseline with subtle recorded / missing / future markers (no noisy '?' spam).
        put(y + height, plot_x - 1, "└", curses.A_DIM)
        selected_index = self.selected - page_start
        for index, bucket in enumerate(buckets):
            bx = plot_x + index * cell_width
            center = bx + cell_width // 2
            if bucket["future"]:
                if cell_width >= 2:
                    put(y + height, center, "·", curses.A_DIM)
            elif bucket["rx"] is None:
                put(y + height, bx, "┄" * cell_width, curses.A_DIM)
            else:
                put(y + height, bx, "─" * cell_width, curses.A_DIM)
            if page_start + index == self.selected:
                put(y + height, center, "▲", curses.A_BOLD | self._attr(1))

        labels = [self._bucket_axis_label(b, cell_width) for b in buckets]
        max_lab_w = max(self.text_width(lab) for lab in labels)
        stride = max(1, math.ceil((max_lab_w + 1) / cell_width))

        def label_x(index, lab):
            center = plot_x + index * cell_width + cell_width // 2
            lw = self.text_width(lab)
            return max(plot_x, min(w - lw - 1, center - lw // 2))

        selected_label = labels[selected_index]
        selected_x = label_x(selected_index, selected_label)
        selected_w = self.text_width(selected_label)
        for index, lab in enumerate(labels):
            selected = page_start + index == self.selected
            x = label_x(index, lab)
            lw = self.text_width(lab)
            overlaps = x < selected_x + selected_w + 1 and x + lw + 1 > selected_x
            if selected or (index % stride == 0 and not overlaps):
                put(y + height + 1, x, lab,
                    (curses.A_REVERSE | curses.A_BOLD) if selected else curses.A_DIM)
        return y + height + 2 + gap

    def _draw_inspector_card(self, y, left, width, current, total_bytes, total_count):
        volume = lambda v: self.volume(v) if v is not None else "—"
        compact = width < 98
        fmt_v = self._compact_vol if compact else volume
        if current["rx"] is not None:
            tot = current["rx"] + current["tx"]
            share = f"{tot / total_bytes * 100:.0f}%" if (compact and total_bytes) else (
                f"{tot / total_bytes * 100:.1f}%" if total_bytes else "—")
            rx_txt = f"{fmt_v(current['rx'])}({self._compact_rate(current['rx_rate'])})" if compact else (
                f"{fmt_v(current['rx'])} ({self._compact_rate(current['rx_rate'])})")
            tx_txt = f"{fmt_v(current['tx'])}({self._compact_rate(current['tx_rate'])})" if compact else (
                f"{fmt_v(current['tx'])} ({self._compact_rate(current['tx_rate'])})")
            tot_txt = f"{fmt_v(tot)}({share})" if compact else f"{fmt_v(tot)} ({share})"
            dur_txt = f"{current['seconds'] / 60:.0f}分"
        else:
            state = "未发生" if current["future"] else "无记录"
            rx_txt = tx_txt = tot_txt = state
            dur_txt = "0分"
        title = f"选中 {current['title']} [{self.selected + 1}/{total_count}]"
        hint = "[Enter]下钻 [t]明细表"
        self._draw_box_top(y, left, width, title, right_badge=hint)
        if compact:
            items = [
                [("↓", self._attr(1) | curses.A_BOLD), (rx_txt, self._attr(1))],
                [("↑", self._attr(2) | curses.A_BOLD), (tx_txt, self._attr(2))],
                [("合", curses.A_DIM), (tot_txt, curses.A_BOLD)],
                [(dur_txt, curses.A_DIM)],
            ]
        else:
            items = [
                [("↓ ", self._attr(1) | curses.A_BOLD), ("下载 ", curses.A_DIM), (rx_txt, self._attr(1))],
                [("↑ ", self._attr(2) | curses.A_BOLD), ("上传 ", curses.A_DIM), (tx_txt, self._attr(2))],
                [("合计 ", curses.A_DIM), (tot_txt, curses.A_BOLD)],
                [("时段均速 ", curses.A_DIM), (dur_txt, 0)],
            ]
        self._draw_box_row(y + 1, left, width, items)
        self._draw_box_bottom(y + 2, left, width)

    def _share_bar(self, value, max_bucket, bar_len=8):
        if not value or not max_bucket:
            return "░" * bar_len
        filled = max(1, min(bar_len, round(value / max_bucket * bar_len)))
        return "█" * filled + "░" * (bar_len - filled)

    def _draw_table_panel(self, y, left, width, height, report, mini=False):
        if height < 4 or width < 36:
            return
        buckets = report["buckets"]
        total_count = len(buckets)
        summary = report["summary"]
        total_bytes = (summary["rx"] or 0) + (summary["tx"] or 0)
        max_bucket = max(((b["rx"] or 0) + (b["tx"] or 0) for b in buckets), default=0)
        visible_rows = max(1, height - 3)
        start = max(0, min(self.selected - visible_rows // 2, total_count - visible_rows))
        end = min(total_count, start + visible_rows)
        window = buckets[start:end]

        if mini:
            title = f"时段明细速览 · 选中 {buckets[self.selected]['title']} [{self.selected + 1}/{total_count}]"
            badge = "[Enter]下钻  [t]完整明细表"
        else:
            title = f"时段明细表 · 第 {start + 1}-{end}/{total_count} 项"
            badge = "[↑↓/←→]选行  [Enter]下钻  [t]返回图表"
        self._draw_box_top(y, left, width, title, right_badge=badge)

        inner = width - 4  # inside "│ " and " │"
        wide_rates = inner >= 86
        show_dur = inner >= 72
        show_bar = inner >= 60

        def rjust(text, col_w):
            pad = max(0, col_w - self.text_width(text))
            return " " * pad + self.clip(text, col_w)

        def ljust(text, col_w):
            return self.pad(self.clip(text, col_w), col_w)

        slot_w = 10 if self.mode in ("month", "week") else 6
        vol_w = 9 if inner >= 76 else 8
        rate_w = 12 if wide_rates else 16
        bar_w = 13 if show_bar else 0
        dur_w = 6 if show_dur else 0

        # Column Header
        hdr_cols = [(ljust("时段", slot_w + 2), curses.A_DIM)]
        hdr_cols.append((rjust("下载", vol_w), self._attr(1) | curses.A_BOLD))
        hdr_cols.append((rjust("上传", vol_w), self._attr(2) | curses.A_BOLD))
        hdr_cols.append((rjust("合计", vol_w), curses.A_BOLD))
        if show_bar:
            hdr_cols.append((ljust("流量占比", bar_w), curses.A_DIM))
        if wide_rates:
            hdr_cols.append((rjust("下载均速", rate_w), curses.A_DIM))
            hdr_cols.append((rjust("上传均速", rate_w), curses.A_DIM))
        elif inner >= 52:
            hdr_cols.append((rjust("时段均速(↓/↑)", rate_w), curses.A_DIM))
        if show_dur:
            hdr_cols.append((rjust("时长", dur_w), curses.A_DIM))
        self._draw_box_row(y + 1, left, width, [[c] for c in hdr_cols])

        vol = lambda v: self.volume(v) if v is not None else "—"
        for idx, bucket in enumerate(window):
            abs_idx = start + idx
            selected = abs_idx == self.selected
            row_y = y + 2 + idx
            prefix = "▸ " if selected else "  "
            if self.mode == "week":
                day = datetime.fromtimestamp(bucket["start"]).date()
                slot_txt = f"{day.strftime('%m-%d')} {WEEKDAYS[day.weekday()]}"
            elif self.mode == "month":
                slot_txt = bucket["title"]
            else:
                slot_txt = bucket["label"]

            if bucket["rx"] is not None:
                tot = bucket["rx"] + bucket["tx"]
                share_pct = f"{tot / total_bytes * 100:4.1f}%" if total_bytes else "   — "
                bar_txt = f"{self._share_bar(tot, max_bucket, 7)} {share_pct}"
                rx_s, tx_s, tot_s = vol(bucket["rx"]), vol(bucket["tx"]), vol(tot)
                rx_r = format_rate(bucket["rx_rate"], self.bits) if wide_rates else self._compact_rate(bucket["rx_rate"])
                tx_r = format_rate(bucket["tx_rate"], self.bits) if wide_rates else self._compact_rate(bucket["tx_rate"])
                dur_s = f"{bucket['seconds'] / 60:.0f}分"
                dim = False
            else:
                rx_s = tx_s = tot_s = "·" if bucket["future"] else "—"
                bar_txt = "未发生" if bucket["future"] else "无记录"
                rx_r = tx_r = dur_s = "—"
                dim = True

            base_attr = curses.A_REVERSE if selected else (curses.A_DIM if dim else 0)
            row_cols = [[(prefix + ljust(slot_txt, slot_w),
                          (curses.A_REVERSE | curses.A_BOLD) if selected else base_attr)]]
            row_cols.append([(rjust(rx_s, vol_w),
                              base_attr if (selected or dim) else self._attr(1))])
            row_cols.append([(rjust(tx_s, vol_w),
                              base_attr if (selected or dim) else self._attr(2))])
            row_cols.append([(rjust(tot_s, vol_w),
                              base_attr if (selected or dim) else curses.A_BOLD)])
            if show_bar:
                row_cols.append([(ljust(bar_txt, bar_w),
                                  base_attr if (selected or dim) else self._attr(1))])
            if wide_rates:
                row_cols.append([(rjust(rx_r, rate_w),
                                  base_attr if (selected or dim) else self._attr(1))])
                row_cols.append([(rjust(tx_r, rate_w),
                                  base_attr if (selected or dim) else self._attr(2))])
            elif inner >= 52:
                pair = "—" if dim else f"{rx_r}/{tx_r}"
                row_cols.append([(rjust(pair, rate_w), base_attr)])
            if show_dur:
                row_cols.append([(rjust(dur_s, dur_w), base_attr if selected else curses.A_DIM)])
            self._draw_box_row(row_y, left, width, row_cols)

        self._draw_box_bottom(y + 2 + len(window), left, width)

    def draw(self, h, w):
        self.size = (h, w)
        if self.report is None:
            self.reload()
            self.select_anchor()
        app, put, report = self.app, self._put, self.report
        card_mode = h >= 22 and w >= 76
        left = 2 if w >= 60 else 1
        card_w = max(10, w - left * 2)
        live = app.live_rates
        summary = report["summary"]
        volume = lambda v: self.volume(v) if v is not None else "暂无数据"
        resolution = RESOLUTION_NAMES.get(report["peak_resolution"], "无记录")
        total = summary["rx"] + summary["tx"] if summary["rx"] is not None else None

        if card_mode:
            mode_names = {
                "month": "月视图",
                "week": "周（周一至周日）视图" if w >= 90 else "周视图",
                "day": "日视图（每5分钟）" if self.fine else "日视图",
            }
            period_badge = f"{mode_names[self.mode]} · {report['start']} ~ {report['end'] - timedelta(days=1)}"
            self._draw_box_top(0, left, card_w, f"datameter · 历史流量 · {app.cfg['interface']}",
                               right_badge=period_badge)
            peak_lbl = f"峰值/{resolution} " if card_w >= 96 else "峰 "
            if card_w < 88:
                short_v = lambda v: self._compact_vol(v).replace("iB", "")
                short_r = lambda v: self._compact_rate(v).replace("/s", "")
                kpi_items = [
                    [("合计 ", curses.A_DIM), (short_v(total), curses.A_BOLD),
                     ("(↓", curses.A_DIM), (short_v(summary["rx"]), self._attr(1)),
                     (" ↑", curses.A_DIM), (short_v(summary["tx"]), self._attr(2)), (")", curses.A_DIM)],
                    [("均速 ", curses.A_DIM), ("↓" + short_r(summary["rx_rate"]), self._attr(1)),
                     (" ↑" + short_r(summary["tx_rate"]), self._attr(2))],
                    [(peak_lbl, curses.A_DIM),
                     ("↓" + short_r(report["peak"]["rx"]), self._attr(1)),
                     (" ↑" + short_r(report["peak"]["tx"]), self._attr(2))],
                    [(f"{report['coverage']:.0%}", curses.A_BOLD)],
                ]
            else:
                kpi_items = [
                    [("合计 ", curses.A_DIM), (self._compact_vol(total), curses.A_BOLD),
                     (" (↓", curses.A_DIM), (self._compact_vol(summary["rx"]), self._attr(1)),
                     (" ↑", curses.A_DIM), (self._compact_vol(summary["tx"]), self._attr(2)), (")", curses.A_DIM)],
                    [("均速 ", curses.A_DIM), ("↓" + self._compact_rate(summary["rx_rate"]), self._attr(1)),
                     (" ↑" + self._compact_rate(summary["tx_rate"]), self._attr(2))],
                    [(peak_lbl, curses.A_DIM),
                     ("↓" + self._compact_rate(report["peak"]["rx"]), self._attr(1)),
                     (" ↑" + self._compact_rate(report["peak"]["tx"]), self._attr(2))],
                    [("覆盖 ", curses.A_DIM), (f"{report['coverage']:.0%}", curses.A_BOLD)],
                ]
            self._draw_box_row(1, left, card_w, kpi_items)
            expanded_card = h >= 28 and w >= 80
            if expanded_card:
                live_spans = [("实时 ", curses.A_DIM)]
                if live.rx is None and live.tx is None:
                    live_spans.append(("—", curses.A_DIM))
                else:
                    live_spans.extend([("↓" + format_rate(live.rx, self.bits), self._attr(1)),
                                       (" ↑" + format_rate(live.tx, self.bits), self._attr(2))])
                sess_spans = [("会话峰 ", curses.A_DIM)]
                if live.peak_rx is None and live.peak_tx is None:
                    sess_spans.append(("—", curses.A_DIM))
                else:
                    sess_spans.extend([("↓" + self._compact_rate(live.peak_rx), self._attr(1)),
                                       (" ↑" + self._compact_rate(live.peak_tx), self._attr(2))])
                self._draw_box_row(2, left, card_w, [
                    live_spans,
                    sess_spans,
                    [("峰值采样 ", curses.A_DIM), (f"{report['peak_coverage']:.0%}", 0)],
                    [("─有记录 ┄缺失 ·未发生", curses.A_DIM)],
                ])
                self._draw_box_bottom(3, left, card_w)
                chart_start = 4
            else:
                self._draw_box_bottom(2, left, card_w)
                chart_start = 3
        else:
            chart_start = 5 if h >= 18 else 4 if h >= 14 else 3 if h >= 11 else 2
            rate = lambda v: format_rate(v, self.bits).replace("暂无数据", "—").replace(" ", "")
            vol = lambda v: volume(v).replace("暂无数据", "—").replace(" ", "")
            summaries = [
                f"均速 ↓{rate(summary['rx_rate'])} ↑{rate(summary['tx_rate'])}",
                f"峰/{resolution} ↓{rate(report['peak']['rx'])} ↑{rate(report['peak']['tx'])}",
                f"实时 ↓{rate(live.rx)} ↑{rate(live.tx)}",
                f"会话峰 ↓{rate(live.peak_rx)} ↑{rate(live.peak_tx)}",
                f"流量覆盖{report['coverage']:.0%} 峰值覆盖{report['peak_coverage']:.0%} ─记录 ┄缺失",
                f"合计 {vol(total)} · ×曲线重合 · ·尚未发生",
            ]
            names = {"month": "月", "week": "周", "day": "日"}
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

        has_status = self.input_active or app.message or self.warning or app.error or summary["rx"] is None
        status_y = h - 3 if has_status else h - 2
        dual = False

        if self.show_table and status_y - chart_start >= 5 and w >= 36:
            table_h = status_y - chart_start
            self._draw_table_panel(chart_start, left, card_w, table_h, report, mini=False)
        else:
            # Compute shared X-axis alignment across visible charts.
            _, _, _, _, capacity = self._plot_layout(w, len(report["buckets"]), 5)
            start = self.selected // capacity * capacity
            buckets = report["buckets"][start:start + capacity]
            total_pages = math.ceil(len(report["buckets"]) / capacity)
            page_num = start // capacity + 1
            page_info = f" · 第{page_num}/{total_pages}页" if total_pages > 1 else ""

            gap = 1 if (h >= 26 and w >= 76) else 0
            if card_mode:
                mini_table = h >= 34 and w >= 80
                bottom_reserve = 6 if mini_table else 3
            else:
                mini_table = False
                bottom_reserve = 2 if h >= 18 else 1

            overheads = [self._chart_spec(w, buckets, line, page_info=page_info)[-1] + 2 + gap for line in (False, True)]
            plot_budget = status_y - chart_start - bottom_reserve - sum(overheads)
            dual = plot_budget >= 4

            bar_spec = self._chart_spec(w, buckets, False, page_info=page_info)
            line_spec = self._chart_spec(w, buckets, True, page_info=page_info)
            shared_tick_w = max(bar_spec[3], line_spec[3]) if dual else (line_spec[3] if self.chart_line else bar_spec[3])
            shared_layout = self._plot_layout(w, len(buckets), shared_tick_w)

            if dual:
                bar_height = min(9, plot_budget // 2)
                line_height = min(9, plot_budget - bar_height)
                y = self.draw_chart(chart_start, w, bar_height, buckets,
                                    page_start=start, gap=gap, layout=shared_layout, page_info=page_info)
                y = self.draw_chart(y, w, line_height, buckets, line=True,
                                    page_start=start, gap=gap, layout=shared_layout, page_info=page_info)
            else:
                overhead = overheads[int(self.chart_line)]
                if status_y - chart_start - bottom_reserve - overhead < 1:
                    bottom_reserve = 0
                height = max(1, status_y - chart_start - bottom_reserve - overhead)
                y = self.draw_chart(chart_start, w, height, buckets, line=self.chart_line,
                                    page_start=start, gap=gap, layout=shared_layout, page_info=page_info)

            current = report["buckets"][self.selected]
            if card_mode and bottom_reserve:
                total_bytes = (summary["rx"] or 0) + (summary["tx"] or 0)
                if mini_table:
                    avail_rows = max(6, status_y - y)
                    self._draw_table_panel(y, left, card_w, min(8, avail_rows), report, mini=True)
                else:
                    self._draw_inspector_card(y, left, card_w, current, total_bytes, len(report["buckets"]))
            elif bottom_reserve:
                label = current["title"] if w >= 60 else current["label"]
                selected = f"{label} [{self.selected + 1}/{len(report['buckets'])}] ↓{volume(current['rx'])} ↑{volume(current['tx'])}"
                put(y, left, selected)
                if bottom_reserve >= 2:
                    put(y + 1, left, f"时段均速：↓ {format_rate(current['rx_rate'], self.bits)}  ↑ {format_rate(current['tx_rate'], self.bits)}"
                        + (f"  已记录 {current['seconds'] / 60:.0f} 分钟" if w >= 80 else ""), curses.A_DIM)

        if self.input_active:
            put(status_y, left, f"跳转日期：{self.input_buffer}█  Enter确认/Esc取消", self._attr(1))
        elif app.message or self.warning or app.error:
            put(status_y, left, app.message or self.warning or f"vnstat 更新失败：{app.error}", self._attr(3))
        elif summary["rx"] is None:
            put(status_y, left, "此范围没有记录；已清理的明细无法恢复。", self._attr(3))

        if w >= 80:
            footer = " m/w/d月周日 [ ]前后 /日期 n今天 u单位 t明细表 r刷新 Esc返回 q退出 "
            table_hint = "t返回图表" if self.show_table else "t明细表"
            hints = f" ←/→选点 Enter下钻 f 5分钟 {table_hint}" + (" i详情" if not card_mode else "") + (" Tab切图" if not dual and not self.show_table else "")
        elif w >= 48:
            footer = " m/w/d范围 [ ]前后 /日期 t明细 Esc返回 q退出 "
            hints = " ←/→选点 Enter下钻 f 5分钟 i详情" + (" Tab切图" if not dual else "")
        else:
            footer = "m/w/d [] /日期 Esc q退出"
            hints = "←→选点 " + ("Tab切图 " if not dual else "") + "i详情 Enter下钻"
        put(h - 2, 0, hints, curses.A_DIM)
        put(h - 1, 0, self.pad(footer, w - 1), curses.A_REVERSE)
