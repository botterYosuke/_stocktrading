"""1分足 + 前日日足からの特徴量。pure・numpy のみ。板列は一切使わない。

正規化規律は gen1 と同じ: 価格系はすべて現在バー close 分母の bps、
出来高は trailing median 比の無次元、分母が欠損/0 なら NaN (0 に潰さない)。
rolling 統計は現在バーより前の完了バーのみ (因果)・per-day 配列で日初リセット。

バー index 窓 (k 本) は wall-clock ではなくバー本数で数える。取引の無い分は
バー自体が存在しないため、窓が昼休み・閑散区間を跨ぐことがある (仕様)。
"""
from __future__ import annotations

import hashlib

import numpy as np

from scalp_agent.bars.config import VOL_MED_BARS, VOL_MED_MIN_BARS

RET_BARS = (1, 3, 5, 15)
RANGE_BARS = (5, 15)

FEATURE_NAMES: tuple[str, ...] = (
    *(f"ret_{k}b_bps" for k in RET_BARS),
    *(f"range_{k}b_bps" for k in RANGE_BARS),
    *(f"closeloc_{k}b" for k in RANGE_BARS),
    "volr_1b",
    "volr_5b",
    "svwap_delta_bps",
    "open_delta_bps",
    "hi_delta_bps",
    "lo_delta_bps",
    "tod_min",
    "gap_bps",
    "prev_ret_bps",
    "prev_range_bps",
)


def feature_schema_hash() -> str:
    payload = ",".join(FEATURE_NAMES) + "|bars-v1"
    return hashlib.sha256(payload.encode()).hexdigest()


def _shift_ret_bps(close: np.ndarray, k: int) -> np.ndarray:
    out = np.full(len(close), np.nan)
    if len(close) > k:
        ref = close[:-k]
        out[k:] = (close[k:] - ref) / ref * 1e4
    return out


def _rolling_extreme(x: np.ndarray, k: int, kind: str) -> np.ndarray:
    """直近 k 本 (現在バー含む) の max/min。i < k-1 は NaN。"""
    n = len(x)
    out = np.full(n, np.nan)
    if n >= k:
        from numpy.lib.stride_tricks import sliding_window_view

        w = sliding_window_view(x, k)
        out[k - 1:] = w.max(axis=1) if kind == "max" else w.min(axis=1)
    return out


def _trailing_vol_median(vol: np.ndarray) -> np.ndarray:
    """直前 VOL_MED_BARS 本 (現在バー除く) の median。過去 < VOL_MED_MIN_BARS は NaN。"""
    n = len(vol)
    out = np.full(n, np.nan)
    for i in range(VOL_MED_MIN_BARS, n):
        lo = max(0, i - VOL_MED_BARS)
        out[i] = np.median(vol[lo:i])
    return out


def build_bar_features(
    bars: dict[str, np.ndarray], prev_ohlc: dict[str, float] | None
) -> dict[str, np.ndarray]:
    """minute_bars 出力 (1 銘柄 1 日) → FEATURE_NAMES 順の特徴量辞書。全て同一長。"""
    close = bars["close"]
    high, low, vol = bars["high"], bars["low"], bars["vol"]
    n = len(close)
    if n == 0:
        return {k: np.array([]) for k in FEATURE_NAMES}
    feats: dict[str, np.ndarray] = {}
    for k in RET_BARS:
        feats[f"ret_{k}b_bps"] = _shift_ret_bps(close, k)
    for k in RANGE_BARS:
        rhi = _rolling_extreme(high, k, "max")
        rlo = _rolling_extreme(low, k, "min")
        rng = rhi - rlo
        feats[f"range_{k}b_bps"] = rng / close * 1e4
        with np.errstate(invalid="ignore", divide="ignore"):
            loc = (close - rlo) / rng
        feats[f"closeloc_{k}b"] = np.where(rng > 0, loc, np.nan)
    med = _trailing_vol_median(vol)
    with np.errstate(invalid="ignore", divide="ignore"):
        feats["volr_1b"] = np.where(med > 0, vol / med, np.nan)
        v5 = np.full(n, np.nan)
        if n >= 5:
            from numpy.lib.stride_tricks import sliding_window_view

            v5[4:] = sliding_window_view(vol, 5).mean(axis=1)
        feats["volr_5b"] = np.where(med > 0, v5 / med, np.nan)
    typical = (high + low + close) / 3.0
    cum_pv = np.cumsum(typical * vol)
    cum_v = np.cumsum(vol)
    with np.errstate(invalid="ignore", divide="ignore"):
        svwap = cum_pv / cum_v
    feats["svwap_delta_bps"] = np.where(cum_v > 0, (close - svwap) / close * 1e4, np.nan)
    sess_open = bars["open"][0]
    feats["open_delta_bps"] = (close - sess_open) / close * 1e4
    feats["hi_delta_bps"] = (close - np.maximum.accumulate(high)) / close * 1e4
    feats["lo_delta_bps"] = (close - np.minimum.accumulate(low)) / close * 1e4
    feats["tod_min"] = (np.mod(bars["end_ts"], 86400.0) - 9 * 3600.0) / 60.0
    if prev_ohlc is not None and prev_ohlc.get("close", 0) > 0:
        pc, po = prev_ohlc["close"], prev_ohlc["open"]
        feats["gap_bps"] = np.full(n, (sess_open - pc) / pc * 1e4)
        feats["prev_ret_bps"] = np.full(n, (pc - po) / po * 1e4 if po > 0 else np.nan)
        feats["prev_range_bps"] = np.full(
            n, (prev_ohlc["high"] - prev_ohlc["low"]) / pc * 1e4
        )
    else:
        for k in ("gap_bps", "prev_ret_bps", "prev_range_bps"):
            feats[k] = np.full(n, np.nan)
    return {k: feats[k] for k in FEATURE_NAMES}
