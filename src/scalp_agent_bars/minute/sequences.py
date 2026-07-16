"""gen3: 生分足系列 + 静的特徴のテンソル化。pure・numpy のみ。

決定バー i に対し、当日内の直近 SEQ_LEN 本 (バー i を含む・過去方向) を
左ゼロパディング + mask チャネル付きで返す。未来バーは構造上参照できない
(index 行列が didx から過去方向にしか伸びない)。
"""
from __future__ import annotations

import numpy as np

from scalp_agent_bars.minute.nn_config import SEQ_CHANNELS, SEQ_LEN, STATIC_FEATURES

VOL_MED_BARS = 30
VOL_MED_MIN_BARS = 10


def _clip_scale(x: np.ndarray, lo: float, hi: float, scale: float) -> np.ndarray:
    return np.clip(x, lo, hi) / scale


def _trailing_vol_median(vol: np.ndarray) -> np.ndarray:
    """直前 VOL_MED_BARS 本 (現在バー除く) の median。過去 < min 本は NaN。
    (gen1b features と同じ定義)"""
    n = len(vol)
    out = np.full(n, np.nan)
    for i in range(VOL_MED_MIN_BARS, n):
        out[i] = np.median(vol[max(0, i - VOL_MED_BARS):i])
    return out


def day_channel_matrix(bars_day: dict[str, np.ndarray]) -> np.ndarray:
    """1 日分のバー → (n_bars, len(SEQ_CHANNELS)-1) float32 (mask 以外の 7ch)。"""
    o, h, l, c = (bars_day[k] for k in ("open", "high", "low", "close"))
    vol = bars_day["vol"]
    n = len(c)
    ret1 = np.zeros(n)
    if n > 1:
        ret1[1:] = (c[1:] / c[:-1] - 1.0) * 1e4
    rng = (h - l) / c * 1e4
    body = (c - o) / c * 1e4
    upper = (h - np.maximum(o, c)) / c * 1e4
    lower = (np.minimum(o, c) - l) / c * 1e4
    med = _trailing_vol_median(vol)
    with np.errstate(invalid="ignore", divide="ignore"):
        volr = np.log1p(vol / med)
    volr = np.where(np.isfinite(volr), volr, 0.0)
    tod = (bars_day["start_tod"] - 9 * 3600.0) / 60.0 / 390.0
    ch = np.column_stack([
        _clip_scale(ret1, -50, 50, 50.0),
        _clip_scale(rng, 0, 100, 100.0),
        _clip_scale(body, -50, 50, 50.0),
        _clip_scale(upper, 0, 50, 50.0),
        _clip_scale(lower, 0, 50, 50.0),
        _clip_scale(volr, 0, 5, 5.0),
        tod,
    ]).astype(np.float32)
    assert ch.shape[1] == len(SEQ_CHANNELS) - 1
    return ch


def day_sequences(
    bars_day: dict[str, np.ndarray],
    didx: np.ndarray,
    atr_arr: np.ndarray,
    prev_ohlc: dict[str, float] | None,
) -> tuple[np.ndarray, np.ndarray]:
    """決定バー index 列 → (seq (nd,K,8) float32, sta (nd,4) float32)。

    seq[d, k] = バー didx[d]-K+1+k のチャネル。当日外 (負 index) はゼロ + mask=0。
    """
    nd = len(didx)
    K = SEQ_LEN
    ch = day_channel_matrix(bars_day)
    n = len(bars_day["close"])
    idx = didx[:, None] - (K - 1) + np.arange(K)[None, :]
    valid = idx >= 0
    idx_c = np.clip(idx, 0, n - 1)
    seq = np.zeros((nd, K, len(SEQ_CHANNELS)), dtype=np.float32)
    seq[:, :, :-1] = np.where(valid[:, :, None], ch[idx_c], 0.0)
    seq[:, :, -1] = valid.astype(np.float32)

    sess_open = bars_day["open"][0]
    if prev_ohlc is not None and prev_ohlc.get("close", 0) > 0:
        pc, po = prev_ohlc["close"], prev_ohlc["open"]
        gap = (sess_open - pc) / pc * 1e4
        pret = (pc - po) / po * 1e4 if po > 0 else 0.0
        prng = (prev_ohlc["high"] - prev_ohlc["low"]) / pc * 1e4
    else:
        gap = pret = prng = 0.0
    with np.errstate(invalid="ignore", divide="ignore"):
        atr_bps = atr_arr[didx] / bars_day["close"][didx] * 1e4
    atr_bps = np.where(np.isfinite(atr_bps), atr_bps, 0.0)
    sta = np.column_stack([
        np.full(nd, _clip_scale(np.array([gap]), -100, 100, 100.0)[0]),
        np.full(nd, _clip_scale(np.array([pret]), -300, 300, 300.0)[0]),
        np.full(nd, _clip_scale(np.array([prng]), 0, 500, 500.0)[0]),
        _clip_scale(atr_bps, 0, 100, 100.0),
    ]).astype(np.float32)
    assert sta.shape[1] == len(STATIC_FEATURES)
    return seq, sta
