from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb

from .config import Settings

BOARD_TABLE = "board_push"


@dataclass(frozen=True)
class BronzeExport:
    trade_date: date
    source: Path
    target_dir: Path
    rows: int


def _parse_trade_date(db_path: Path) -> date | None:
    """`2026-07-09.duckdb` -> date(2026, 7, 9); None if the stem is not a date."""
    try:
        return date.fromisoformat(db_path.stem)
    except ValueError:
        return None


def _is_live(db_path: Path) -> bool:
    """A DuckDB with a sibling write-ahead log is still being recorded."""
    return db_path.with_name(db_path.name + ".wal").exists()


def discover_confirmed_dbs(source_root: Path, today: date | None = None) -> list[tuple[date, Path]]:
    """Daily board DuckDBs that are safe to snapshot: not today's, no live WAL.

    We deliberately skip today's file and any DB with an open `.wal` so bronze
    never captures a mid-write, inconsistent snapshot. `.log` heartbeat files and
    anything without a date-shaped name are ignored.
    """
    today = today or date.today()
    confirmed: list[tuple[date, Path]] = []
    for db_path in sorted(source_root.glob("*.duckdb")):
        trade_date = _parse_trade_date(db_path)
        if trade_date is None or trade_date >= today or _is_live(db_path):
            continue
        confirmed.append((trade_date, db_path))
    return confirmed


def export_to_bronze(
    settings: Settings,
    today: date | None = None,
    limit: int | None = None,
) -> list[BronzeExport]:
    """Snapshot confirmed daily board DBs into immutable date/code parquet.

    Bronze keeps every source column verbatim (audit-first, no interpretation).
    Layout: `data/bronze/date=YYYY-MM-DD/code=XXXX/*.parquet`.
    """
    if not settings.board_source_root.exists():
        return []

    dbs = discover_confirmed_dbs(settings.board_source_root, today=today)
    if limit is not None:
        dbs = dbs[:limit]

    exports: list[BronzeExport] = []
    settings.bronze_root.mkdir(parents=True, exist_ok=True)
    for trade_date, source in dbs:
        target_dir = settings.bronze_root / f"date={trade_date.isoformat()}"
        con = duckdb.connect(str(source), read_only=True)
        try:
            rows = con.execute(f"SELECT count(*) FROM {BOARD_TABLE}").fetchone()[0]
            con.execute(
                f"COPY (SELECT * FROM {BOARD_TABLE}) TO '{target_dir.as_posix()}' "
                "(FORMAT PARQUET, PARTITION_BY (code), OVERWRITE_OR_IGNORE)"
            )
        finally:
            con.close()
        exports.append(
            BronzeExport(trade_date=trade_date, source=source, target_dir=target_dir, rows=rows)
        )
    return exports
