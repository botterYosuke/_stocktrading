"""ラベル生成。taker 前提: 「N 秒後の mid がスプレッド+バッファを超えて動くか」。

pure・numpy のみ。将来リーク防止のため、参照するのは t+horizon **以前で最も近い**
スナップショット (未来を跨がない側に丸めると horizon が縮むので、こちらは
「t+horizon 以降で最初の」点を使う。ホライズン終端をまたいだ直後の値 = 実際に
taker が返済できる時点の板に最も近い)。ホライズン内に次データが無い末尾は無効。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class LabelSpec:
    horizon_s: float          # 何秒先を当てるか
    threshold_spread_mult: float  # 発火閾値 = その時点の spread × この倍率


def forward_mid(ts: np.ndarray, mid_px: np.ndarray, horizon_s: float) -> tuple[np.ndarray, np.ndarray]:
    """各 t について t+horizon_s 以降で最初の mid と、その有効マスクを返す。"""
    idx = np.searchsorted(ts, ts + horizon_s, side="left")
    valid = idx < len(ts)
    fwd = mid_px[np.clip(idx, 0, len(ts) - 1)]
    return np.where(valid, fwd, np.nan), valid


def make_labels(
    ts: np.ndarray,
    mid_px: np.ndarray,
    spread_now: np.ndarray,
    spec: LabelSpec,
) -> tuple[np.ndarray, np.ndarray]:
    """3 値ラベル (+1/-1/0) と有効マスク。

    +1: mid が +spread×mult 以上動いた (ロング taker が往復 friction を超える)
    -1: 同、下方向
     0: どちらでもない
    """
    fwd, valid = forward_mid(ts, mid_px, spec.horizon_s)
    move = fwd - mid_px
    thr = spread_now * spec.threshold_spread_mult
    y = np.zeros(len(ts), dtype=np.int8)
    with np.errstate(invalid="ignore"):
        y[valid & (move >= thr)] = 1
        y[valid & (move <= -thr)] = -1
    return y, valid
