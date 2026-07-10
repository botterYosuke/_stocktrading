from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from stocktrading.backtest import CostParams, Tick, run_backtest, signal_states
from stocktrading.signals import SignalParams

T0 = datetime(2026, 7, 9, 9, 0, 0)
BANG_BANG = SignalParams(enter_threshold=0.30, exit_threshold=0.30)
FREE = CostParams(commission_bps=0.0, lot=100, fill_delay_ticks=0)


def _tick(bid: float, ask: float, imb: float | None, secs: float = 0.0) -> Tick:
    return Tick(
        ts_event=T0 + timedelta(seconds=secs),
        bid_px=bid,
        ask_px=ask,
        mid=(bid + ask) / 2,
        imbalance=imb,
    )


def _ticks(*specs: tuple[float, float, float | None]) -> list[Tick]:
    return [_tick(bid, ask, imb, secs=float(i)) for i, (bid, ask, imb) in enumerate(specs)]


def _run(ticks: list[Tick], signal_params: SignalParams, cost_params: CostParams = FREE):
    return run_backtest(ticks, symbol="TEST", signal_params=signal_params, cost_params=cost_params)


# --- accounting (unchanged behaviour) ---------------------------------------


def test_run_backtest_round_trip_no_cost() -> None:
    # t0: go long (buy @ ask=101). t1 is last tick -> forced flat (sell @ bid=102).
    result = _run(_ticks((100, 101, 1.0), (102, 103, 0.0)), BANG_BANG)
    assert result.n_fills == 2
    assert result.gross_pnl == 100.0  # (102 - 101) * 100 shares
    assert result.commission == 0.0
    assert result.net_pnl == 100.0
    assert result.max_abs_position == 1


def test_run_backtest_spread_and_commission_cost() -> None:
    # Buy and sell at the same book -> pays the spread; commission on top.
    result = _run(
        _ticks((100, 101, 1.0), (100, 101, 0.0)),
        BANG_BANG,
        CostParams(commission_bps=10.0, lot=100, fill_delay_ticks=0),
    )
    # buy 100 @ 101 = -10100, sell 100 @ 100 = +10000 -> gross -100 (spread)
    assert result.gross_pnl == -100.0
    # commission = (10100 + 10000) * 10bps = 20.1
    assert round(result.commission, 2) == 20.1
    assert round(result.net_pnl, 2) == -120.1


def test_run_backtest_forces_flat_at_close() -> None:
    # Signal stays long the whole time; must still end flat (day-trade rule).
    result = _run(_ticks((100, 101, 1.0), (100, 101, 1.0), (100, 101, 1.0)), BANG_BANG)
    assert result.max_abs_position == 1
    assert result.n_fills == 2


def test_forced_flat_overrides_min_hold() -> None:
    params = SignalParams(enter_threshold=0.30, exit_threshold=0.30, min_hold_secs=3600.0)
    result = _run(_ticks((100, 101, 1.0), (100, 101, 1.0), (100, 101, 1.0)), params)
    assert result.n_fills == 2  # entered, then closed at the bell despite the hold


def test_fill_delay_executes_the_previous_decision_at_the_current_touch() -> None:
    # Decide long at t0; the fill lands at t1's ask (110), not t0's (101).
    ticks = _ticks((100, 101, 1.0), (109, 110, 1.0), (120, 121, 1.0))
    result = _run(ticks, BANG_BANG, CostParams(commission_bps=0.0, lot=100, fill_delay_ticks=1))
    assert result.n_fills == 2
    # bought @110 at t1, forced flat @120 (bid) at t2
    assert result.gross_pnl == pytest.approx((120 - 110) * 100)


def test_no_position_before_the_delay_has_elapsed() -> None:
    ticks = _ticks((100, 101, 1.0), (100, 101, 1.0))
    result = _run(ticks, BANG_BANG, CostParams(commission_bps=0.0, lot=100, fill_delay_ticks=5))
    # every decision is still in flight; the close-out has nothing to close
    assert result.n_fills == 0
    assert result.max_abs_position == 0


def test_zero_spread_round_trip_is_free() -> None:
    # Locked books (ask == bid) occur in the recording; they must cost nothing.
    result = _run(_ticks((100, 100, 1.0), (100, 100, 0.0)), BANG_BANG)
    assert result.gross_pnl == 0.0


# --- churn suppression ------------------------------------------------------


def _oscillating(n: int) -> list[Tick]:
    # Imbalance rattles across the entry threshold on every tick; the book never moves.
    return [_tick(100, 101, 0.35 if i % 2 == 0 else 0.25, secs=float(i)) for i in range(n)]


def test_hysteresis_cuts_fills_and_loses_less() -> None:
    ticks = _oscillating(20)
    base = _run(ticks, BANG_BANG)
    hyst = _run(ticks, SignalParams(enter_threshold=0.30, exit_threshold=0.10))
    assert base.n_fills == 20  # in and out on every tick
    assert hyst.n_fills == 2  # one entry, one forced close
    assert hyst.net_pnl > base.net_pnl


def test_min_hold_cuts_fills() -> None:
    ticks = _oscillating(20)
    base = _run(ticks, BANG_BANG)
    held = _run(
        ticks, SignalParams(enter_threshold=0.30, exit_threshold=0.30, min_hold_secs=5.0)
    )
    assert held.n_fills < base.n_fills


