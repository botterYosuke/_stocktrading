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


def ofi_level(
    bid_px: np.ndarray,
    bid_qty: np.ndarray,
    ask_px: np.ndarray,
    ask_qty: np.ndarray,
) -> np.ndarray:
    """任意 1 段の OFI (ofi_l1 と同じ規則を段に一般化)。"""
    return ofi_l1(bid_px, bid_qty, ask_px, ask_qty)


def mlofi(snap: dict[str, np.ndarray], levels: int = 5) -> np.ndarray:
    """多段 OFI (MLOFI): 上位 levels 段の OFI 合計。"""
    out = np.zeros(len(snap["ts"]))
    for i in range(1, levels + 1):
        out += ofi_level(
            snap[f"bid_px_{i}"], snap[f"bid_qty_{i}"],
            snap[f"ask_px_{i}"], snap[f"ask_qty_{i}"],
        )
    return out


def trailing_median_1hz(
    ts: np.ndarray,
    x: np.ndarray,
    window_s: float = 300.0,
    min_s: int = 60,
) -> np.ndarray:
    """因果 trailing median。数量系正規化の分母に使う。

    - 1 秒グリッドに「各秒の最終値」を落とし、欠測秒は直前値で前方補完
    - 行 (秒 S 内の PUSH) が参照するのは **秒 S-1 までの完了秒** の直近
      window_s 秒分の median (現在秒・未来は見ない)
    - 経過秒 < min_s は欠損 (NaN)。日初リセットは per-day 配列であることで担保
    """
    n = len(ts)
    if n == 0:
        return np.array([])
    sec = np.floor(ts).astype(np.int64)
    s0 = int(sec[0])
    n_sec = int(sec[-1]) - s0 + 1
    dense = np.full(n_sec, np.nan)
    dense[sec - s0] = x  # 同一秒は後勝ち = 各秒の最終値
    # 前方補完 (因果)
    has = ~np.isnan(dense)
    fill_idx = np.where(has, np.arange(n_sec), 0)
    np.maximum.accumulate(fill_idx, out=fill_idx)
    dense = dense[fill_idx]
    w = int(window_s)
    med = np.full(n_sec, np.nan)
    if n_sec >= w:
        from numpy.lib.stride_tricks import sliding_window_view

        med[w - 1:] = np.median(sliding_window_view(dense, w), axis=1)
    # 部分窓 (立ち上がり): min_s 秒経過以降は expanding median
    upper = min(w - 1, n_sec)
    for j in range(min_s - 1, upper):
        med[j] = np.median(dense[: j + 1])
    # 秒 S の行は index (S - s0 - 1) の median を参照 (= 前秒までの窓)
    q = sec - s0 - 1
    return np.where(q >= 0, med[np.clip(q, 0, None)], np.nan)


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


# ---- 正規化特徴量 (gen1・全銘柄プール用) ------------------------------------
#
# 価格系は mid 比 bps、数量系は trailing median depth 比。分母が欠損/0 の行は
# 0 埋めせず NaN (LightGBM は NaN をネイティブに扱う)。
# rolling median は現在秒より前だけを使い、per-day 配列で日初リセット。

FEATURE_NAMES: tuple[str, ...] = (
    "spread_bps",
    "micro_delta_bps",
    "imb_l1",
    "imb_d5",
    "last_delta_bps",
    "depth_bid_ratio",
    "depth_ask_ratio",
    *(f"ret_{w:g}s_bps" for w in FEATURE_WINDOWS_S),
    *(f"vol_{w:g}s_bps" for w in FEATURE_WINDOWS_S),
    *(f"ofi1_{w:g}s_norm" for w in FEATURE_WINDOWS_S),
    *(f"mlofi_{w:g}s_norm" for w in FEATURE_WINDOWS_S),
)


def feature_schema_hash() -> str:
    """特徴量スキーマの指紋。キャッシュ整合性検査に使う。"""
    import hashlib

    payload = ",".join(FEATURE_NAMES) + "|v1"
    return hashlib.sha256(payload.encode()).hexdigest()


def _safe_ratio(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore", divide="ignore"):
        out = num / den
    return np.where(np.isfinite(den) & (den > 0), out, np.nan)


def build_features_normalized(snap: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """executable 部分列 → FEATURE_NAMES 順の特徴量辞書。全て無次元。"""
    from scalp_agent.config import MEDIAN_MIN_S, MEDIAN_WINDOW_S

    ts = snap["ts"]
    b1, a1 = snap["bid_px_1"], snap["ask_px_1"]
    bq1, aq1 = snap["bid_qty_1"], snap["ask_qty_1"]
    m = mid(b1, a1)
    bid5 = depth_qty(snap, "bid")
    ask5 = depth_qty(snap, "ask")
    med_bid = trailing_median_1hz(ts, bid5, MEDIAN_WINDOW_S, MEDIAN_MIN_S)
    med_ask = trailing_median_1hz(ts, ask5, MEDIAN_WINDOW_S, MEDIAN_MIN_S)
    med_total = trailing_median_1hz(ts, bid5 + ask5, MEDIAN_WINDOW_S, MEDIAN_MIN_S)
    ofi1 = ofi_l1(b1, bq1, a1, aq1)
    ml = mlofi(snap, levels=5)
    feats: dict[str, np.ndarray] = {
        "spread_bps": spread_bps(b1, a1),
        "micro_delta_bps": (microprice(b1, a1, bq1, aq1) - m) / m * 1e4,
        "imb_l1": imbalance(bq1, aq1),
        "imb_d5": imbalance(bid5, ask5),
        "last_delta_bps": (snap["last_px"] - m) / m * 1e4,
        "depth_bid_ratio": _safe_ratio(bid5, med_bid),
        "depth_ask_ratio": _safe_ratio(ask5, med_ask),
    }
    for w in FEATURE_WINDOWS_S:
        feats[f"ret_{w:g}s_bps"] = trailing_return(ts, m, w) / m * 1e4
        feats[f"vol_{w:g}s_bps"] = trailing_realized_vol(ts, m, w) / m * 1e4
        feats[f"ofi1_{w:g}s_norm"] = _safe_ratio(trailing_sum(ts, ofi1, w), med_total)
        feats[f"mlofi_{w:g}s_norm"] = _safe_ratio(trailing_sum(ts, ml, w), med_total)
    return {k: feats[k] for k in FEATURE_NAMES}


def features_matrix(feats: dict[str, np.ndarray]) -> np.ndarray:
    """FEATURE_NAMES 順の (n, k) float64 行列。"""
    return np.column_stack([feats[k] for k in FEATURE_NAMES])
