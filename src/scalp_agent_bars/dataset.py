"""gen1b: 銘柄×日の決定行テーブル (1分足決定グリッド) の組立と parquet キャッシュ。

gen1 の dataset.py と同じ構造・同じ列命名 (b_ts / f_* / y_* / <ck>_<L|S>_<field>)
を保つ — execution.simulate_symbol_day / gates / nulls をそのまま共有するため。

決定グリッド (gen1 の 1Hz に対応する gen1b 規則):
- 決定境界 t = 取引のあった 1 分バーの確定時刻 (end_ts)。新しいバーが
  確定していなければ推論しない (gen1 の「新着 PUSH なしなら推論しない」に対応)
- 特徴量は t 以前に確定したバーのみから計算 (as-of・lookahead なし)
- as-of PUSH = timestamp <= t の最後の PUSH。エントリはその厳密な次 PUSH
  (= t より厳密に後の最初の PUSH)。ENTRY_MAX_LATENCY_S の stale ガードは gen1 と同一
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from scalp_agent import loader
from scalp_agent_bars.bars import minute_bars
from scalp_agent_bars.config import (
    ENTRY_END_TOD,
    ENTRY_MAX_LATENCY_S,
    FORCE_CLOSE_TOD,
    HORIZONS_S,
    MULTS,
    PREV_DAY,
    SESSION_AFTERNOON,
    SESSION_MORNING,
    cell_key,
)
from scalp_agent_bars.features import (
    FEATURE_NAMES,
    build_bar_features,
    feature_schema_hash,
)
from scalp_agent.dataset import _materialize_side
from scalp_agent.execution import SIDE_FIELDS
from scalp_agent.labels import barrier_outcomes_grid, labels_from_outcomes
from scalp_agent.sessions import exec_subset, time_of_day

CACHE_DIR = Path(os.environ.get("SCALP_BARS_CACHE_DIR", "artifacts/cache/gen1b"))
CACHE_VERSION = 1


def cache_paths(day: str, code: str) -> tuple[Path, Path]:
    base = CACHE_DIR / day
    return base / f"{code}.parquet", base / f"{code}.meta.json"


def _source_fingerprint(day: str) -> dict:
    p = loader.db_path(day)
    st = p.stat()
    return {"path": str(p), "size": st.st_size, "mtime": st.st_mtime}


def prev_day_of(day: str) -> str | None:
    """凍結 PREV_DAY マップの参照。未宣言日は明示エラー (暗黙 fallback しない)。"""
    if day not in PREV_DAY:
        raise KeyError(f"day {day} は PREV_DAY マップ未宣言 — config で凍結してから使う")
    return PREV_DAY[day]


# ---- 日足 (前日コンテキスト) -------------------------------------------------

def daily_ohlc_all(day: str) -> dict[str, dict[str, float]]:
    """1 日分・全銘柄の日足 OHLC (歩み値 09:00–15:30・last_px>0)。JSON キャッシュ付き。

    定義の正本は bars.daily_ohlc (テストで duckdb 集計と突合する)。
    """
    import duckdb

    cache = CACHE_DIR / "daily" / f"{day}.json"
    fp = _source_fingerprint(day)
    if cache.exists():
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            if saved.get("source") == fp:
                return saved["ohlc"]
        except (OSError, json.JSONDecodeError):
            pass
    with duckdb.connect(str(loader.db_path(day)), read_only=True) as con:
        con.execute("SET enable_progress_bar=false")
        rows = con.execute(
            """
            with t as (
              select code, ts_local, last_px from board_push
              where last_px > 0
                and cast(ts_local as time) >= time '09:00:00'
                and cast(ts_local as time) <= time '15:30:00'
            )
            select code,
                   arg_min(last_px, ts_local) as open,
                   max(last_px) as high,
                   min(last_px) as low,
                   arg_max(last_px, ts_local) as close
            from t group by code
            """
        ).fetchall()
    ohlc = {
        r[0]: {"open": float(r[1]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4])}
        for r in rows
    }
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps({"source": fp, "ohlc": ohlc}, ensure_ascii=False), encoding="utf-8"
    )
    return ohlc


# ---- 決定グリッド ------------------------------------------------------------

def bar_decision_grid(
    ex_ts: np.ndarray, bar_end_ts: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(as-of PUSH idx, 決定境界 ts, バー idx)。エントリ窓内のバー確定時刻のみ。"""
    tod = np.mod(bar_end_ts, 86400.0)
    in_window = (
        ((tod >= SESSION_MORNING[0]) & (tod < SESSION_MORNING[1]))
        | ((tod >= SESSION_AFTERNOON[0]) & (tod < ENTRY_END_TOD))
    )
    bar_idx = np.where(in_window)[0]
    b_ts = bar_end_ts[bar_idx]
    didx = np.searchsorted(ex_ts, b_ts, side="right") - 1
    keep = didx >= 0
    return didx[keep].astype(np.int64), b_ts[keep], bar_idx[keep]