def test_smoothing_cuts_fills() -> None:
    # A quiet prefix builds mass in the average, then imbalance slams between the
    # extremes. Raw imbalance flips on every tick; the smoothed mean stays near 0.
    quiet = [_tick(100, 101, 0.0, secs=float(i)) for i in range(10)]
    noisy = [_tick(100, 101, 0.9 if i % 2 == 0 else -0.9, secs=float(i)) for i in range(10, 30)]
    ticks = quiet + noisy

    base = _run(ticks, BANG_BANG)
    smooth = _run(
        ticks, SignalParams(enter_threshold=0.30, exit_threshold=0.30, halflife_secs=10.0)
    )
    assert base.n_fills == 20  # a flip on every noisy tick
    assert smooth.n_fills == 0  # never convinced to take a side at all


# --- hold-time reporting ----------------------------------------------------


def test_round_trips_and_time_in_market() -> None:
    # long from t0 (fill at t0), flat at t2, long again t3, closed at the t4 bell
    ticks = _ticks(
        (100, 101, 1.0),
        (100, 101, 1.0),
        (100, 101, 0.0),
        (100, 101, 1.0),
        (100, 101, 1.0),
    )
    result = _run(ticks, BANG_BANG)
    assert result.n_round_trips == 2
    assert result.time_in_market_secs == pytest.approx(3.0)  # t0->t2 and t3->t4
    assert result.avg_hold_secs == pytest.approx(1.5)


def test_avg_hold_is_zero_when_never_in_the_market() -> None:
    result = _run(_ticks((100, 101, 0.0), (100, 101, 0.0)), BANG_BANG)
    assert result.n_fills == 0
    assert result.n_round_trips == 0
    assert result.avg_hold_secs == 0.0


def test_a_flip_counts_as_a_completed_round_trip() -> None:
    ticks = _ticks((100, 101, 1.0), (100, 101, -1.0), (100, 101, -1.0))
    result = _run(ticks, BANG_BANG)
    assert result.n_fills == 3  # buy 1, sell 2 (flip), buy 1 (close)
    assert result.max_abs_position == 1
    assert result.n_round_trips == 2


# --- degenerate inputs ------------------------------------------------------


def test_empty_ticks() -> None:
    result = _run([], BANG_BANG)
    assert (result.n_ticks, result.n_fills, result.net_pnl) == (0, 0, 0.0)
    assert result.n_sessions == 0


def test_single_tick_never_opens_a_position() -> None:
    result = _run(_ticks((100, 101, 1.0)), BANG_BANG)
    assert result.n_fills == 0
    assert result.net_pnl == 0.0


# --- multi-day: every session stands alone ----------------------------------


def _day(day: int, *specs: tuple[float, float, float | None]) -> list[Tick]:
    base = datetime(2026, 7, 9 + day, 9, 0, 0)
    return [
        Tick(ts_event=base + timedelta(seconds=i), bid_px=b, ask_px=a, mid=(a + b) / 2, imbalance=m)
        for i, (b, a, m) in enumerate(specs)
    ]


LONG_DAY = ((100, 101, 1.0), (100, 101, 1.0), (100, 101, 1.0))


def test_position_is_flat_at_every_session_close_not_just_the_last() -> None:
    # Signal wants to stay long across both days; the day-trade rule forbids it.
    result = _run(_day(0, *LONG_DAY) + _day(1, *LONG_DAY), BANG_BANG)
    assert result.n_sessions == 2
    assert result.n_round_trips == 2  # one per session, not one spanning the night
    assert result.n_fills == 4  # buy/sell on each day
    # each session holds ~2s; an overnight carry would show ~86,400s
    assert result.time_in_market_secs == pytest.approx(4.0)


def test_sessions_are_independent_of_each_other() -> None:
    days = _day(0, *LONG_DAY) + _day(1, *LONG_DAY)
    combined = _run(days, BANG_BANG)
    separate = [_run(_day(d, *LONG_DAY), BANG_BANG) for d in (0, 1)]
    assert combined.n_fills == sum(r.n_fills for r in separate)
    assert combined.net_pnl == pytest.approx(sum(r.net_pnl for r in separate))
    assert combined.time_in_market_secs == pytest.approx(
        sum(r.time_in_market_secs for r in separate)
    )


def test_the_fill_delay_window_does_not_span_the_overnight_gap() -> None:
    # Day 1 ends wanting long. With a delay, day 2's first tick must NOT execute
    # yesterday's decision at today's price.
    cost = CostParams(commission_bps=0.0, lot=100, fill_delay_ticks=1)
    days = _day(0, *LONG_DAY) + _day(1, (200, 201, 0.0), (200, 201, 0.0))
    result = run_backtest(days, symbol="TEST", signal_params=BANG_BANG, cost_params=cost)
    # day 2 is flat throughout: no fills there, so no 200-priced trades at all
    assert result.turnover_yen == pytest.approx(20_100.0)  # buy@101 + sell@100, day 1 only


def test_min_hold_does_not_survive_the_night() -> None:
    params = SignalParams(enter_threshold=0.30, exit_threshold=0.30, min_hold_secs=3600.0)
    result = _run(_day(0, *LONG_DAY) + _day(1, *LONG_DAY), params)
    assert result.n_fills == 4  # forced flat each day despite the hour-long hold


def test_signal_state_restarts_each_session() -> None:
    # A day of strong positive imbalance must not seed day 2's smoothed average.
    params = SignalParams(enter_threshold=0.30, exit_threshold=0.30, halflife_secs=10.0)
    days = _day(0, *LONG_DAY) + _day(1, (100, 101, -1.0), (100, 101, -1.0))
    states = signal_states(days, params)
    assert states[3].smoothed == pytest.approx(-1.0)  # first tick of day 2 seeds fresh
