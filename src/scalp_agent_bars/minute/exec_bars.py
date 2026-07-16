"""保守的バー執行モデル (板なし)。pure・numpy のみ。

板 PUSH が存在しない歴史分足での約定近似。不確実な箇所はすべて戦略に不利へ倒す:

- エントリ: 決定 = バー i の確定時刻。**次バー j=i+1 の始値** + 不利半スプレッドで約定。
  次バーが 1 分後に存在しない (昼休み・欠損・引け) 決定は無効。
- バリア: Δ = ATR20(決定バーまで) × mult。Long TP: high ≥ base+Δ / SL: low ≤ base−Δ。
- バー内の順序は観測できないため:
  * 同一バーで TP と SL の両方に接触 → **SL 扱い**
  * 始値が既にバリアを跨ぐ (ギャップ) → SL は始値 (不利)、TP はバリア価格 (有利は放棄)
  * TP 約定はバリア価格ちょうど (バー内の有利な伸びは取らない)
- timeout: エントリから horizon_bars 本後のバー始値。EOD (14:55) が先ならそのバー始値。
- exit にも不利半スプレッド。friction = スプレッド全額 (往復) がバーごとに成立し、
  gross − friction = net の恒等式は execution.trade_pnl_bps と同一。
- exit_ts はエグジットバーの**確定時刻** (start+60): 同一バー内での再エントリを禁止。
"""
from __future__ import annotations

import numpy as np

from scalp_agent.labels import EXIT_EOD, EXIT_NONE, EXIT_SL, EXIT_TIMEOUT, EXIT_TP

BAR_S = 60.0


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int) -> np.ndarray:
    """True Range の n 本単純移動平均 (現在バー含む・因果)。< n 本は NaN。"""
    m = len(close)
    if m == 0:
        return np.array([])
    pc = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum(high - low, np.maximum(np.abs(high - pc), np.abs(low - pc)))
    cs = np.concatenate([[0.0], np.cumsum(tr)])
    out = np.full(m, np.nan)
    if m >= n:
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def eligible_decisions(
    start_tod: np.ndarray,
    open_: np.ndarray,
    atr_arr: np.ndarray,
    morning: tuple[float, float],
    afternoon: tuple[float, float],
    entry_end_tod: float,
) -> np.ndarray:
    """決定バー index i の配列。エントリバー j=i+1 の存在と窓を保証する。

    - 連続性: start[j] == start[i] + 60 (昼休み・欠損跨ぎのエントリなし)
    - エントリバー開始が前場 [09:01, 11:30) または後場 [12:31, 14:55) 内
      (11:30 前場引け・12:30 寄りの単発バーには入らない)
    - ATR (決定バーまで) が有限かつ正
    """
    n = len(start_tod)
    if n < 2:
        return np.array([], dtype=np.int64)
    i = np.arange(n - 1)
    j_tod = start_tod[i + 1]
    contiguous = j_tod == start_tod[i] + BAR_S
    in_morning = (j_tod >= morning[0] + BAR_S) & (j_tod < morning[1])
    in_afternoon = (j_tod > afternoon[0]) & (j_tod < entry_end_tod)
    ok = (
        contiguous
        & (in_morning | in_afternoon)
        & np.isfinite(atr_arr[i])
        & (atr_arr[i] > 0)
        & (open_[i + 1] > 0)
    )
    return i[ok].astype(np.int64)


