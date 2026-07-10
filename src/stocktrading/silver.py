from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb

from .config import Settings
from .sql import sql_str as _sql_str


N_LEVELS = 10  # bronze carries the full 10-level book; silver passes it through


def _level_columns() -> str:
    """Levels 2..N verbatim; level 1 keeps its legacy aliases above."""
    lines = []
    for level in range(2, N_LEVELS + 1):
        for side in ("bid", "ask"):
            lines.append(f"    {side}_px_{level},")
            lines.append(f"    {side}_qty_{level},")
    return "\n".join(lines)


def _silver_select(glob: str, session_open: str, session_close: str) -> str:
    """Normalize the raw board snapshot into an analysis-friendly view.

    Interpretation lives here (mid / spread / imbalance), not in bronze. Values
    are inlined because DuckDB cannot prepare parameters inside CREATE/COPY.

    Besides the legacy L1 columns the maker simulator needs the full 10-level
    book, the quote-condition signs (0101 = continuous trading; 0107 special
    quote / itayose; 0120 halted) and the auction market-order quantities.
    """
    return f"""
SELECT
    code,
    ts_local                                   AS ts_event,
    bid_px_1                                   AS bid_px,
    bid_qty_1                                  AS bid_qty,
    ask_px_1                                   AS ask_px,
    ask_qty_1                                  AS ask_qty,
    (bid_px_1 + ask_px_1) / 2.0                AS mid,
    ask_px_1 - bid_px_1                        AS spread,
    CASE WHEN (bid_qty_1 + ask_qty_1) > 0
         THEN (bid_qty_1 - ask_qty_1)::DOUBLE / (bid_qty_1 + ask_qty_1)
    END                                        AS imbalance,
{_level_columns()}
    bid_sign,
    ask_sign,
    mo_buy_qty,
    mo_sell_qty,
    under_qty,
    over_qty,
    last_px,
    volume,
    turnover,
    vwap
FROM read_parquet({_sql_str(glob)}, hive_partitioning => true)
WHERE bid_px_1 > 0
  AND ask_px_1 > 0
  AND ask_px_1 >= bid_px_1
  AND ts_local::TIME BETWEEN {_sql_str(session_open)}::TIME AND {_sql_str(session_close)}::TIME
ORDER BY code, ts_event
"""


@dataclass(frozen=True)
class SilverBuild:
    trade_date: date
    target_dir: Path
    rows: int


def _bronze_dates(settings: Settings) -> list[date]:
    dates: list[date] = []
    if not settings.bronze_root.exists():
        return dates
    for child in sorted(settings.bronze_root.glob("date=*")):
        try:
            dates.append(date.fromisoformat(child.name.removeprefix("date=")))
        except ValueError:
            continue
    return dates


def build_silver(settings: Settings, trade_date: date) -> SilverBuild:
    """Transform one bronze date into normalized silver parquet (partitioned by code)."""
    bronze_dir = settings.bronze_root / f"date={trade_date.isoformat()}"
    glob = (bronze_dir / "*" / "*.parquet").as_posix()
    target_dir = settings.silver_root / f"date={trade_date.isoformat()}"
    target_dir.parent.mkdir(parents=True, exist_ok=True)

    select = _silver_select(glob, settings.session_open, settings.session_close)
    con = duckdb.connect()
    try:
        rows = con.execute(f"SELECT count(*) FROM ({select}) t").fetchone()[0]
        con.execute(
            f"COPY ({select}) TO {_sql_str(target_dir.as_posix())} "
            "(FORMAT PARQUET, PARTITION_BY (code), OVERWRITE_OR_IGNORE)"
        )
    finally:
        con.close()
    return SilverBuild(trade_date=trade_date, target_dir=target_dir, rows=rows)


def build_all_silver(settings: Settings) -> list[SilverBuild]:
    return [build_silver(settings, d) for d in _bronze_dates(settings)]
