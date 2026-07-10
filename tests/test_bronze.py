from __future__ import annotations

from datetime import date
from pathlib import Path

from stocktrading.bronze import discover_confirmed_dbs


def _touch(path: Path) -> None:
    path.write_bytes(b"")


def test_discover_confirmed_dbs_filters(tmp_path: Path) -> None:
    _touch(tmp_path / "2026-07-08.duckdb")  # confirmed
    _touch(tmp_path / "2026-07-09.duckdb")  # confirmed
    _touch(tmp_path / "2026-07-10.duckdb")  # today -> excluded
    _touch(tmp_path / "2026-07-09.duckdb.wal")  # WAL -> excludes 07-09 live? see below
    _touch(tmp_path / "heartbeat_kabu_2026-07-09.log")  # not a board DB

    confirmed = discover_confirmed_dbs(tmp_path, today=date(2026, 7, 10))

    # 07-09 has a live WAL so it is excluded; 07-10 is today; only 07-08 remains.
    assert [d.isoformat() for d, _ in confirmed] == ["2026-07-08"]


def test_discover_confirmed_dbs_excludes_future_and_today(tmp_path: Path) -> None:
    _touch(tmp_path / "2026-07-09.duckdb")
    _touch(tmp_path / "2026-07-11.duckdb")  # future

    confirmed = discover_confirmed_dbs(tmp_path, today=date(2026, 7, 10))

    assert [d.isoformat() for d, _ in confirmed] == ["2026-07-09"]