def barrier_outcomes_bars(
    ts: np.ndarray,
    start_tod: np.ndarray,
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    didx: np.ndarray,
    atr_arr: np.ndarray,
    spread_bps: float,
    horizon_bars: int,
    atr_mult: float,
    force_close_tod: float,
) -> dict[str, dict[str, np.ndarray]]:
    """全決定行 × 両サイドのバリア解決 (1 セル分)。

    戻り値: {"long"|"short"} → SIDE_FIELDS 互換の配列辞書。
    reason == EXIT_NONE は無効 (データ終端で time exit バーが無い)。
    """
    nd = len(didx)
    n = len(ts)
    H = int(horizon_bars)
    cols = H + 1  # 列 H = timeout バー
    empty = {
        "reason": np.zeros(nd, dtype=np.int8),
        "entry_ts": np.full(nd, np.nan), "exit_ts": np.full(nd, np.nan),
        "entry_px": np.full(nd, np.nan), "exit_px": np.full(nd, np.nan),
        "mid_entry": np.full(nd, np.nan), "mid_exit": np.full(nd, np.nan),
        "tp_trigger_ts": np.full(nd, np.nan), "exit_trigger_ts": np.full(nd, np.nan),
        "mae_bps": np.full(nd, np.nan),
    }
    out = {"long": empty, "short": {k: v.copy() for k, v in empty.items()}}
    if nd == 0:
        return out

    J = didx + 1
    IDX = J[:, None] + np.arange(cols)[None, :]
    valid = IDX < n
    IDXc = np.minimum(IDX, n - 1)
    OP, HI, LO = open_[IDXc], high[IDXc], low[IDXc]
    TOD = start_tod[IDXc]
    rows = np.arange(nd)
    colidx = np.arange(cols)[None, :]

    # EOD: 最初に start >= 14:55 となる列。エントリバーは窓により 14:55 前 → eod_col >= 1
    is_eod = (TOD >= force_close_tod) & valid
    eod_col = np.where(is_eod.any(axis=1), is_eod.argmax(axis=1), cols)
    lim = np.minimum(eod_col, H)          # バリア走査は列 < lim、time exit は列 lim
    time_reason = np.where(eod_col <= H, EXIT_EOD, EXIT_TIMEOUT).astype(np.int8)

    base = open_[J]
    delta = atr_arr[didx] * atr_mult
    half = spread_bps / 2.0 / 1e4

    for side, sgn in (("long", 1.0), ("short", -1.0)):
        tp = base + sgn * delta
        sl = base - sgn * delta
        if side == "long":
            tp_touch = HI >= tp[:, None]
            sl_touch = LO <= sl[:, None]
            gap_sl = OP <= sl[:, None]
        else:
            tp_touch = LO <= tp[:, None]
            sl_touch = HI >= sl[:, None]
            gap_sl = OP >= sl[:, None]
        scan = valid & (colidx < lim[:, None])
        touch = (tp_touch | sl_touch) & scan
        has_touch = touch.any(axis=1)
        c = touch.argmax(axis=1)  # has_touch=False の行では 0 (後でマスク)

        # バー内順序は不可知 → 両接触は SL。ギャップ SL は始値 (さらに不利)。
        t_tp = tp_touch[rows, c]
        t_sl = sl_touch[rows, c]
        is_sl = t_sl  # SL に触れていれば TP 同時でも SL 扱い
        g_sl = gap_sl[rows, c]
        sl_fill = np.where(
            g_sl,
            OP[rows, c],
            sl,
        )
        exit_base_touch = np.where(is_sl, sl_fill, tp)

        # time exit (timeout / EOD): 列 lim のバー始値
        te_valid = valid[rows, lim]
        exit_base_time = OP[rows, lim]

        resolved = has_touch | te_valid
        exit_col = np.where(has_touch, c, lim)
        exit_base = np.where(has_touch, exit_base_touch, exit_base_time)
        reason = np.where(
            has_touch,
            np.where(is_sl, EXIT_SL, EXIT_TP),
            np.where(te_valid, time_reason, EXIT_NONE),
        ).astype(np.int8)

        # MAE: エントリバーから exit バーまでの逆行極値 (bps・正が逆行)
        if side == "long":
            adverse = np.minimum.accumulate(np.where(valid, LO, np.inf), axis=1)
            mae = (base - adverse[rows, exit_col]) / base * 1e4
        else:
            adverse = np.maximum.accumulate(np.where(valid, HI, -np.inf), axis=1)
            mae = (adverse[rows, exit_col] - base) / base * 1e4

        exit_bar = IDXc[rows, exit_col]
        rec = out[side]
        ok = resolved
        rec["reason"][:] = np.where(ok, reason, EXIT_NONE)
        rec["entry_ts"][:] = np.where(ok, ts[J], np.nan)
        rec["exit_ts"][:] = np.where(ok, ts[exit_bar] + BAR_S, np.nan)
        rec["entry_px"][:] = np.where(ok, base * (1.0 + sgn * half), np.nan)
        rec["exit_px"][:] = np.where(ok, exit_base * (1.0 - sgn * half), np.nan)
        rec["mid_entry"][:] = np.where(ok, base, np.nan)
        rec["mid_exit"][:] = np.where(ok, exit_base, np.nan)
        rec["tp_trigger_ts"][:] = np.where(
            ok & (reason == EXIT_TP), ts[exit_bar], np.nan
        )
        rec["exit_trigger_ts"][:] = np.where(ok, ts[exit_bar], np.nan)
        rec["mae_bps"][:] = np.where(ok, np.maximum(mae, 0.0), np.nan)
    return out
