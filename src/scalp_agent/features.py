"""板スナップショット列からの特徴量。すべて pure・numpy ベクトル演算のみ。

オフライン学習とライブランタイムが同じ関数を共有する。ライブ側は逐次
スナップショットを追記した配列に対して末尾 1 点だけ評価してもよい。
時刻は epoch 秒 (float64)、配列は ts 昇順を前提とする。
"""
from __future__ import annotations

import numpy as np


def mid(bid_px_1: np.ndarray, ask_px_1: np.ndarray) -> np.ndarray:
    return (bid_px_1 + ask_px_1) / 2.0


def spread(bid_px_1: np.ndarray, ask_px_1: np.ndarray) -> np.ndarray:
    """絶対スプレッド (円)。taker の往復 friction そのもの。"""
    return ask_px_1 - bid_px_1


def spread_bps(bid_px_1: np.ndarray, ask_px_1: np.ndarray) -> np.ndarray:
    return (ask_px_1 - bid_px_1) / mid(bid_px_1, ask_px_1) * 1e4


def microprice(
    bid_px_1: np.ndarray,
    ask_px_1: np.ndarray,
    bid_qty_1: np.ndarray,
    ask_qty_1: np.ndarray,
) -> np.ndarray:
    """サイズ加重ミッド。厚い側と反対方向に寄る短期フェアバリュー近似。"""
    denom = bid_qty_1 + ask_qty_1
    with np.errstate(invalid="ignore", divide="ignore"):
        mp = (bid_px_1 * ask_qty_1 + ask_px_1 * bid_qty_1) / denom
    return np.where(denom > 0, mp, mid(bid_px_1, ask_px_1))


def imbalance(bid_qty: np.ndarray, ask_qty: np.ndarray) -> np.ndarray:
    """(買い − 売り) / (買い + 売り)。単段でも深さ合算でも使う。[-1, 1]。"""
    denom = bid_qty + ask_qty
    with np.errstate(invalid="ignore", divide="ignore"):
        oi = (bid_qty - ask_qty) / denom
    return np.where(denom > 0, oi, 0.0)


def depth_qty(snap: dict[str, np.ndarray], side: str, levels: int = 5) -> np.ndarray:
    """side='bid'/'ask' の上位 levels 段の数量合計。"""
    return sum(snap[f"{side}_qty_{i}"] for i in range(1, levels + 1))


def ofi_l1(
    bid_px_1: np.ndarray,
    bid_qty_1: np.ndarray,
    ask_px_1: np.ndarray,
    ask_qty_1: np.ndarray,
) -> np.ndarray:
    """L1 Order Flow Imbalance (Cont-Kukanov-Stoikov)。先頭要素は 0。

    bid 側: 価格上昇→+qty、同値→Δqty、下落→-前qty。ask 側は符号反転で対称。
    """
    n = len(bid_px_1)
    out = np.zeros(n)
    if n < 2:
        return out
    b_up = bid_px_1[1:] > bid_px_1[:-1]
    b_dn = bid_px_1[1:] < bid_px_1[:-1]
    e_bid = np.where(b_up, bid_qty_1[1:], np.where(b_dn, -bid_qty_1[:-1], bid_qty_1[1:] - bid_qty_1[:-1]))
    a_up = ask_px_1[1:] > ask_px_1[:-1]
    a_dn = ask_px_1[1:] < ask_px_1[:-1]
    e_ask = np.where(a_dn, ask_qty_1[1:], np.where(a_up, -ask_qty_1[:-1], ask_qty_1[1:] - ask_qty_1[:-1]))
    out[1:] = e_bid - e_ask
    return out


def trailing_return(ts: np.ndarray, px: np.ndarray, window_s: float) -> np.ndarray:
    """window_s 秒前 (以前で最も近い点) からの変化。データ不足の先頭は 0。"""
    idx = np.searchsorted(ts, ts - window_s, side="right") - 1
    valid = idx >= 0
    ref = px[np.clip(idx, 0, None)]
    return np.where(valid, px - ref, 0.0)


def trailing_sum(ts: np.ndarray, x: np.ndarray, window_s: float) -> np.ndarray:
    """過去 window_s 秒の合計 (自分を含む)。OFI の窓集計などに使う。"""
    csum = np.concatenate([[0.0], np.cumsum(x)])
    start = np.searchsorted(ts, ts - window_s, side="left")
    return csum[np.arange(len(x)) + 1] - csum[start]


def trailing_realized_vol(ts: np.ndarray, mid_px: np.ndarray, window_s: float) -> np.ndarray:
    """過去 window_s 秒の |Δmid| 合計 (絶対変動量)。短期活性度の代理。"""
    dm = np.abs(np.diff(mid_px, prepend=mid_px[0]))
    return trailing_sum(ts, dm, window_s)


FEATURE_WINDOWS_S = (5.0, 30.0, 120.0)


def build_features(snap: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """loader.load_symbol_day の出力 → 特徴量名→配列。全て同一長。"""
    ts = snap["ts"]
    b1, a1 = snap["bid_px_1"], snap["ask_px_1"]
    bq1, aq1 = snap["bid_qty_1"], snap["ask_qty_1"]
    m = mid(b1, a1)
    ofi = ofi_l1(b1, bq1, a1, aq1)
    feats: dict[str, np.ndarray] = {
        "spread": spread(b1, a1),
        "spread_bps": spread_bps(b1, a1),
        "micro_minus_mid": microprice(b1, a1, bq1, aq1) - m,
        "imb_l1": imbalance(bq1, aq1),
        "imb_d5": imbalance(depth_qty(snap, "bid"), depth_qty(snap, "ask")),
        "last_minus_mid": snap["last_px"] - m,
    }
    for w in FEATURE_WINDOWS_S:
        feats[f"ret_{w:g}s"] = trailing_return(ts, m, w)
        feats[f"ofi_{w:g}s"] = trailing_sum(ts, ofi, w)
        feats[f"vol_{w:g}s"] = trailing_realized_vol(ts, m, w)
    return feats
