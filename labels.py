"""FEP-based intraday labels y_buy / y_sell (jpx_mlbot_15m, #8 Phase 0b / C6).

Faithful port of the crypto tutorial's label block (``example/tutorial.ipynb``)
to JP-equity 15-minute bars. The tutorial models a *limit-maker* execution and
takes the realised return as the regression target:

  1. quote a limit ``ATR*atr_mult`` away from close (buy below / sell above),
  2. if the next bar trades through it the limit fills,
  3. ``horizon`` bars later, exit via Force Entry Price (chase with a limit),
  4. y = exit/entry - 1 - round_trip_cost.

The ONE JP-equity change vs the tutorial: the crypto ``2 * fee`` term (which
could be a *negative* maker rebate) is replaced by ``cost_model`` — a strictly
positive round-trip cost (commission + slippage, plus 貸株料 for SHORT). This
is what makes the friction-floor kill gate (#8 C9 / R3) bite.

``calc_force_entry_price`` is numba-jitted when numba is importable (production),
and falls back to identical pure-Python when it is not (so labels are testable
on machines without a numba/llvmlite toolchain). Output columns mirror the
tutorial: ``buy_price, sell_price, buy_fep, buy_fet, sell_fep, sell_fet,
buy_executed, sell_executed, y_buy, y_sell, buy_cost, sell_cost``.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import talib

try:  # numba is optional: jit in production, pure-Python fallback otherwise.
    from numba import njit
except Exception:  # pragma: no cover - exercised only on toolchain-less envs
    def njit(*args, **kwargs):
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]

        def _decorate(func):
            return func

        return _decorate


@njit
def calc_force_entry_price(entry_price, lo, pips):
    """Force Entry Price: chase ``entry_price`` with a limit until it fills.

    For each i, scan forward for the first j>i whose low trades through the
    standing limit ``entry_price[j-1]``; record that fill price (``y``) and the
    bars waited (``force_entry_time``). NaN if never filled. Verbatim from the
    tutorial; negate inputs/output to reuse it for the sell side.
    """
    y = entry_price.copy()
    y[:] = np.nan
    force_entry_time = entry_price.copy()
    force_entry_time[:] = np.nan
    for i in range(entry_price.size):
        for j in range(i + 1, entry_price.size):
            if round(lo[j] / pips) < round(entry_price[j - 1] / pips):
                y[i] = entry_price[j - 1]
                force_entry_time[i] = j - i
                break
    return y, force_entry_time


def _one_way_cost_vec(cost_model, prices: np.ndarray) -> np.ndarray:
    """Vectorized per-side cost fraction (commission + slippage) at ``prices``.

    Reuses ``cost_model.params`` as the single source of the tick table /
    commission so it stays in lock-step with the scalar ``CostModel`` (an
    equivalence test pins them together). Non-finite / non-positive prices map
    to NaN; they are masked out by ``*_executed`` downstream.
    """
    p = np.asarray(prices, dtype="float64")
    uppers = np.array([u for u, _ in cost_model.params.tick_table], dtype="float64")
    ticks = np.array([t for _, t in cost_model.params.tick_table], dtype="float64")
    out = np.full(p.shape, np.nan, dtype="float64")
    valid = np.isfinite(p) & (p > 0)
    idx = np.searchsorted(uppers, p[valid], side="left")
    idx = np.clip(idx, 0, len(ticks) - 1)
    slippage = cost_model.params.slippage_ticks * ticks[idx] / p[valid]
    out[valid] = cost_model.params.commission_rate + slippage
    return out


def compute_labels(
    df: pd.DataFrame,
    cost_model,
    *,
    pips: float = 1.0,
    atr_mult: float = 0.5,
    atr_period: int = 14,
    horizon: int = 1,
    holding_days: float = 1.0 / 24.0,
) -> pd.DataFrame:
    """Add FEP execution labels to a single instrument's 15-min OHLCV frame.

    ``df`` columns: ``open, high, low, close, volume``. Returns ``df`` with the
    tutorial's label columns added. ``cost_model`` supplies the JP round-trip
    cost in place of the crypto maker fee. ``holding_days`` only affects the
    SHORT 貸株料 term (negligible intraday, exposed for sensitivity).
    """
    high = df["high"].astype("float64")
    low = df["low"].astype("float64")
    close = df["close"].astype("float64")

    atr = talib.ATR(high, low, close, timeperiod=atr_period)
    limit_price_dist = atr * atr_mult
    limit_price_dist = np.maximum(1.0, (limit_price_dist / pips).round().fillna(1)) * pips

    df["buy_price"] = close - limit_price_dist
    df["sell_price"] = close + limit_price_dist

    df["buy_fep"], df["buy_fet"] = calc_force_entry_price(
        entry_price=df["buy_price"].values, lo=df["low"].values, pips=pips
    )
    # negate inputs/output to reuse the same routine for the sell side (uses high)
    sell_fep, df["sell_fet"] = calc_force_entry_price(
        entry_price=(-df["sell_price"]).values, lo=(-df["high"]).values, pips=pips
    )
    df["sell_fep"] = -sell_fep

    # fill indicators: did the NEXT bar trade through the limit? (tutorial parity)
    df["buy_executed"] = (
        (df["buy_price"] / pips).round() > (df["low"].shift(-1) / pips).round()
    ).astype("float64")
    df["sell_executed"] = (
        (df["sell_price"] / pips).round() < (df["high"].shift(-1) / pips).round()
    ).astype("float64")

    # JP round-trip cost replaces the crypto `2 * fee` (no maker rebate).
    buy_one_way = _one_way_cost_vec(cost_model, df["buy_price"].values)
    sell_one_way = _one_way_cost_vec(cost_model, df["sell_price"].values)
    buy_round_trip = 2.0 * buy_one_way
    borrow = cost_model.params.borrow_fee_annual * (holding_days / 365.0)
    sell_round_trip = 2.0 * sell_one_way + borrow  # SHORT bears 貸株料

    df["y_buy"] = np.where(
        df["buy_executed"].astype(bool),
        df["sell_fep"].shift(-horizon) / df["buy_price"] - 1.0 - buy_round_trip,
        0.0,
    )
    df["y_sell"] = np.where(
        df["sell_executed"].astype(bool),
        -(df["buy_fep"].shift(-horizon) / df["sell_price"] - 1.0) - sell_round_trip,
        0.0,
    )

    # per-bar entry cost for the backtester (C9): entry slippage + one-way cost.
    df["buy_cost"] = np.where(
        df["buy_executed"].astype(bool),
        df["buy_price"] / close - 1.0 + buy_one_way,
        0.0,
    )
    df["sell_cost"] = np.where(
        df["sell_executed"].astype(bool),
        -(df["sell_price"] / close - 1.0) + sell_one_way,
        0.0,
    )
    return df
