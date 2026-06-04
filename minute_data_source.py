from __future__ import annotations

import calendar
import csv
import gzip
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

# Reuse stdlib loader primitives from the daily layer (do NOT redefine these).
from data_source import (
    _optional_float,
    code_to_symbol,  # noqa: F401  (re-exported for convenience)
    jquants_cache_dir,
    normalize_code,
    parse_date,
)


MINUTE_FILE_GLOB = "equities_bars_minute_*.csv.gz"
# Minute files are named with a YYYYMM (monthly) or YYYYMMDD (per-day fragment)
# token; both regimes coexist on S:/j-quants. The token lets us skip files that
# cannot overlap the requested [start, end] window (Phase 0a follow-up #1: the
# loader previously scanned all ~25 months for a single-day request).
_MINUTE_TOKEN_RE = re.compile(r"equities_bars_minute_(\d{6}|\d{8})\.csv\.gz$")

# Session anchors (TSE). Morning session opens 09:00 and ends 11:30; the
# afternoon session opens 12:30. The afternoon close is deliberately NOT
# hard-coded: TSE extended it from 15:00 to 15:30 on 2024-11-05, and binning
# whatever bars exist past 12:30 handles both regimes transparently.
_MORNING_ANCHOR = (9, 0)
_MORNING_END = (11, 30)
_AFTERNOON_ANCHOR = (12, 30)
_BIN = timedelta(minutes=15)


@dataclass(frozen=True)
class MinuteBar:
    timestamp: datetime
    code: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    value: float | None = None


def iter_minute_bar_files(cache_dir: str | Path | None = None) -> list[Path]:
    root = jquants_cache_dir(cache_dir)
    return sorted(root.glob(MINUTE_FILE_GLOB))


def _file_covers_range(
    path: Path, start_date: date | None, end_date: date | None
) -> bool:
    """Whether a minute file's date token can overlap ``[start_date, end_date]``.

    A 6-digit token covers a whole calendar month; an 8-digit token a single
    day. Files whose name does not match the expected pattern are kept (never
    silently dropped). ``None`` bounds are open-ended.
    """
    m = _MINUTE_TOKEN_RE.search(path.name)
    if m is None:
        return True
    token = m.group(1)
    if len(token) == 6:
        year, month = int(token[:4]), int(token[4:6])
        first = date(year, month, 1)
        last = date(year, month, calendar.monthrange(year, month)[1])
    else:  # YYYYMMDD
        day = date(int(token[:4]), int(token[4:6]), int(token[6:8]))
        first = last = day
    if start_date is not None and last < start_date:
        return False
    if end_date is not None and first > end_date:
        return False
    return True


def load_minute_bars(
    *,
    cache_dir: str | Path | None = None,
    start: str | datetime | None = None,
    end: str | datetime | None = None,
    codes: Iterable[str] | None = None,
) -> dict[str, list[MinuteBar]]:
    """Load tier-2 minute bars from CSV.gz without pandas or Nautilus.

    Return shape is {normalized_code: [MinuteBar, ...]}. ``Date`` (%Y-%m-%d)
    and ``Time`` (%H:%M) are combined into a naive datetime. Rows are filtered
    point-in-time by *date* (inclusive) and de-duplicated on (code, timestamp),
    keeping the FIRST occurrence. This protects against the documented dual
    monthly + per-day fragment overlap on S:/j-quants. Each code's bars are
    returned sorted ascending by timestamp.
    """
    start_date = parse_date(start) if start is not None else None
    end_date = parse_date(end) if end is not None else None
    # parse_date returns datetime unchanged (datetime is a date subclass); the
    # row/file comparisons below are date-level, so normalize to date.
    if isinstance(start_date, datetime):
        start_date = start_date.date()
    if isinstance(end_date, datetime):
        end_date = end_date.date()
    code_filter = {normalize_code(c) for c in codes} if codes is not None else None

    seen: set[tuple[str, datetime]] = set()
    bars_by_code: dict[str, list[MinuteBar]] = {}

    for path in iter_minute_bar_files(cache_dir):
        # Skip files whose date token cannot overlap the window (follow-up #1).
        if not _file_covers_range(path, start_date, end_date):
            continue
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                row_date = parse_date(row["Date"])
                if start_date is not None and row_date < start_date:
                    continue
                if end_date is not None and row_date > end_date:
                    continue

                code = normalize_code(row["Code"])
                if code_filter is not None and code not in code_filter:
                    continue

                timestamp = datetime.strptime(
                    f"{row['Date']} {row['Time']}", "%Y-%m-%d %H:%M"
                )

                key = (code, timestamp)
                if key in seen:
                    continue
                seen.add(key)

                bar = MinuteBar(
                    timestamp=timestamp,
                    code=code,
                    open=float(row["O"]),
                    high=float(row["H"]),
                    low=float(row["L"]),
                    close=float(row["C"]),
                    volume=int(float(row["Vo"])),
                    value=_optional_float(row.get("Va")),
                )
                bars_by_code.setdefault(code, []).append(bar)

    for bars in bars_by_code.values():
        bars.sort(key=lambda b: b.timestamp)
    return bars_by_code


def _bin_start(ts: datetime) -> datetime | None:
    """Return the 15-min bin-start for ``ts`` anchored on the session boundary.

    Morning bars (09:00 <= T < 11:30) anchor on 09:00; afternoon bars
    (T >= 12:30) anchor on 12:30. Because the two sessions use *separate*
    anchors, the 11:15 bin and the 12:30 bin never merge. Bars that fall in the
    11:30-12:30 lunch gap (should not exist) return None and are dropped.
    """
    day = ts.date()
    morning_anchor = datetime(day.year, day.month, day.day, *_MORNING_ANCHOR)
    morning_end = datetime(day.year, day.month, day.day, *_MORNING_END)
    afternoon_anchor = datetime(day.year, day.month, day.day, *_AFTERNOON_ANCHOR)

    if morning_anchor <= ts < morning_end:
        anchor = morning_anchor
    elif ts >= afternoon_anchor:
        anchor = afternoon_anchor
    else:
        return None

    steps = (ts - anchor) // _BIN
    return anchor + steps * _BIN


def resample_15min(
    bars_by_code: dict[str, list[MinuteBar]],
) -> dict[str, list[MinuteBar]]:
    """Aggregate 1-minute bars into session-anchored 15-minute bars.

    Aggregation per bin: open = first bar's open, high = max(high),
    low = min(low), close = last bar's close, volume = sum, value = sum
    (None if every contributing bar's value is None). The resulting
    MinuteBar.timestamp is the bin-start time. Empty bins are NOT synthesized;
    only bins backed by real bars are emitted.
    """
    out: dict[str, list[MinuteBar]] = {}
    for code, bars in bars_by_code.items():
        bins: dict[datetime, list[MinuteBar]] = {}
        for bar in sorted(bars, key=lambda b: b.timestamp):
            bin_ts = _bin_start(bar.timestamp)
            if bin_ts is None:
                continue
            bins.setdefault(bin_ts, []).append(bar)

        aggregated: list[MinuteBar] = []
        for bin_ts in sorted(bins):
            group = bins[bin_ts]  # already in ascending timestamp order
            values = [b.value for b in group if b.value is not None]
            aggregated.append(
                MinuteBar(
                    timestamp=bin_ts,
                    code=code,
                    open=group[0].open,
                    high=max(b.high for b in group),
                    low=min(b.low for b in group),
                    close=group[-1].close,
                    volume=sum(b.volume for b in group),
                    value=sum(values) if values else None,
                )
            )
        out[code] = aggregated
    return out
