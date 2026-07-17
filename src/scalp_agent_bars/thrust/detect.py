"""thrust 検出器と前方経路の測定。pure・numpy のみ (I/O なし・状態は引数と戻り値)。

検出器は issue#1 の `down_thrust_scalp_01._down_thrust_metrics` からの**凍結移植**:

    cum_ret = (close[i] - close[i - lookback]) / close[i - lookback]
    vol_med = median(volume[i-vol_window+1 .. i])
    down: cum_ret <= -0.005  かつ  volume[i] >= 3.0 * vol_med
    up  : cum_ret >= +0.005  かつ  volume[i] >= 3.0 * vol_med   (鏡像・新パラメータ 0)

owner 承認 (2026-07-17 grill-me): 検出器は凍結し、マッチした事象は選び好みせず
全件抽出・全件測定する。パラメータ掃引はしない (G8 honest-N)。

保守側への倒し方 (CLAUDE.md「fill が不確実な箇所は戦略に不利な側へ倒す」):
  - TP 到達判定は **close** 基準 (髭で届いても取らない = 届きにくい側)
  - stop 到達判定は **high/low** 基準 (髭で引っかかる = 引っかかりやすい側)
  - 同一 bar で TP と stop が両方成立した場合は **stop を優先** (不利な側)
  - entry は発火 bar の **次 bar の close** (発火 bar では約定しない = 因果)
  - 日跨ぎなし。経路がその日の最終 bar を超える場合は打ち切る (セッション境界リセット)
"""
from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

# ---- 凍結パラメータ (issue#1 の既定値。掃引禁止) --------------------------------

THRUST_LOOKBACK = 3
THRUST_RET_PCT = 0.005          # 絶対値。down は -、up は +
THRUST_VOL_MULT = 3.0
VOL_MEDIAN_WINDOW = 20

# issue#1 の cover_tp_pct = 7yen / 2380 (STRATEGY_PARAM_COVER_TP_PCT の既定値)
TP_BPS = 29.41
# owner の実トレードが実際に耐えた逆行 (2382 -> 2387 = -5yen / 2381)。
# 「徹底した損切り」を適用した場合の許容幅の実証値であり、掃引パラメータではない。
OWNER_MAE_BPS = 21.0

MAX_HORIZON = 15                # cover_time_stop=12 に余裕を持たせた前方 bar 数

SIDE_DOWN = -1                  # down-thrust -> short
SIDE_UP = +1                    # up-thrust   -> long


def rolling_median(x: np.ndarray, window: int) -> np.ndarray:
    """x[i] に対する median(x[i-window+1 .. i])。i < window-1 は NaN。"""
    x = np.asarray(x, dtype=np.float64)
    out = np.full(x.shape, np.nan, dtype=np.float64)
    if x.size < window:
        return out
    win = sliding_window_view(x, window)
    out[window - 1:] = np.median(win, axis=1)
    return out


