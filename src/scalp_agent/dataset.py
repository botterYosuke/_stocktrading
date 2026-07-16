"""銘柄×日の決定行テーブルを parquet にキャッシュする層。

S: ドライブは読み出しが遅いため、(day, code) 単位で
  決定境界 ts + 正規化特徴量 + 全 24 (horizon, mult) セル × 両サイドの
  バリア解決 (約定価格まで実体化) + 3 値ラベル
を一度だけ計算してローカル parquet に落とす。以降の学習・掃引・OOS・ヌルは
すべてキャッシュから読む。

キャッシュ整合性は sidecar JSON (.meta.json) で検査する:
  day / code / feature_schema_hash / label grid / source fingerprint (サイズ+mtime)
のいずれかが不一致なら stale として再計算する。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from scalp_agent import loader
from scalp_agent.config import (
    ENTRY_MAX_LATENCY_S,
    FORCE_CLOSE_TOD,
    HORIZONS_S,
    MULTS,
    cell_key,
)
from scalp_agent.execution import SIDE_FIELDS
from scalp_agent.features import (
    FEATURE_NAMES,
    build_features_normalized,
    feature_schema_hash,
)
from scalp_agent.labels import barrier_outcomes_grid, labels_from_outcomes
from scalp_agent.sessions import decision_grid, exec_subset, time_of_day

CACHE_DIR = Path(os.environ.get("SCALP_CACHE_DIR", "artifacts/cache/gen1"))
CACHE_VERSION = 1


def cache_paths(day: str, code: str) -> tuple[Path, Path]:
    base = CACHE_DIR / day
    return base / f"{code}.parquet", base / f"{code}.meta.json"


def _source_fingerprint(day: str) -> dict:
    p = loader.db_path(day)
    st = p.stat()
    return {"path": str(p), "size": st.st_size, "mtime": st.st_mtime}


def _expected_meta(day: str, code: str) -> dict:
    return {
        "version": CACHE_VERSION,
        "day": day,
        "code": code,
        "feature_schema_hash": feature_schema_hash(),
        "label_spec": {
            "kind": "triple_barrier_v1",
            "horizons_s": list(HORIZONS_S),
            "mults": list(MULTS),
            "entry_max_latency_s": ENTRY_MAX_LATENCY_S,
            "force_close_tod": FORCE_CLOSE_TOD,
        },
        "source": _source_fingerprint(day),
    }


def is_cache_valid(day: str, code: str) -> bool:
    pq_path, meta_path = cache_paths(day, code)
    if not (pq_path.exists() and meta_path.exists()):
        return False
    try:
        saved = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    expected = _expected_meta(day, code)
    saved.pop("n_decisions", None)
    return saved == expected


def _materialize_side(
    rec: dict[str, np.ndarray],
    ts: np.ndarray,
    bid: np.ndarray,
    ask: np.ndarray,
    mid: np.ndarray,
    side: int,
) -> dict[str, np.ndarray]:
    """entry/exit index → 実約定価格・時刻の配列に実体化。無効行は NaN/0。"""
    e = rec["entry_idx"]
    x = rec["exit_idx"]
    ok = (e >= 0) & (x >= 0)
    e_c = np.clip(e, 0, None)
    x_c = np.clip(x, 0, None)
    nanfill = lambda a: np.where(ok, a, np.nan)  # noqa: E731
    return {
        "reason": rec["reason"],
        "entry_ts": nanfill(ts[e_c]),
        "exit_ts": nanfill(ts[x_c]),
        "entry_px": nanfill(ask[e_c] if side == 1 else bid[e_c]),
        "exit_px": nanfill(bid[x_c] if side == 1 else ask[x_c]),
        "mid_entry": nanfill(mid[e_c]),
        "mid_exit": nanfill(mid[x_c]),
        "tp_trigger_ts": rec["tp_trigger_ts"],
        "exit_trigger_ts": rec["exit_trigger_ts"],
        "mae_bps": rec["mae_bps"],
    }


def build_symbol_day_table(day: str, code: str) -> dict[str, np.ndarray]:
    """1 銘柄 1 日の決定行テーブルを計算する (キャッシュ非依存の pure 組立)。"""
    snap = loader.load_symbol_day(day, code)
    ex = exec_subset(snap)
    ts = ex["ts"]
    didx, b_ts = decision_grid(ts)
    table: dict[str, np.ndarray] = {"b_ts": b_ts}
    feats = build_features_normalized(ex)
    for name in FEATURE_NAMES:
        table[f"f_{name}"] = feats[name][didx] if len(didx) else np.array([])
    if len(didx) == 0:
        for h in HORIZONS_S:
            for m in MULTS:
                ck = cell_key(h, m)
                table[f"y_{ck}"] = np.array([], dtype=np.int8)
                table[f"yv_{ck}"] = np.array([], dtype=bool)
                for sp in ("L", "S"):
                    for f in SIDE_FIELDS:
                        table[f"{ck}_{sp}_{f}"] = np.array([])
        return table
    bid, ask = ex["bid_px_1"], ex["ask_px_1"]
    mid = (bid + ask) / 2.0
    tod = time_of_day(ts)
    outcomes = barrier_outcomes_grid(
        ts, tod, bid, ask, didx, b_ts,
        mults=MULTS, horizons_s=HORIZONS_S,
        entry_max_latency_s=ENTRY_MAX_LATENCY_S,
        force_close_tod=FORCE_CLOSE_TOD,
    )
    for h in HORIZONS_S:
        for m in MULTS:
            ck = cell_key(h, m)
            cell = outcomes[(h, m)]
            y, yv = labels_from_outcomes(cell["long"], cell["short"])
            table[f"y_{ck}"] = y
            table[f"yv_{ck}"] = yv
            for sp, side, rec in (("L", 1, cell["long"]), ("S", -1, cell["short"])):
                mat = _materialize_side(rec, ts, bid, ask, mid, side)
                for f in SIDE_FIELDS:
                    table[f"{ck}_{sp}_{f}"] = mat[f]
    return table


def write_cache(day: str, code: str, table: dict[str, np.ndarray]) -> None:
    pq_path, meta_path = cache_paths(day, code)
    pq_path.parent.mkdir(parents=True, exist_ok=True)
    arrays = {k: pa.array(np.asarray(v)) for k, v in table.items()}
    pq.write_table(pa.table(arrays), pq_path)
    meta = _expected_meta(day, code)
    meta["n_decisions"] = int(len(table["b_ts"]))
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def load_cache(day: str, code: str) -> dict[str, np.ndarray]:
    pq_path, _ = cache_paths(day, code)
    t = pq.read_table(pq_path)
    return {name: t.column(name).to_numpy(zero_copy_only=False) for name in t.column_names}


def ensure_cache(day: str, code: str) -> dict[str, np.ndarray]:
    """有効キャッシュがあれば読み、無ければ構築して書く。"""
    if is_cache_valid(day, code):
        return load_cache(day, code)
    table = build_symbol_day_table(day, code)
    write_cache(day, code, table)
    return table


# ---- 学習・評価用の組立 -------------------------------------------------------

def side_fields_from_table(
    table: dict[str, np.ndarray], ck: str, side_prefix: str
) -> dict[str, np.ndarray]:
    return {f: table[f"{ck}_{side_prefix}_{f}"] for f in SIDE_FIELDS}


def features_from_table(table: dict[str, np.ndarray]) -> np.ndarray:
    if len(table["b_ts"]) == 0:
        return np.empty((0, len(FEATURE_NAMES)))
    return np.column_stack([table[f"f_{n}"] for n in FEATURE_NAMES])


def training_arrays(
    tables: dict[tuple[str, str], dict[str, np.ndarray]],
    horizon_s: float,
    mult: float,
) -> tuple[np.ndarray, np.ndarray]:
    """(day, code)→table の辞書から、あるセルの X, y (クラス index 0/1/2) を組む。

    無効ラベル行 (yv=False) は落とす。特徴量の NaN は LightGBM に委ねる。
    """
    ck = cell_key(horizon_s, mult)
    xs, ys = [], []
    for table in tables.values():
        if len(table["b_ts"]) == 0:
            continue
        yv = table[f"yv_{ck}"].astype(bool)
        if not yv.any():
            continue
        xs.append(features_from_table(table)[yv])
        ys.append(table[f"y_{ck}"][yv].astype(np.int64) + 1)  # {-1,0,1}→{0,1,2}
    if not xs:
        return np.empty((0, len(FEATURE_NAMES))), np.empty((0,), dtype=np.int64)
    return np.concatenate(xs), np.concatenate(ys)
