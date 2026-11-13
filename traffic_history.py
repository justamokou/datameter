"""Calendar reports and a local vnstat archive, using only the standard library.

rx is download, tx is upload. Rates are bytes / observed wall-clock seconds;
historical maxima are maxima of interval averages, never instantaneous peaks.
"""
from __future__ import annotations

import calendar
from bisect import bisect_left
from datetime import date, datetime, time as day_time, timedelta
from pathlib import Path
import sqlite3
import time

RESOLUTIONS = {"fiveminute": 300, "hour": 3600, "day": 86400, "month": 2678400}
RESOLUTION_NAMES = {"fiveminute": "5分钟", "hour": "小时", "day": "日", "month": "月"}


def midnight(day: date) -> float:
    return datetime.combine(day, day_time()).timestamp()


def shift_month(day: date, delta: int) -> date:
    number = day.year * 12 + day.month - 1 + delta
    year, month = divmod(number, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def period_bounds(mode: str, anchor: date) -> tuple[date, date]:
    if mode == "month":
        start = anchor.replace(day=1)
        return start, shift_month(start, 1)
    if mode == "week":
        start = anchor - timedelta(days=anchor.weekday())
        return start, start + timedelta(days=7)
    if mode == "day":
        return anchor, anchor + timedelta(days=1)
    raise ValueError("未知统计范围")


def parse_anchor(value: str, mode: str) -> date:
    value = value.strip()
    if mode == "month" and len(value) == 7:
        return datetime.strptime(value, "%Y-%m").date()
    if mode == "week" and len(value) == 8 and value[4:6].upper() == "-W":
        return date.fromisocalendar(int(value[:4]), int(value[6:]), 1)
    return date.fromisoformat(value)


def _timestamp(entry: dict) -> float:
    if "timestamp" in entry:
        return float(entry["timestamp"])
    d, t = entry["date"], entry.get("time", {})
    return datetime(d["year"], d["month"], d.get("day", 1),
                    t.get("hour", 0), t.get("minute", 0)).timestamp()


def normalize_snapshot(traffic: dict, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    updated = traffic.get("_updated", {}).get("timestamp", now)
    asof = min(float(updated), now)
    created = float(traffic.get("_created", {}).get("timestamp", 0))
    result = []
    for resolution in RESOLUTIONS:
        for entry in traffic.get(resolution, []):
            stamp = _timestamp(entry)
            local = datetime.fromtimestamp(stamp)
            if resolution == "day":
                end = midnight(local.date() + timedelta(days=1))
            elif resolution == "month":
                end = midnight(shift_month(local.date(), 1))
            else:
                end = stamp + RESOLUTIONS[resolution]
            begin, stop = max(stamp, created), min(end, asof)
            if stop <= begin:
                continue
            result.append({"resolution": resolution, "stamp": stamp, "end": end,
                           "begin": begin, "stop": stop,
                           "rx": max(0, int(entry.get("rx", 0))),
                           "tx": max(0, int(entry.get("tx", 0)))})
    return result


class HistoryStore:
    """Keep snapshots by interface and interval; current intervals are upserted."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("""CREATE TABLE IF NOT EXISTS samples (
                interface TEXT NOT NULL, resolution TEXT NOT NULL,
                stamp REAL NOT NULL, end REAL NOT NULL, begin REAL NOT NULL,
                stop REAL NOT NULL, rx INTEGER NOT NULL, tx INTEGER NOT NULL,
                PRIMARY KEY (interface, resolution, stamp))""")
        except Exception:
            connection.close()
            raise
        return connection

    def ingest(self, interface: str, traffic: dict, now: float | None = None) -> None:
        samples = normalize_snapshot(traffic, now)
        connection = self._connect()
        try:
            with connection:
                connection.executemany("""INSERT INTO samples
                    (interface, resolution, stamp, end, begin, stop, rx, tx)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(interface, resolution, stamp) DO UPDATE SET
                    end=excluded.end, begin=excluded.begin, stop=excluded.stop,
                    rx=excluded.rx, tx=excluded.tx
                    WHERE excluded.stop >= samples.stop AND
                    (excluded.stop != samples.stop OR excluded.rx != samples.rx OR
                     excluded.tx != samples.tx)""",
                    [(interface, s["resolution"], s["stamp"], s["end"], s["begin"],
                      s["stop"], s["rx"], s["tx"]) for s in samples])
        finally:
            connection.close()

    def read(self, interface: str, start: float, end: float) -> list[dict]:
        if not self.path.exists():
            return []
        connection = self._connect()
        try:
            return [dict(row) for row in connection.execute(
                "SELECT resolution, stamp, end, begin, stop, rx, tx FROM samples "
                "WHERE interface=? AND stamp>=? AND stamp<? ORDER BY stamp",
                (interface, start, end))]
        finally:
            connection.close()


def _aggregate(samples: list[dict], start: float, end: float) -> dict:
    """Use coarse authoritative totals first, fill uncovered slots with detail.

    A day total and its hourly/5-minute children must never be added together.
    Missing intervals remain missing rather than being invented as zero traffic.
    """
    chosen, intervals, starts = [], [], []
    for sample in sorted(samples, key=lambda s: (-RESOLUTIONS[s["resolution"]], s["stamp"])):
        if sample["stamp"] < start or sample["end"] > end or sample["stop"] <= sample["begin"]:
            continue
        index = bisect_left(starts, sample["stamp"])
        if ((index and intervals[index - 1][1] > sample["stamp"]) or
                (index < len(intervals) and intervals[index][0] < sample["end"])):
            continue
        chosen.append(sample)
        starts.insert(index, sample["stamp"])
        intervals.insert(index, (sample["stamp"], sample["end"]))
    seconds = sum(s["stop"] - s["begin"] for s in chosen)
    rx, tx = sum(s["rx"] for s in chosen), sum(s["tx"] for s in chosen)
    return {"rx": rx if chosen else None, "tx": tx if chosen else None,
            "seconds": seconds, "rx_rate": rx / seconds if seconds else None,
            "tx_rate": tx / seconds if seconds else None,
            "sources": sorted({s["resolution"] for s in chosen}, key=RESOLUTIONS.get)}


def build_report(samples: list[dict], mode: str, anchor: date,
                 fine: bool = False, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    first, last = period_bounds(mode, anchor)
    start, end = midnight(first), midnight(last)
    samples = [s for s in samples if start <= s["stamp"] < end and s["end"] <= end]
    summary = _aggregate(samples, start, end)
    slots = []
    if mode == "day":
        step = 300 if fine else 3600
        cursor = start
        while cursor < end:
            stop = min(cursor + step, end)
            # Timestamps preserve both instances of an hour across DST fallback.
            local = datetime.fromtimestamp(cursor).astimezone()
            slots.append((cursor, stop, local.strftime("%H:%M"), local.strftime("%Y-%m-%d %H:%M %z")))
            cursor = stop
    else:
        day = first
        while day < last:
            slots.append((midnight(day), midnight(day + timedelta(days=1)),
                          day.strftime("%d"), day.isoformat()))
            day += timedelta(days=1)
    buckets = []
    for slot_start, slot_end, label, title in slots:
        bucket = _aggregate(samples, slot_start, slot_end)
        bucket.update(start=slot_start, end=slot_end, label=label, title=title,
                      future=slot_start >= now)
        buckets.append(bucket)

    # One fixed source resolution for each peak avoids mixing daily and 5-min
    # averages into an apparently comparable instantaneous maximum.
    peak_samples, peak_resolution = [], None
    for resolution in RESOLUTIONS:
        found = [s for s in samples if s["resolution"] == resolution]
        if found:
            peak_samples, peak_resolution = found, resolution
            break
    peaks = {}
    for direction in ("rx", "tx"):
        best = max(peak_samples, key=lambda s: s[direction] / (s["stop"] - s["begin"]), default=None)
        peaks[direction] = best[direction] / (best["stop"] - best["begin"]) if best else None
        peaks[direction + "_at"] = best["stamp"] if best else None
    elapsed = max(0, min(end, now) - start)
    peak_seconds = sum(s["stop"] - s["begin"] for s in peak_samples)
    return {"mode": mode, "anchor": anchor, "fine": fine, "start": first, "end": last,
            "buckets": buckets, "summary": summary, "peak": peaks,
            "peak_resolution": peak_resolution,
            "coverage": min(1, summary["seconds"] / elapsed) if elapsed else 0,
            "peak_coverage": min(1, peak_seconds / elapsed) if elapsed else 0}


def rate_unit(maximum: float, bits: bool = False) -> tuple[float, str]:
    base = 1000 if bits else 1024
    names = ("bit/s", "kbit/s", "Mbit/s", "Gbit/s", "Tbit/s") if bits else (
        "B/s", "KiB/s", "MiB/s", "GiB/s", "TiB/s")
    divisor = 1 / 8 if bits else 1
    for name in names[:-1]:
        if maximum / divisor < base:
            return divisor, name
        divisor *= base
    return divisor, names[-1]


def format_rate(value: float | None, bits: bool = False) -> str:
    if value is None:
        return "暂无数据"
    divisor, unit = rate_unit(value, bits)
    return f"{value / divisor:.1f} {unit}"


class LiveRates:
    """One-second sysfs counter deltas; session maxima reset on iface changes."""

    def __init__(self, root: Path = Path("/sys/class/net")):
        self.root, self.interface, self.previous = root, None, None
        self.rx = self.tx = self.peak_rx = self.peak_tx = None

    def poll(self, interface: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if interface != self.interface:
            self.interface, self.previous = interface, None
            self.rx = self.tx = self.peak_rx = self.peak_tx = None
        try:
            if Path(interface).name != interface or interface in (".", ".."):
                raise ValueError("无效网卡")
            folder = self.root / interface / "statistics"
            rx = int((folder / "rx_bytes").read_text())
            tx = int((folder / "tx_bytes").read_text())
        except (OSError, ValueError):
            self.previous = None
            self.rx = self.tx = None
            return
        if self.previous:
            then, old_rx, old_tx = self.previous
            duration = now - then
            if duration <= 0:
                return
            if rx >= old_rx and tx >= old_tx:
                self.rx, self.tx = (rx - old_rx) / duration, (tx - old_tx) / duration
                self.peak_rx = max(self.peak_rx or 0, self.rx)
                self.peak_tx = max(self.peak_tx or 0, self.tx)
            else:
                self.rx = self.tx = None
        self.previous = (now, rx, tx)
