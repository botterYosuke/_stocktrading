"""gen4 トレードシミュレーション + 診断。pure (入力はデータセット配列と score)。

各 (day, tod) グループで score 上位 K を long・下位 K を short。
entry = 判断時刻直後バーの始値 (taker)、exit = horizon バー始値 (taker)。
friction は呼値ラダー保守モデル (dataset 列)。
"""
from __future__ import annotations

import numpy as np

from scalp_agent.execution import Trade

_DAY_EPOCH_CACHE: dict[str, float] = {}


def _day_epoch(day: str) -> float:
    v = _DAY_EPOCH_CACHE.get(day)
    if v is None:
        v = float(np.datetime64(day).astype("datetime64[s]").astype(np.int64))
        _DAY_EPOCH_CACHE[day] = v
    return v


def _group_bounds(day: np.ndarray, tod: np.ndarray) -> list[tuple[int, int]]:
    """(day, tod) ソート済み前提。"""
    key = np.char.add(np.char.add(day.astype(str), "|"),
                      tod.astype(np.float64).astype(np.int64).astype(str))
    _, first = np.unique(key, return_index=True)
    bounds = np.sort(np.concatenate([first, [len(key)]]))
    return [(int(bounds[i]), int(bounds[i + 1])) for i in range(len(bounds) - 1)]


def _make_trade(data: dict[str, np.ndarray], i: int, h: int, side: int,
                stress: bool = False) -> Trade:
    day = str(data["day"][i])
    tod = float(data["tod"][i])
    t0 = _day_epoch(day) + tod
    gross = side * float(data[f"h{h}_gross_bps"][i])
    fric_col = "friction_stress_bps" if stress else "friction_bps"
    fric = float(data[fric_col][i])
    if side == 1:
        mae = max(0.0, -float(data[f"h{h}_path_min_bps"][i]))
    else:
        mae = max(0.0, float(data[f"h{h}_path_max_bps"][i]))
    entry_px = float(data["entry_px"][i])
    exit_px = entry_px * (1.0 + float(data[f"h{h}_gross_bps"][i]) / 1e4)
    return Trade(
        code=str(data["code"][i]), day=day, side=side,
        decision_ts=t0, entry_ts=t0, exit_ts=t0 + h * 60.0,
        entry_px=entry_px, exit_px=exit_px,
        mid_entry=entry_px, mid_exit=exit_px,
        exit_reason=int(data[f"h{h}_exit_reason"][i]),
        exit_trigger_ts=t0 + h * 60.0,
        mae_bps=mae, gross_bps=gross, friction_bps=fric, net_bps=gross - fric,
    )


def book_trades(
    data: dict[str, np.ndarray], scores: np.ndarray, h: int, k: int,
    mask: np.ndarray, stress: bool = False,
) -> list[Trade]:
    """グループごとに top-K long + bottom-K short。有効数 < 4K のグループはスキップ
    (上下が重なる薄いグループを取らない)。"""
    trades: list[Trade] = []
    day, tod = data["day"], data["tod"]
    for lo, hi in _group_bounds(day, tod):
        idx = np.arange(lo, hi)
        el = mask[lo:hi] & np.isfinite(scores[lo:hi]) \
            & ~data["near_limit"][lo:hi] \
            & np.isfinite(data[f"h{h}_gross_bps"][lo:hi]) \
            & np.isfinite(data["friction_bps"][lo:hi])
        cand = idx[el]
        if len(cand) < 4 * k:
            continue
        sc = scores[cand]
        order = np.argsort(sc, kind="stable")
        for i in cand[order[-k:]]:
            trades.append(_make_trade(data, int(i), h, +1, stress))
        for i in cand[order[:k]]:
            trades.append(_make_trade(data, int(i), h, -1, stress))
    return trades


def null_gap(
    data: dict[str, np.ndarray], h: int, k: int, mask: np.ndarray,
    actual_net_per_entry: float, n_shuffles: int, seed: int = 20260716,
) -> dict:
    """G2: 同日・同時刻・同 K のランダム銘柄ヌル。

    ヌル分布の net/entry と実測の gap、および gap のヌル標準偏差に対する z。
    """
    rng = np.random.default_rng(seed)
    day, tod = data["day"], data["tod"]
    groups = []
    for lo, hi in _group_bounds(day, tod):
        el = mask[lo:hi] \
            & ~data["near_limit"][lo:hi] \
            & np.isfinite(data[f"h{h}_gross_bps"][lo:hi]) \
            & np.isfinite(data["friction_bps"][lo:hi])
        cand = np.arange(lo, hi)[el]
        if len(cand) >= 4 * k:
            groups.append(cand)
    if not groups:
        return {"null_mean": None, "gap": None, "gap_z": None}
    gross = data[f"h{h}_gross_bps"]
    fric = data["friction_bps"]
    means = np.empty(n_shuffles)
    for s in range(n_shuffles):
        nets = []
        for cand in groups:
            pick = rng.choice(cand, size=2 * k, replace=False)
            side = np.concatenate([np.ones(k), -np.ones(k)])
            nets.append(side * gross[pick] - fric[pick])
        means[s] = float(np.mean(np.concatenate(nets)))
    null_mean = float(means.mean())
    null_std = float(means.std(ddof=1))
    gap = actual_net_per_entry - null_mean
    return {
        "null_mean": null_mean, "null_std": null_std, "gap": gap,
        "gap_z": gap / null_std if null_std > 0 else None,
    }


def decile_diagnostics(
    data: dict[str, np.ndarray], scores: np.ndarray, h: int, mask: np.ndarray,
) -> dict:
    """score decile → 実現 adjusted gross (bps) の平均。単調性 = Spearman。"""
    day, tod = data["day"], data["tod"]
    sums = np.zeros(10)
    cnts = np.zeros(10)
    for lo, hi in _group_bounds(day, tod):
        el = mask[lo:hi] & np.isfinite(scores[lo:hi]) \
            & np.isfinite(data[f"h{h}_adj_bps"][lo:hi])
        n = int(el.sum())
        if n < 30:
            continue
        idx = np.arange(lo, hi)[el]
        sc = scores[idx]
        ranks = np.argsort(np.argsort(sc, kind="stable"), kind="stable")
        dec = np.minimum((ranks * 10) // n, 9)
        adj = data[f"h{h}_adj_bps"][idx]
        for d in range(10):
            m = dec == d
            sums[d] += float(adj[m].sum())
            cnts[d] += int(m.sum())
    with np.errstate(invalid="ignore", divide="ignore"):
        means = sums / cnts
    fin = np.isfinite(means)
    if fin.sum() < 10:
        return {"decile_mean_adj_bps": means.tolist(), "spearman": None}
    rk = np.argsort(np.argsort(means))
    d_idx = np.arange(10)
    spearman = float(np.corrcoef(rk, d_idx)[0, 1])
    return {"decile_mean_adj_bps": means.tolist(), "spearman": spearman}
