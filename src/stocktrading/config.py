from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
import os


@dataclass(frozen=True)
class Settings:
    backcast_root: Path
    board_source_root: Path
    medallion_root: Path
    market_timezone: str
    session_open: str
    session_close: str

    @property
    def bronze_root(self) -> Path:
        return self.medallion_root / "bronze"

    @property
    def silver_root(self) -> Path:
        return self.medallion_root / "silver"

    @property
    def gold_root(self) -> Path:
        return self.medallion_root / "gold"


def load_settings(env_file: str | Path = ".env") -> Settings:
    load_dotenv(env_file)
    return Settings(
        backcast_root=Path(os.getenv("BACKCAST_ROOT", r"C:\Users\sasai\Documents\backcast")),
        board_source_root=Path(os.getenv("BOARD_SOURCE_ROOT", r"S:\jp\stocks_board_kabu_push")),
        medallion_root=Path(os.getenv("MEDALLION_ROOT", r".\data")),
        market_timezone=os.getenv("MARKET_TIMEZONE", "Asia/Tokyo"),
        session_open=os.getenv("SESSION_OPEN", "09:00:00"),
        session_close=os.getenv("SESSION_CLOSE", "15:30:00"),
    )
