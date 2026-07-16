"""gen4 friction モデル。pure。

板実測が無い 300+ 銘柄に対する保守的スプレッド推定:
  spread_bps = max(呼値1tick / price × 1e4, SPREAD_FLOOR_BPS)
  friction_bps = spread_bps × FRICTION_SAFETY + FRICTION_FLAT_BPS   (往復)

呼値は**非 TOPIX100 の一般ラダー**を全銘柄へ適用する。2022-11 以降 TOPIX100、
2024-11 以降 TOPIX500 相当は実際にはより細かい呼値のため、本モデルは流動性上位
銘柄で friction を過大評価する (= 戦略に不利な側。プロジェクト規約どおり)。
gen2 の 17 銘柄板実測 (1.5〜5.3bps median) との照合は pipeline の calibrate で行う。

stress (G6): spread 4 tick 分 + 10bps。
"""
from __future__ import annotations

import numpy as np

from scalp_agent_bars.xsec.config import (
    FRICTION_FLAT_BPS,
    FRICTION_SAFETY,
    SPREAD_FLOOR_BPS,
)

# JPX 一般 (非 TOPIX100) 呼値ラダー: (価格上限, tick)
_TICK_LADDER: tuple[tuple[float, float], ...] = (
    (3_000, 1),
    (5_000, 5),
    (30_000, 10),
    (50_000, 50),
    (300_000, 100),
    (500_000, 500),
    (3_000_000, 1_000),
    (5_000_000, 5_000),
    (30_000_000, 10_000),
    (50_000_000, 50_000),
    (float("inf"), 100_000),
)


def tick_size(price: np.ndarray | float) -> np.ndarray:
    """価格 → 一般ラダーの呼値。vectorized。"""
    p = np.asarray(price, dtype=np.float64)
    out = np.full(p.shape, _TICK_LADDER[-1][1], dtype=np.float64)
    for upper, tick in reversed(_TICK_LADDER):
        out = np.where(p <= upper, tick, out)
    return out


def spread_bps_model(price: np.ndarray | float) -> np.ndarray:
    p = np.asarray(price, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        raw = tick_size(p) / p * 1e4
    return np.maximum(raw, SPREAD_FLOOR_BPS)


def friction_bps(price: np.ndarray | float) -> np.ndarray:
    """往復 friction (bps)。taker 両側 = full spread 1 本分をモデルで見る。"""
    return spread_bps_model(price) * FRICTION_SAFETY + FRICTION_FLAT_BPS


def friction_bps_stress(price: np.ndarray | float) -> np.ndarray:
    """G6 stress: 4 tick + 10bps。"""
    p = np.asarray(price, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        four_ticks = 4.0 * tick_size(p) / p * 1e4
    return four_ticks + 10.0