# ---- テーブル組立 (pure) ------------------------------------------------------

def build_table_from_exec(
    ex: dict[str, np.ndarray], prev_ohlc: dict[str, float] | None
) -> dict[str, np.ndarray]:
    """executable 部分列 + 前日日足 → 決定行テーブル。I/O なし。"""
    ts = ex["ts"]
    bars = minute_bars(ts, ex["last_px"], ex["volume"])
    didx, b_ts, bar_idx = bar_decision_grid(ts, bars["end_ts"])
    table: dict[str, np.ndarray] = {"b_ts": b_ts}
    feats = build_bar_features(bars, prev_ohlc)
    for name in FEATURE_NAMES:
        table[f"f_{name}"] = feats[name][bar_idx] if len(bar_idx) else np.array([])
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


def build_symbol_day_table(day: str, code: str) -> dict[str, np.ndarray]:
    """1 銘柄 1 日の決定行テーブル (キャッシュ非依存)。前日日足を含めて組む。"""
    snap = loader.load_symbol_day(day, code)
    ex = exec_subset(snap)
    pd = prev_day_of(day)
    prev_ohlc = daily_ohlc_all(pd).get(code) if pd else None
    return build_table_from_exec(ex, prev_ohlc)


# ---- キャッシュ ---------------------------------------------------------------

def _expected_meta(day: str, code: str) -> dict:
    pd = prev_day_of(day)
    return {
        "version": CACHE_VERSION,
        "day": day,
        "code": code,
        "feature_schema_hash": feature_schema_hash(),
        "label_spec": {
            "kind": "triple_barrier_bars_v1",
            "horizons_s": list(HORIZONS_S),
            "mults": list(MULTS),
            "entry_max_latency_s": ENTRY_MAX_LATENCY_S,
            "force_close_tod": FORCE_CLOSE_TOD,
        },
        "prev_day": pd,
        "source": _source_fingerprint(day),
        "prev_source": _source_fingerprint(pd) if pd else None,
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
    if is_cache_valid(day, code):
        return load_cache(day, code)
    table = build_symbol_day_table(day, code)
    write_cache(day, code, table)
    return table


# ---- 学習・評価用の組立 --------------------------------------------------------

def features_from_table(table: dict[str, np.ndarray]) -> np.ndarray:
    if len(table["b_ts"]) == 0:
        return np.empty((0, len(FEATURE_NAMES)))
    return np.column_stack([table[f"f_{n}"] for n in FEATURE_NAMES])


def training_arrays(
    tables: dict[tuple[str, str], dict[str, np.ndarray]],
    horizon_s: float,
    mult: float,
) -> tuple[np.ndarray, np.ndarray]:
    """あるセルの X, y (クラス index 0/1/2)。無効ラベル行 (yv=False) は落とす。"""
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
