from __future__ import annotations

from pathlib import Path

from stocktrading.config import Settings
from stocktrading.medallion import ensure_medallion_dirs


def test_ensure_medallion_dirs_creates_layers(tmp_path: Path) -> None:
    settings = Settings(
        backcast_root=tmp_path / "backcast",
        board_source_root=tmp_path / "board",
        medallion_root=tmp_path / "data",
        market_timezone="Asia/Tokyo",
        session_open="09:00:00",
        session_close="15:30:00",
    )

    statuses = ensure_medallion_dirs(settings)

    assert [status.name for status in statuses] == ["bronze", "silver", "gold"]
    assert all(status.exists for status in statuses)
