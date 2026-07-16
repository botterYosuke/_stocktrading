"""gen2 特徴量。pure・numpy のみ。

- OWN (P1/P2/P3 共通・19 個): gen1b の 18 特徴 (scalp_agent_bars.features) + atr_bps
- CROSS (P2/P3・7 個): ユニバース平均リターン (leave-self-out)・ブレス・
  市場相対リターン・最相関ペアのリターン
  すべて「同じ分ラベルのバーが確定した銘柄」だけで as-of 計算 (lookahead なし)。
"""
from __future__ import annotations

import hashlib

import numpy as np

from scalp_agent_bars.features import FEATURE_NAMES as GEN1B_FEATURE_NAMES
from scalp_agent_bars.features import build_bar_features

OWN_FEATURE_NAMES: tuple[str, ...] = (*GEN1B_FEATURE_NAMES, "atr_bps")

CROSS_FEATURE_NAMES: tuple[str, ...] = (
    "mkt_ret_1b_bps",
    "mkt_ret_5b_bps",
    "mkt_ret_15b_bps",
    "mkt_breadth_1b",
    "rel_ret_1b_bps",
    "peer_ret_1b_bps",
    "peer_ret_5b_bps",
)

ALL_FEATURE_NAMES: tuple[str, ...] = (*OWN_FEATURE_NAMES, *CROSS_FEATURE_NAMES)

CROSS_MIN_NAMES = 5  # 市場特徴に必要な最小銘柄数 (自分を除く)


def feature_schema_hash() -> str:
    payload = ",".join(ALL_FEATURE_NAMES) + "|gen2-minute-v1"
    return hashlib.sha256(payload.encode()).hexdigest()


def _ret_bps(close: np.ndarray, k: int) -> np.ndarray:
    out = np.full(len(close), np.nan)
    if len(close) > k:
        ref = close[:-k]
        out[k:] = (close[k:] - ref) / ref * 1e4
    return out


def build_own_features(
    bars_day: dict[str, np.ndarray],
    prev_ohlc: dict[str, float] | None,
    atr_arr: np.ndarray,
) -> dict[str, np.ndarray]:
    """1 銘柄 1 日のバー配列 → OWN_FEATURE_NAMES 順の辞書 (バー数と同一長)。"""
    bars = {
        "end_ts": bars_day["ts"] + 60.0,
        "open": bars_day["open"],
        "high": bars_day["high"],
        "low": bars_day["low"],
        "close": bars_day["close"],
        "vol": bars_day["vol"],
    }
    feats = build_bar_features(bars, prev_ohlc)
    with np.errstate(invalid="ignore", divide="ignore"):
        feats["atr_bps"] = atr_arr / bars_day["close"] * 1e4
    return {k: feats[k] for k in OWN_FEATURE_NAMES}


def day_cross_features(
    per_code: dict[str, dict[str, np.ndarray]],
    peer_map: dict[str, str],
    min_names: int = CROSS_MIN_NAMES,
) -> dict[str, dict[str, np.ndarray]]:
    """1 日分・全銘柄のバーからクロス特徴を作る。

    per_code[c] = {"minute": バー開始分 (int64・日内一意・昇順), "close": 終値}
    戻り値: code → CROSS_FEATURE_NAMES の辞書 (その code のバー数と同一長)。
    """
    rets: dict[str, dict[int, np.ndarray]] = {}
    for c, d in per_code.items():
        rets[c] = {k: _ret_bps(d["close"], k) for k in (1, 5, 15)}

    all_min = np.unique(np.concatenate([d["minute"] for d in per_code.values()]))
    codes = sorted(per_code)
    nm, nc = len(all_min), len(codes)
    mats = {k: np.full((nm, nc), np.nan) for k in (1, 5, 15)}
    for ci, c in enumerate(codes):
        pos = np.searchsorted(all_min, per_code[c]["minute"])
        for k in (1, 5, 15):
            mats[k][pos, ci] = rets[c][k]

    sums = {k: np.nansum(m, axis=1) for k, m in mats.items()}
    cnts = {k: np.sum(np.isfinite(m), axis=1) for k, m in mats.items()}
    pos1 = np.nansum(np.where(np.isfinite(mats[1]), mats[1] > 0, 0.0), axis=1)

    out: dict[str, dict[str, np.ndarray]] = {}
    for ci, c in enumerate(codes):
        pos = np.searchsorted(all_min, per_code[c]["minute"])
        own = {k: rets[c][k] for k in (1, 5, 15)}
        feats: dict[str, np.ndarray] = {}
        for k, name in ((1, "mkt_ret_1b_bps"), (5, "mkt_ret_5b_bps"), (15, "mkt_ret_15b_bps")):
            own_fin = np.isfinite(own[k])
            s = sums[k][pos] - np.where(own_fin, own[k], 0.0)
            n = cnts[k][pos] - own_fin.astype(np.int64)
            with np.errstate(invalid="ignore", divide="ignore"):
                mean = s / n
            feats[name] = np.where(n >= min_names, mean, np.nan)
        own1_fin = np.isfinite(own[1])
        pos_cnt = pos1[pos] - np.where(own1_fin & (own[1] > 0), 1.0, 0.0)
        n1 = cnts[1][pos] - own1_fin.astype(np.int64)
        with np.errstate(invalid="ignore", divide="ignore"):
            breadth = pos_cnt / n1
        feats["mkt_breadth_1b"] = np.where(n1 >= min_names, breadth, np.nan)
        feats["rel_ret_1b_bps"] = own[1] - feats["mkt_ret_1b_bps"]
        peer = peer_map.get(c)
        if peer is not None and peer in per_code:
            pi = codes.index(peer)
            feats["peer_ret_1b_bps"] = mats[1][pos, pi]
            feats["peer_ret_5b_bps"] = mats[5][pos, pi]
        else:
            feats["peer_ret_1b_bps"] = np.full(len(pos), np.nan)
            feats["peer_ret_5b_bps"] = np.full(len(pos), np.nan)
        out[c] = {k: feats[k] for k in CROSS_FEATURE_NAMES}
    return out


def compute_peer_map(daily_closes: dict[str, dict[str, float]]) -> dict[str, str]:
    """train 窓の日次終値から、銘柄ごとの最相関 (絶対値) ペアを決める。pure。

    daily_closes: code → {day: close}。共通日 < 60 のペアは対象外。
    """
    codes = sorted(daily_closes)
    rets: dict[str, dict[str, float]] = {}
    for c in codes:
        days = sorted(daily_closes[c])
        r = {}
        for a, b in zip(days[:-1], days[1:]):
            pc = daily_closes[c][a]
            if pc > 0:
                r[b] = daily_closes[c][b] / pc - 1.0
        rets[c] = r
    peer: dict[str, str] = {}
    for c in codes:
        best, best_corr = None, -1.0
        for other in codes:
            if other == c:
                continue
            common = sorted(set(rets[c]) & set(rets[other]))
            if len(common) < 60:
                continue
            x = np.array([rets[c][d] for d in common])
            y = np.array([rets[other][d] for d in common])
            if x.std() == 0 or y.std() == 0:
                continue
            corr = abs(float(np.corrcoef(x, y)[0, 1]))
            if corr > best_corr:
                best, best_corr = other, corr
        if best is not None:
            peer[c] = best
    return peer
