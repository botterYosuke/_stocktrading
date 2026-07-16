"""G2 代替ヌル (OOS 1 日探針用)。2026-07-16 確定仕様。

- 時刻ヌル: 同銘柄・同 30 分帯の eligible な 1Hz 決定行から、実取引と同数・
  同 side 構成のエントリをランダム抽出。next-PUSH 約定・トリプルバリア・
  ポジション重複禁止を維持。200 有効標本・固定 seed 20260714。
- サイドヌル: 実エントリ時刻を固定し、銘柄内で side をシャッフル。long/short
  数を維持。単一 side しかない銘柄は「退化」として報告 (恣意的 50/50 は作らない)。

1 日内の取引は独立ではないため、結果を「有意差」「G2 PASS」とは呼ばない。
正式な日選択 G2 は D≥20 到達後に実施する。
"""
from __future__ import annotations

import numpy as np

from scalp_agent.config import NULL_BAND_S, NULL_N_SAMPLES, NULL_SEED
from scalp_agent.execution import SIDE_FIELDS, Trade, make_trade
from scalp_agent.labels import EXIT_NONE


def _band(ts: float | np.ndarray) -> np.ndarray:
    return (np.mod(ts, 86400.0) // NULL_BAND_S).astype(np.int64)


def _eligible_pool(
    tables: dict[str, dict[str, np.ndarray]], ck: str
) -> dict[str, dict[int, dict[int, np.ndarray]]]:
    """code → side(+1/-1) → band → 決定行 index 配列 (バリア解決済みの行のみ)。"""
    pool: dict[str, dict[int, dict[int, np.ndarray]]] = {}
    for code, table in tables.items():
        b_ts = table["b_ts"]
        if len(b_ts) == 0:
            continue
        bands = _band(b_ts)
        per_side: dict[int, dict[int, np.ndarray]] = {}
        for side, sp in ((1, "L"), (-1, "S")):
            ok = table[f"{ck}_{sp}_reason"] != EXIT_NONE
            d: dict[int, np.ndarray] = {}
            for b in np.unique(bands[ok]):
                d[int(b)] = np.where(ok & (bands == b))[0]
            per_side[side] = d
        pool[code] = per_side
    return pool


def _place_without_overlap(
    slots: list[tuple[int, int]],
    tables: dict[str, dict[str, np.ndarray]],
    code: str,
    ck: str,
    rng: np.random.Generator,
    pool: dict[int, dict[int, np.ndarray]],
    max_tries: int = 50,
) -> list[tuple[int, int]] | None:
    """1 銘柄分のヌルエントリ配置。(side, band) のスロット列を、重複禁止を
    満たすまでリサンプルして決定行 index 列にする。失敗なら None。"""
    table = tables[code]
    b_ts = table["b_ts"]
    placed: list[tuple[int, int]] = []  # (decision_idx, side)
    intervals: list[tuple[float, float]] = []
    for side, band in slots:
        cand = pool.get(side, {}).get(band)
        if cand is None or len(cand) == 0:
            return None
        sp = "L" if side == 1 else "S"
        exit_ts_arr = table[f"{ck}_{sp}_exit_ts"]
        ok = False
        for _ in range(max_tries):
            d = int(cand[rng.integers(len(cand))])
            t0, t1 = float(b_ts[d]), float(exit_ts_arr[d])
            if any(not (t1 <= a or t0 >= b) for a, b in intervals):
                continue
            placed.append((d, side))
            intervals.append((t0, t1))
            ok = True
            break
        if not ok:
            return None
    return placed


def time_shuffle_null(
    trades: list[Trade],
    tables: dict[str, dict[str, np.ndarray]],
    ck: str,
    n_samples: int = NULL_N_SAMPLES,
    seed: int = NULL_SEED,
) -> dict:
    """時刻ヌル。各レプリケートの net/entry 分布と actual の比較を返す。"""
    rng = np.random.default_rng(seed)
    actual_net = float(np.mean([t.net_bps for t in trades])) if trades else None
    slots_by_code: dict[str, list[tuple[int, int]]] = {}
    for t in trades:
        slots_by_code.setdefault(t.code, []).append((t.side, int(_band(t.entry_ts))))
    pools = _eligible_pool(tables, ck)
    reps: list[float] = []
    attempts = 0
    max_attempts = n_samples * 20
    while len(reps) < n_samples and attempts < max_attempts:
        attempts += 1
        nets: list[float] = []
        failed = False
        for code, slots in slots_by_code.items():
            if code not in pools:
                failed = True
                break
            placed = _place_without_overlap(slots, tables, code, ck, rng, pools[code])
            if placed is None:
                failed = True
                break
            table = tables[code]
            for d, side in placed:
                sp = "L" if side == 1 else "S"
                fields = {f: table[f"{ck}_{sp}_{f}"] for f in SIDE_FIELDS}
                nets.append(make_trade(code, "", side, float(table["b_ts"][d]), fields, d).net_bps)
        if failed or not nets:
            continue
        reps.append(float(np.mean(nets)))
    arr = np.array(reps)
    result = {
        "kind": "time_shuffle",
        "n_valid_replicates": len(reps),
        "n_attempts": attempts,
        "actual_net_per_entry": actual_net,
    }
    if len(reps) and actual_net is not None:
        result |= {
            "null_median": float(np.median(arr)),
            "null_p05": float(np.percentile(arr, 5)),
            "null_p95": float(np.percentile(arr, 95)),
            "gap": actual_net - float(np.median(arr)),
            "p_upper": float((1 + np.sum(arr >= actual_net)) / (1 + len(arr))),
        }
    return result


def side_shuffle_null(
    trades: list[Trade],
    tables: dict[str, dict[str, np.ndarray]],
    ck: str,
    n_samples: int = NULL_N_SAMPLES,
    seed: int = NULL_SEED,
) -> dict:
    """サイドヌル。実エントリ時刻固定・銘柄内 side シャッフル。

    side 反転で exit 時刻が変わるため重複禁止を再適用し、弾かれた取引は
    そのレプリケートから落とす (平均 drop 数を報告)。単一 side 銘柄は退化。
    """
    rng = np.random.default_rng(seed + 1)
    actual_net = float(np.mean([t.net_bps for t in trades])) if trades else None
    by_code: dict[str, list[Trade]] = {}
    for t in trades:
        by_code.setdefault(t.code, []).append(t)
    degenerate = sorted(
        c for c, ts_ in by_code.items() if len({t.side for t in ts_}) == 1
    )
    # 取引 → 決定行 index の逆引き (b_ts は一意)
    didx: dict[tuple[str, float], int] = {}
    for code, table in tables.items():
        for i, b in enumerate(table["b_ts"]):
            didx[(code, float(b))] = i
    reps: list[float] = []
    drops: list[int] = []
    for _ in range(n_samples):
        nets: list[float] = []
        dropped = 0
        for code, ts_ in by_code.items():
            sides = np.array([t.side for t in ts_])
            perm = rng.permutation(sides)
            table = tables[code]
            busy_until = -np.inf
            for t, side in zip(sorted(ts_, key=lambda x: x.decision_ts), perm):
                if t.decision_ts < busy_until:
                    dropped += 1
                    continue
                d = didx[(code, t.decision_ts)]
                sp = "L" if side == 1 else "S"
                if table[f"{ck}_{sp}_reason"][d] == EXIT_NONE:
                    dropped += 1
                    continue
                fields = {f: table[f"{ck}_{sp}_{f}"] for f in SIDE_FIELDS}
                tr = make_trade(code, t.day, int(side), t.decision_ts, fields, d)
                nets.append(tr.net_bps)
                busy_until = tr.exit_ts
        if nets:
            reps.append(float(np.mean(nets)))
            drops.append(dropped)
    arr = np.array(reps)
    result = {
        "kind": "side_shuffle",
        "n_valid_replicates": len(reps),
        "degenerate_codes": degenerate,
        "mean_dropped_per_replicate": float(np.mean(drops)) if drops else None,
        "actual_net_per_entry": actual_net,
    }
    if len(reps) and actual_net is not None:
        result |= {
            "null_median": float(np.median(arr)),
            "null_p05": float(np.percentile(arr, 5)),
            "null_p95": float(np.percentile(arr, 95)),
            "gap": actual_net - float(np.median(arr)),
            "p_upper": float((1 + np.sum(arr >= actual_net)) / (1 + len(arr))),
        }
    return result