def thrust_signals(
    close: np.ndarray,
    volume: np.ndarray,
    side: int,
    lookback: int = THRUST_LOOKBACK,
    ret_pct: float = THRUST_RET_PCT,
    vol_mult: float = THRUST_VOL_MULT,
    vol_window: int = VOL_MEDIAN_WINDOW,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(signal, cum_ret, vol_mult_obs) を返す。全て len(close) の配列。

    side=SIDE_DOWN なら cum_ret <= -ret_pct、SIDE_UP なら cum_ret >= +ret_pct。
    因果: index i の判定は close[..i] / volume[..i] のみを使う。
    """
    close = np.asarray(close, dtype=np.float64)
    volume = np.asarray(volume, dtype=np.float64)
    n = close.size

    cum_ret = np.full(n, np.nan, dtype=np.float64)
    if n > lookback:
        prior = close[:-lookback]
        last = close[lookback:]
        with np.errstate(divide="ignore", invalid="ignore"):
            r = np.where(prior > 0, (last - prior) / prior, np.nan)
        cum_ret[lookback:] = r

    vol_med = rolling_median(volume, vol_window)
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_mult_obs = np.where(vol_med > 0, volume / vol_med, np.nan)

    ok_vol = (vol_med > 0) & (volume >= vol_mult * vol_med)
    if side == SIDE_DOWN:
        ok_ret = cum_ret <= -ret_pct
    elif side == SIDE_UP:
        ok_ret = cum_ret >= ret_pct
    else:
        raise ValueError(f"side は {SIDE_DOWN} か {SIDE_UP}: {side}")

    signal = ok_vol & ok_ret & ~np.isnan(cum_ret)
    return signal, cum_ret, vol_mult_obs


def forward_paths(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    idx: np.ndarray,
    side: int,
    horizon: int = MAX_HORIZON,
) -> dict[str, np.ndarray]:
    """発火 index 群に対する前方経路。entry = close[i+1] (次 bar・因果)。

    返り値 (全て len(idx)):
      entry_px : entry 価格 (close[i+1])
      ret_bps  : (horizon, len(idx)) 各 h での符号付きリターン bps
      mfe_bps  : 経路中の最良 (side 方向・close 基準)
      mae_bps  : 経路中の最悪 (side 方向・**髭** 基準 = 不利側)
      n_bars   : その日の中で実際に測れた bar 数 (打ち切り後)
    """
    close = np.asarray(close, dtype=np.float64)
    high = np.asarray(high, dtype=np.float64)
    low = np.asarray(low, dtype=np.float64)
    idx = np.asarray(idx, dtype=np.int64)
    n = close.size
    m = idx.size

    entry_i = idx + 1
    entry_px = np.where(entry_i < n, close[np.clip(entry_i, 0, n - 1)], np.nan)

    ret = np.full((horizon, m), np.nan, dtype=np.float64)
    # side 方向の含み益は close 基準、逆行は髭基準で測る
    fav = np.full((horizon, m), np.nan, dtype=np.float64)
    adv = np.full((horizon, m), np.nan, dtype=np.float64)
    n_bars = np.zeros(m, dtype=np.int64)

    for h in range(1, horizon + 1):
        j = entry_i + h
        valid = (j < n) & (entry_i < n) & np.isfinite(entry_px)
        jj = np.clip(j, 0, n - 1)
        with np.errstate(divide="ignore", invalid="ignore"):
            r = side * (close[jj] - entry_px) / entry_px * 1e4
            # 逆行の最悪値: short(side=-1) は high、long(side=+1) は low が不利
            worst_px = np.where(side == SIDE_DOWN, high[jj], low[jj])
            a = side * (worst_px - entry_px) / entry_px * 1e4
        ret[h - 1] = np.where(valid, r, np.nan)
        fav[h - 1] = np.where(valid, r, np.nan)
        adv[h - 1] = np.where(valid, a, np.nan)
        n_bars += valid.astype(np.int64)

    with np.errstate(invalid="ignore"):
        mfe = np.nanmax(np.where(np.isnan(fav), -np.inf, fav), axis=0)
        mae = np.nanmin(np.where(np.isnan(adv), np.inf, adv), axis=0)
    mfe = np.where(n_bars > 0, mfe, np.nan)
    mae = np.where(n_bars > 0, mae, np.nan)

    return {
        "entry_px": entry_px,
        "ret_bps": ret,
        "mfe_bps": mfe,
        "mae_bps": mae,
        "n_bars": n_bars,
    }


def race_on_paths(
    ret_close: np.ndarray,
    ret_worst: np.ndarray,
    tp_bps: float,
    stop_bps: float,
) -> np.ndarray:
    """事前計算した前方経路に対する先着判定。+1=TP / -1=stop / 0=timeout。

    ret_close[e, h] : entry から h+1 bar 後の close 基準リターン bps (side 符号込み)
    ret_worst[e, h] : 同 髭 基準の逆行 bps (side 符号込み)

    `first_touch_race` と同一の規約 (TP=close 基準 / stop=髭基準 / 同一 bar は stop
    優先) を、任意の (TP, stop) 格子へ適用できるようにしたもの。両者の一致は
    `test_race_on_paths_matches_first_touch_race` で固定する。
    """
    ret_close = np.asarray(ret_close, dtype=np.float64)
    ret_worst = np.asarray(ret_worst, dtype=np.float64)
    n, h_max = ret_close.shape
    out = np.zeros(n, dtype=np.int64)
    done = np.zeros(n, dtype=bool)
    for h in range(h_max):
        live = (~done) & np.isfinite(ret_close[:, h])
        if not live.any():
            continue
        hit_stop = live & (ret_worst[:, h] <= -stop_bps)
        hit_tp = live & (ret_close[:, h] >= tp_bps) & (~hit_stop)
        out = np.where(hit_stop, -1, np.where(hit_tp, 1, out))
        done = done | hit_stop | hit_tp
    return out


def first_touch_race(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    idx: np.ndarray,
    side: int,
    tp_bps: float = TP_BPS,
    stop_bps: float = OWNER_MAE_BPS,
    horizon: int = MAX_HORIZON,
) -> np.ndarray:
    """TP と stop のどちらに先に触れるか。

    返り値: +1 = TP 先着 / -1 = stop 先着 / 0 = どちらも未達 (time stop)。

    保守側 (CLAUDE.md):
      - TP 到達は close 基準 (髭で届いても取らない)
      - stop 到達は 髭 (short=high / long=low) 基準
      - 同一 bar で両方成立 → **stop 優先** (bar 内順序が不明なので不利側へ)
    """
    close = np.asarray(close, dtype=np.float64)
    high = np.asarray(high, dtype=np.float64)
    low = np.asarray(low, dtype=np.float64)
    idx = np.asarray(idx, dtype=np.int64)
    n = close.size
    m = idx.size

    entry_i = idx + 1
    out = np.zeros(m, dtype=np.int64)
    done = np.zeros(m, dtype=bool)
    valid_entry = entry_i < n
    entry_px = np.where(valid_entry, close[np.clip(entry_i, 0, n - 1)], np.nan)

    for h in range(1, horizon + 1):
        j = entry_i + h
        live = (~done) & (j < n) & valid_entry & np.isfinite(entry_px)
        if not live.any():
            continue
        jj = np.clip(j, 0, n - 1)
        with np.errstate(divide="ignore", invalid="ignore"):
            r_close = side * (close[jj] - entry_px) / entry_px * 1e4
            worst_px = np.where(side == SIDE_DOWN, high[jj], low[jj])
            r_worst = side * (worst_px - entry_px) / entry_px * 1e4
        hit_stop = live & (r_worst <= -stop_bps)
        hit_tp = live & (r_close >= tp_bps) & (~hit_stop)   # stop 優先
        out = np.where(hit_stop, -1, np.where(hit_tp, 1, out))
        done = done | hit_stop | hit_tp
    return out
