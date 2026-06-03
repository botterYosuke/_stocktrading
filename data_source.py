from __future__ import annotations

import csv
import gzip
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable


DAILY_FILE_GLOB = "equities_bars_daily_*.csv.gz"


@dataclass(frozen=True)
class DailyBar:
    date: date
    code: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    value: float | None = None
    adj_factor: float | None = None


def jquants_cache_dir(explicit: str | Path | None = None) -> Path:
    """Return the tier-2 CSV.gz directory used by the prediction brain."""
    if explicit is not None:
        return Path(explicit)
    env_value = os.environ.get("DEV_J_QUANTS_CACHE")
    if not env_value:
        raise ValueError("DEV_J_QUANTS_CACHE is not set")
    return Path(env_value)


def normalize_code(raw_code: object) -> str:
    """Normalize J-Quants 5 digit codes like 72030 to stocktrading's 7203."""
    text = str(raw_code).strip()
    if text.endswith(".0"):
        text = text[:-2]
    if len(text) == 5 and text.endswith("0"):
        text = text[:4]
    return text


def code_to_symbol(raw_code: object, market: str = "TSE") -> str:
    return f"{normalize_code(raw_code)}.{market}"


def parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(value, "%Y-%m-%d").date()


def iter_daily_bar_files(cache_dir: str | Path | None = None) -> list[Path]:
    root = jquants_cache_dir(cache_dir)
    return sorted(root.glob(DAILY_FILE_GLOB))


def load_daily_bars(
    *,
    cache_dir: str | Path | None = None,
    start: str | date | None = None,
    end: str | date | None = None,
    codes: Iterable[str] | None = None,
) -> dict[str, list[DailyBar]]:
    """Load tier-2 daily bars from CSV.gz without pandas or Nautilus.

    The return shape is {normalized_code: [DailyBar, ...]}. Rows are filtered
    point-in-time by date and de-duplicated on (code, date), keeping the first
    occurrence. This protects against mixed monthly + per-day fragments.
    """
    start_date = parse_date(start) if start is not None else None
    end_date = parse_date(end) if end is not None else None
    code_filter = {normalize_code(c) for c in codes} if codes is not None else None

    seen: set[tuple[str, date]] = set()
    bars_by_code: dict[str, list[DailyBar]] = {}

    for path in iter_daily_bar_files(cache_dir):
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

                key = (code, row_date)
                if key in seen:
                    continue
                seen.add(key)

                bar = DailyBar(
                    date=row_date,
                    code=code,
                    open=float(row["O"]),
                    high=float(row["H"]),
                    low=float(row["L"]),
                    close=float(row["C"]),
                    volume=int(float(row["Vo"])),
                    value=_optional_float(row.get("Va")),
                    adj_factor=_optional_float(row.get("AdjFactor")),
                )
                bars_by_code.setdefault(code, []).append(bar)

    for bars in bars_by_code.values():
        bars.sort(key=lambda b: b.date)
    return bars_by_code


def newest_close_as_of(
    bars_by_code: dict[str, list[DailyBar]],
    code: str,
    as_of: str | date,
) -> float:
    """Return the latest close at or before as_of for the price-band filter."""
    normalized = normalize_code(code)
    cutoff = parse_date(as_of)
    candidates = [bar for bar in bars_by_code.get(normalized, []) if bar.date <= cutoff]
    if not candidates:
        raise KeyError(f"no daily bars for {normalized} at or before {cutoff}")
    return candidates[-1].close


def _optional_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value)
