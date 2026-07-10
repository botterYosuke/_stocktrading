from __future__ import annotations

from stocktrading.backtest import CostParams, Tick, run_backtest
from stocktrading.signals import SignalParams


def _tick(bid: float, ask: float, imb: float | None) -> Tick:
    return Tick(ts_event=None, bid_px=bid, ask_px=ask, mid=(bid + ask) / 2, imbalance=imb)


def test_run_backtest_round_trip_no_cost() -> None:
    # t0: go long (buy @ ask=101). t1 is last tick -> forced flat (sell @ bid=102).
    ticks = [_tick(100, 101, 1.0), _tick(102, 103, 0.0)]
    result = run_backtest(
        ticks,
        symbol="TEST",
        signal_params=SignalParams(threshold=0.3),
        cost_params=CostParams(commission_bps=0.0, lot=100, fill_delay_ticks=0),
    )
    assert result.n_fills == 2
    assert result.gross_pnl == 100.0  # (102 - 101) * 100 shares
    assert result.commission == 0.0
    assert result.net_pnl == 100.0
    assert result.max_abs_position == 1


def test_run_backtest_spread_and_commission_cost() -> None:
    # Buy and sell at the same book -> pays the spread; commission on top.
    ticks = [_tick(100, 101, 1.0), _tick(100, 101, 0.0)]
    result = run_backtest(
        ticks,
        symbol="TEST",
        signal_params=SignalParams(threshold=0.3),
        cost_params=CostParams(commission_bps=10.0, lot=100, fill_delay_ticks=0),
    )
    # buy 100 @ 101 = -10100, sell 100 @ 100 = +10000 -> gross -100 (spread)
    assert result.gross_pnl == -100.0
    # commission = (10100 + 10000) * 10bps = 20.1
    assert round(result.commission, 2) == 20.1
    assert round(result.net_pnl, 2) == -120.1


def test_run_backtest_forces_flat_at_close() -> None:
    # Signal stays long the whole time; must still end flat (day-trade rule).
    ticks = [_tick(100, 101, 1.0), _tick(100, 101, 1.0), _tick(100, 101, 1.0)]
    result = run_backtest(
        ticks,
        symbol="TEST",
        signal_params=SignalParams(threshold=0.3),
        cost_params=CostParams(commission_bps=0.0, lot=100, fill_delay_ticks=0),
    )
    assert result.max_abs_position == 1
    # entered long at t0, closed at final tick -> net position 0 (no overnight)
    assert result.n_fills == 2
