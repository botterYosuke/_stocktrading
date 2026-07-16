"""gen2: 銘柄ごとの決定行テーブル組立と parquet キャッシュ。

列命名は gen1/gen1b と同一 (b_ts / f_* / y_* / <ck>_<L|S>_<field>) に加え、
複数日を 1 テーブルに持つため "day" (文字列) 列を持つ。
execution.simulate_symbol_day / gates.trade_metrics は day スライスで再利用する。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from scalp_agent.execution import SIDE_FIELDS
from scalp_agent.labels import labels_from_outcomes
from scalp_agent_bars.minute import source
from scalp_agent_bars.minute.config import (
    ATR_BARS,
    ATR_MULTS,
    ENTRY_END_TOD,
    FORCE_CLOSE_TOD,
    FRICTION_CALIBRATION_PATH,
    FRICTION_SAFETY,
    HORIZON_BARS,
    SESSION_AFTERNOON,
    SESSION_MORNING,
    UNIVERSE,
    cell_key,
    config_hash,
)
from scalp_agent_bars.minute.exec_bars import (
    atr,
    barrier_outcomes_bars,
    eligible_decisions,
)
from scalp_agent_bars.minute.features import (
    ALL_FEATURE_NAMES,
    build_own_features,
    day_cross_features,
    feature_schema_hash,
)

CACHE_DIR = Path(os.environ.get("GEN2_MINUTE_CACHE_DIR", "artifacts/cache/gen2_minute"))


# ---- 較正値の読み書き -----------------------------------------------------------

def load_friction() -> dict[str, float]:
    """凍結済み friction (spread_bps × 安全係数) を返す。無ければ明示エラー。"""
    raw = json.loads(FRICTION_CALIBRATION_PATH.read_text(encoding="utf-8"))
    return {c: raw["median_spread_bps"][c] * FRICTION_SAFETY for c in raw["median_spread_bps"]}


def universe_fingerprint() -> str:
    items = [source.source_fingerprint(c) for c in UNIVERSE]
    payload = json.dumps(items, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


# ---- 1 銘柄 1 日の組立 (pure) ---------------------------------------------------

def build_day_rows(
    code: str,
    day: str,
    bars_day: dict[str, np.ndarray],
    prev_ohlc: dict[str, float] | None,
    cross_feats: dict[str, np.ndarray] | None,
    spread_bps: float,
) -> dict[str, np.ndarray] | None:
    """決定行テーブル (1 銘柄 1 日)。決定ゼロなら None。"""
    ts = bars_day["ts"]
    atr_arr = atr(bars_day["high"], bars_day["low"], bars_day["close"], ATR_BARS)
    didx = eligible_decisions(
        bars_day["start_tod"], bars_day["open"], atr_arr,
        SESSION_MORNING, SESSION_AFTERNOON, ENTRY_END_TOD,
    )
    if len(didx) == 0:
        return None
    table: dict[str, np.ndarray] = {"b_ts": ts[didx + 1]}
    own = build_own_features(bars_day, prev_ohlc, atr_arr)
    for name, arr in own.items():
        table[f"f_{name}"] = arr[didx]
    for name in ALL_FEATURE_NAMES:
        if f"f_{name}" in table:
            continue
        if cross_feats is not None:
            table[f"f_{name}"] = cross_feats[name][didx]
        else:
            table[f"f_{name}"] = np.full(len(didx), np.nan)
    for h in HORIZON_BARS:
        for m in ATR_MULTS:
            ck = cell_key(h, m)
            cell = barrier_outcomes_bars(
                ts, bars_day["start_tod"],
                bars_day["open"], bars_day["high"], bars_day["low"],
                didx, atr_arr, spread_bps, h, m, FORCE_CLOSE_TOD,
            )
            y, yv = labels_from_outcomes(cell["long"], cell["short"])
            table[f"y_{ck}"] = y
            table[f"yv_{ck}"] = yv
            for sp, side in (("L", "long"), ("S", "short")):
                for f in SIDE_FIELDS:
                    table[f"{ck}_{sp}_{f}"] = cell[side][f]
    table["day"] = np.full(len(didx), day, dtype=object)
    return table


# ---- ユニバース一括組立 ----------------------------------------------------------

def _prev_ohlc_of(bars_by_day: dict[str, dict[str, np.ndarray]], day: str) -> dict | None:
    days = sorted(bars_by_day)
    i = days.index(day)
    if i == 0:
        return None
    b = bars_by_day[days[i - 1]]
    return {
        "open": float(b["open"][0]), "high": float(b["high"].max()),
        "low": float(b["low"].min()), "close": float(b["close"][-1]),
    }


def build_universe_tables(
    day_min: str,
    day_max: str,
    peer_map: dict[str, str],
    friction: dict[str, float],
    progress=None,
) -> dict[str, dict[str, np.ndarray]]:
    """全銘柄のテーブルを組む。code → 複数日連結テーブル。"""
    all_bars = {c: source.load_symbol_days(c, day_min, day_max) for c in UNIVERSE}
    all_days = sorted({d for b in all_bars.values() for d in b})
    cross_by_day: dict[str, dict[str, dict[str, np.ndarray]]] = {}
    for day in all_days:
        per_code = {
            c: {
                "minute": (all_bars[c][day]["start_tod"] // 60.0).astype(np.int64),
                "close": all_bars[c][day]["close"],
            }
            for c in UNIVERSE if day in all_bars[c]
        }
        cross_by_day[day] = day_cross_features(per_code, peer_map)
    out: dict[str, dict[str, np.ndarray]] = {}
    for ci, c in enumerate(UNIVERSE):
        parts: list[dict[str, np.ndarray]] = []
        for day in sorted(all_bars[c]):
            rows = build_day_rows(
                c, day, all_bars[c][day], _prev_ohlc_of(all_bars[c], day),
                cross_by_day[day].get(c), friction[c],
            )
            if rows is not None:
                parts.append(rows)
        if not parts:
            continue
        out[c] = {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}
        if progress:
            progress(f"{ci+1}/{len(UNIVERSE)} {c}: {len(out[c]['b_ts'])} decisions")
    return out


# ---- parquet キャッシュ ----------------------------------------------------------

def cache_paths(code: str, scope: str) -> tuple[Path, Path]:
    base = CACHE_DIR / scope
    return base / f"{code}.parquet", base / f"{code}.meta.json"


def _expected_meta(code: str, scope: str, day_min: str, day_max: str) -> dict:
    return {
        "config_hash": config_hash(),
        "feature_schema_hash": feature_schema_hash(),
        "scope": scope,
        "day_range": [day_min, day_max],
        "universe_fingerprint": universe_fingerprint(),
    }


def write_cache(code: str, scope: str, day_min: str, day_max: str,
                table: dict[str, np.ndarray]) -> None:
    pq_path, meta_path = cache_paths(code, scope)
    pq_path.parent.mkdir(parents=True, exist_ok=True)
    arrays = {}
    for k, v in table.items():
        arrays[k] = pa.array(list(v)) if v.dtype == object else pa.array(np.asarray(v))
    pq.write_table(pa.table(arrays), pq_path)
    meta = _expected_meta(code, scope, day_min, day_max)
    meta["n_decisions"] = int(len(table["b_ts"]))
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def is_cache_valid(code: str, scope: str, day_min: str, day_max: str) -> bool:
    pq_path, meta_path = cache_paths(code, scope)
    if not (pq_path.exists() and meta_path.exists()):
        return False
    try:
        saved = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    saved.pop("n_decisions", None)
    return saved == _expected_meta(code, scope, day_min, day_max)


def load_cache(code: str, scope: str) -> dict[str, np.ndarray]:
    pq_path, _ = cache_paths(code, scope)
    t = pq.read_table(pq_path)
    out = {}
    for name in t.column_names:
        col = t.column(name).to_numpy(zero_copy_only=False)
        out[name] = col
    return out


# ---- 学習・評価用 ----------------------------------------------------------------

def features_matrix(table: dict[str, np.ndarray], names: tuple[str, ...]) -> np.ndarray:
    if len(table["b_ts"]) == 0:
        return np.empty((0, len(names)))
    return np.column_stack([np.asarray(table[f"f_{n}"], dtype=np.float64) for n in names])


def day_mask(table: dict[str, np.ndarray], day_min: str, day_max: str) -> np.ndarray:
    days = np.asarray(table["day"])
    return (days >= day_min) & (days <= day_max)
