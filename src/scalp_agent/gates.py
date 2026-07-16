"""ADR-0001 準拠のメトリクス計算。pure。

主指標: net_per_entry / ratio (mean gross ÷ mean friction) / t_net (日クラスタ) /
hit_rate。併記: n, D, 集中度 (単一日・単一銘柄シェア), 左裾 (worst, p1, MAE)。

3 営業日構成では G4 (n≥30 かつ D≥20) を満たせないため、判定は常に
EVALUATION-INCOMPLETE。PASS やエッジ確認とは呼ばない (2026-07-16 確定)。
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np

from scalp_agent.execution import Trade


def _cluster_t(net: np.ndarray, days: list[str]) -> float | None:
    """日クラスタ t 値。クラスタ数 < 2 なら None (1 日 OOS では定義不能)。"""
    uniq = sorted(set(days))
    if len(uniq) < 2:
        return None
    day_means = np.array([np.mean([n for n, d in zip(net, days) if d == u]) for u in uniq])
    se = day_means.std(ddof=1) / np.sqrt(len(uniq))
    return float(day_means.mean() / se) if se > 0 else None


def trade_metrics(trades: list[Trade]) -> dict:
    """取引列 → ADR-0001 採点フォーマットの数値一式。"""
    n = len(trades)
    if n == 0:
        return {"n": 0, "D": 0, "net_per_entry": None, "gross_per_entry": None,
                "friction_per_entry": None, "ratio": None, "t_net": None,
                "hit_rate": None, "max_day_share": None, "max_code_share": None,
                "worst_bps": None, "p1_bps": None, "mae_median_bps": None,
                "mae_p10_bps": None, "by_code": {}, "by_day": {},
                "exit_reasons": {}, "long_n": 0, "short_n": 0}
    net = np.array([t.net_bps for t in trades])
    gross = np.array([t.gross_bps for t in trades])
    fric = np.array([t.friction_bps for t in trades])
    mae = np.array([t.mae_bps for t in trades])
    days = [t.day for t in trades]
    codes = [t.code for t in trades]

    by_day: dict[str, float] = defaultdict(float)
    by_code: dict[str, float] = defaultdict(float)
    by_code_n: dict[str, int] = defaultdict(int)
    for t in trades:
        by_day[t.day] += t.net_bps
        by_code[t.code] += t.net_bps
        by_code_n[t.code] += 1
    total_net = float(net.sum())

    def share(parts: dict[str, float]) -> float | None:
        if total_net == 0:
            return None
        return max(abs(v) for v in parts.values()) / abs(total_net)

    reasons: dict[int, int] = defaultdict(int)
    for t in trades:
        reasons[t.exit_reason] += 1

    mean_fric = float(fric.mean())
    return {
        "n": n,
        "D": len(set(days)),
        "net_per_entry": float(net.mean()),
        "gross_per_entry": float(gross.mean()),
        "friction_per_entry": mean_fric,
        "ratio": float(gross.mean() / mean_fric) if mean_fric > 0 else None,
        "t_net": _cluster_t(net, days),
        "hit_rate": float((net > 0).mean()),
        "max_day_share": share(by_day),
        "max_code_share": share(by_code),
        "worst_bps": float(net.min()),
        "p1_bps": float(np.percentile(net, 1)),
        "mae_median_bps": float(np.nanmedian(mae)),
        "mae_p10_bps": float(np.nanpercentile(mae, 90)),  # 逆行が大きい側の 10%
        "by_code": {c: {"n": by_code_n[c], "net_sum_bps": by_code[c]} for c in sorted(by_code)},
        "by_day": dict(sorted(by_day.items())),
        "exit_reasons": dict(sorted(reasons.items())),
        "long_n": sum(1 for t in trades if t.side == 1),
        "short_n": sum(1 for t in trades if t.side == -1),
    }


def is_candidate(m: dict, min_n: int, min_ratio: float) -> bool:
    """07-13 IS 検証での候補条件: n ≥ 100, net/entry > 0, ratio ≥ 3。"""
    return (
        m["n"] >= min_n
        and m["net_per_entry"] is not None
        and m["net_per_entry"] > 0
        and m["ratio"] is not None
        and m["ratio"] >= min_ratio
    )


def select_frozen_cell(
    results: dict[tuple[float, float, float], dict],
    min_n: int,
    min_ratio: float,
) -> tuple[float, float, float] | None:
    """候補から凍結セルを 1 つ選ぶ。net/entry 最大、同値なら
    n 多 → horizon 短 → mult 小 → τ 高。候補ゼロなら None (IS-KILL)。"""
    cands = [(k, m) for k, m in results.items() if is_candidate(m, min_n, min_ratio)]
    if not cands:
        return None
    def sort_key(item: tuple[tuple[float, float, float], dict]):
        (h, mu, tau), m = item
        return (-m["net_per_entry"], -m["n"], h, mu, -tau)
    cands.sort(key=sort_key)
    return cands[0][0]
