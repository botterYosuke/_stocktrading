"""1分足・日足の構築。pure・numpy のみ。板列 (bid_*/ask_*) は一切参照しない。

入力は executable 部分列の ts / last_px / volume (日中累積)。last_px は
歩み値の最新約定値、volume は日初からの累積出来高という録画仕様を前提とする。
"""
from __future__ import annotations

import numpy as np

from scalp_agent_bars.config import BAR_S

BAR_KEYS = ("end_ts", "open", "high", "low", "close", "vol")


def _empty_bars() -> dict[str, np.ndarray]:
    return {k: np.array([], dtype=np.float64) for k in BAR_KEYS}


def minute_bars(
    ts: np.ndarray, last_px: np.ndarray, volume: np.ndarray
) -> dict[str, np.ndarray]:
    """歩み値 → 1分足。取引 (有効な last_px 更新行) のあった分だけバーを作る。

    - last_px が有限かつ > 0 の行だけを歩み値として使う (欠損・0 は無視)
    - バーの区間は [t, t+60)。end_ts = t+60 = バー確定時刻 (この時刻以降にのみ
      そのバーを特徴量として参照できる)
    - vol はバー内最終累積出来高 − 前バー最終累積出来高。先頭バーは日初からの
      累積をそのまま持つ (寄り成行を含む)。累積の逆行は 0 に切り上げる
    """
    valid = np.isfinite(ts) & np.isfinite(last_px) & (last_px > 0)
    t, px = ts[valid], last_px[valid]
    cum = np.where(np.isfinite(volume[valid]), volume[valid], 0.0)
    if len(t) == 0:
        return _empty_bars()
    minute = np.floor(t / BAR_S).astype(np.int64)
    uniq, first_idx = np.unique(minute, return_index=True)
    last_idx = np.concatenate([first_idx[1:], [len(t)]]) - 1
    cum_end = cum[last_idx]
    vol = np.maximum(np.diff(cum_end, prepend=0.0), 0.0)
    return {
        "end_ts": (uniq + 1).astype(np.float64) * BAR_S,
        "open": px[first_idx],
        "high": np.maximum.reduceat(px, first_idx),
        "low": np.minimum.reduceat(px, first_idx),
        "close": px[last_idx],
        "vol": vol,
    }


def daily_ohlc(ts: np.ndarray, last_px: np.ndarray) -> dict[str, float] | None:
    """1 日分の歩み値 → 日足 OHLC。有効な歩み値が無ければ None。

    プロダクション経路 (dataset.daily_ohlc_all) は duckdb 集計で同じ定義を
    計算する。本関数はテスト・検算用の正本定義。
    """
    valid = np.isfinite(ts) & np.isfinite(last_px) & (last_px > 0)
    if not valid.any():
        return None
    t, px = ts[valid], last_px[valid]
    order = np.argsort(t, kind="stable")
    px = px[order]
    return {
        "open": float(px[0]),
        "high": float(px.max()),
        "low": float(px.min()),
        "close": float(px[-1]),
    }
