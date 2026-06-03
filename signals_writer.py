from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from data_source import code_to_symbol, normalize_code, parse_date


SIGNAL_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: str
    confidence: float
    code: str | None = None
    brand: str | None = None


def stocktrading_side_to_text(value: object) -> str:
    side = int(value)
    if side == 1:
        return "SHORT"
    if side == 2:
        return "LONG"
    raise ValueError(f"unsupported stocktrading side: {value!r}")


def signal_from_target_row(row: Mapping[str, object]) -> Signal:
    code = normalize_code(row["code"])
    return Signal(
        symbol=code_to_symbol(code),
        code=code,
        brand=_optional_str(row.get("brand")),
        side=stocktrading_side_to_text(row["side"]),
        confidence=float(row["pred"]),
    )


def write_daily_signals(
    *,
    output_dir: str | Path,
    target_date: str | date,
    as_of: str | date,
    rows: Iterable[Mapping[str, object] | Signal],
    source: str = "stocktrading.model_manager",
    generated_at: str | None = None,
    regulation_filter: Mapping[str, object] | None = None,
) -> Path:
    """Write signals_YYYY-MM-DD.json for TTWR's SignalDrivenStrategy."""
    target = parse_date(target_date)
    as_of_date = parse_date(as_of)
    signals = [_coerce_signal(row) for row in rows]

    payload = {
        "schema_version": SIGNAL_SCHEMA_VERSION,
        "target_date": target.isoformat(),
        "as_of": as_of_date.isoformat(),
        "source": source,
        "generated_at": generated_at or _utc_now_iso(),
        "regulation_filter": dict(regulation_filter or _default_regulation_filter()),
        "signals": [_signal_to_json(signal) for signal in signals],
    }

    path = Path(output_dir) / f"signals_{target.isoformat()}.json"
    _write_json_atomic(path, payload)
    return path


def write_manifest(
    *,
    output_dir: str | Path,
    start: str | date,
    end: str | date,
    signal_files: Iterable[str | Path],
    timezone_name: str = "Asia/Tokyo",
    prediction_horizon: str = "next_business_day",
    retrain_policy: str = "daily",
    train_window_business_days: int = 80,
    regulation_filter: Mapping[str, object] | None = None,
) -> Path:
    """Write manifest.json containing the replay range and instrument union."""
    output_path = Path(output_dir)
    files = [Path(p).name for p in signal_files]
    instruments = _instrument_union(output_path, files)

    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "start": parse_date(start).isoformat(),
        "end": parse_date(end).isoformat(),
        "timezone": timezone_name,
        "prediction_horizon": prediction_horizon,
        "retrain_policy": retrain_policy,
        "train_window_business_days": int(train_window_business_days),
        "regulation_filter": dict(regulation_filter or _default_regulation_filter()),
        "files": files,
        "instruments": instruments,
    }

    path = output_path / "manifest.json"
    _write_json_atomic(path, payload)
    return path


def _coerce_signal(row: Mapping[str, object] | Signal) -> Signal:
    if isinstance(row, Signal):
        return row
    return signal_from_target_row(row)


def _signal_to_json(signal: Signal) -> dict[str, object]:
    payload: dict[str, object] = {
        "symbol": signal.symbol,
        "side": signal.side,
        "confidence": signal.confidence,
    }
    if signal.code is not None:
        payload["code"] = signal.code
    if signal.brand is not None:
        payload["brand"] = signal.brand
    return payload


def _instrument_union(output_dir: Path, files: list[str]) -> list[str]:
    instruments: set[str] = set()
    for file_name in files:
        path = output_dir / file_name
        doc = json.loads(path.read_text(encoding="utf-8"))
        for signal in doc.get("signals", []):
            instruments.add(str(signal["symbol"]))
    return sorted(instruments)


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _default_regulation_filter() -> dict[str, str]:
    return {
        "brain": "disabled",
        "replay": "not_available",
        "live": "pre_trade_check",
    }


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
