from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Settings


@dataclass(frozen=True)
class LayerStatus:
    name: str
    path: Path
    exists: bool


def ensure_medallion_dirs(settings: Settings) -> list[LayerStatus]:
    layers = [
        ("bronze", settings.bronze_root),
        ("silver", settings.silver_root),
        ("gold", settings.gold_root),
    ]
    statuses: list[LayerStatus] = []
    for name, path in layers:
        path.mkdir(parents=True, exist_ok=True)
        statuses.append(LayerStatus(name=name, path=path, exists=path.exists()))
    return statuses
