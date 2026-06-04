from __future__ import annotations

import csv
import gzip
import statistics
from datetime import date
from pathlib import Path

from data_source import (
    DailyBar,
    _optional_float,
    iter_daily_bar_files,
    newest_close_as_of,
    normalize_code,
    parse_date,
)


def _load_daily_bars_pit(
    cache_dir: str | Path | None, cutoff: date
) -> dict[str, list[DailyBar]]:
    """Point-in-time daily-bar load that tolerates J-Quants no-trade rows.

    This mirrors ``data_source.load_daily_bars`` (same stdlib csv.gz pattern,
    same ``normalize_code`` + first-wins (code, date) de-dup, same PIT date
    filter) but additionally SKIPS rows whose OHLC fields are blank. Real
    J-Quants daily files contain ~3% no-trade rows (O/H/L/C/Vo/Va all empty)
    that ``data_source.load_daily_bars`` cannot parse (``float('')`` raises).
    The canonical loader is deliberately left untouched (it is on the LSTM
    path); the universe layer owns this tolerance.
    """
    seen: set[tuple[str, date]] = set()
    bars_by_code: dict[str, list[DailyBar]] = {}

    for path in iter_daily_bar_files(cache_dir):
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                row_date = parse_date(row["Date"])
                if row_date > cutoff:
                    continue

                close_raw = row.get("C")
                if close_raw is None or close_raw == "":
                    continue  # no-trade row: drop

                code = normalize_code(row["Code"])
                key = (code, row_date)
                if key in seen:
                    continue
                seen.add(key)

                bars_by_code.setdefault(code, []).append(
                    DailyBar(
                        date=row_date,
                        code=code,
                        open=float(row["O"]),
                        high=float(row["H"]),
                        low=float(row["L"]),
                        close=float(close_raw),
                        volume=int(float(row["Vo"])),
                        value=_optional_float(row.get("Va")),
                        adj_factor=_optional_float(row.get("AdjFactor")),
                    )
                )

    for bars in bars_by_code.values():
        bars.sort(key=lambda b: b.date)
    return bars_by_code


def select_universe(
    *,
    as_of: str | date,
    top_n: int = 100,
    price_band: tuple[float, float] = (700.0, 6000.0),
    va_window: int = 20,
    cache_dir: str | Path | None = None,
) -> list[str]:
    """Select a liquidity-ranked, price-banded, point-in-time universe.

    Ranking metric = median of the daily traded value (Va) over the most recent
    ``va_window`` business days at or before ``as_of`` (point-in-time: rows dated
    after ``as_of`` are ignored). Bars with a missing Va are excluded; codes with
    fewer than ``va_window`` valid Va observations are dropped. A price-band
    filter (price_band[0] < latest close < price_band[1], exclusive both sides)
    is then applied, and the survivors are ranked by Va median descending, with
    code ascending as a deterministic tie-break. Returns up to ``top_n``
    normalized codes.
    """
    cutoff = parse_date(as_of)
    bars_by_code = _load_daily_bars_pit(cache_dir, cutoff)
    lo, hi = price_band

    ranked: list[tuple[float, str]] = []
    for code, bars in bars_by_code.items():
        pit = [bar for bar in bars if bar.date <= cutoff]
        vas = [bar.value for bar in pit if bar.value is not None]
        if len(vas) < va_window:
            continue
        va_median = statistics.median(vas[-va_window:])

        try:
            close = newest_close_as_of(bars_by_code, code, cutoff)
        except KeyError:
            continue
        if not (lo < close < hi):
            continue

        ranked.append((va_median, normalize_code(code)))

    # Va median descending; code ascending tie-break (deterministic).
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [code for _, code in ranked[:top_n]]
