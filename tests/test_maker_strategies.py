from __future__ import annotations

from datetime import datetime, timedelta

from stocktrading.maker import BoardSnap, Cancel, OrderView, OwnView, Post, TakerExit
from stocktrading.maker_strategies import (
    BenchmarkJoin,
    ImbalanceMaker,
    ImbalanceMakerParams,
    l1_imbalance,
)
from stocktrading.signals import SignalParams

T0 = datetime(2026, 7, 9, 10, 0, 0)


def _snap(
    secs: float, bid: float, ask: float, bid_qty: int = 1000, ask_qty: int = 1000
) -> BoardSnap:
    return BoardSnap(
        ts_event=T0 + timedelta(seconds=secs),
        bids=((bid, bid_qty),),
        asks=((ask, ask_qty),),
        last_px=None,
        volume=0,
        tradable=True,
    )


def _own(position: int = 0, orders: tuple[OrderView, ...] = (), avg_px: float | None = None):
    return OwnView(position=position, orders=orders, avg_px=avg_px)


IMB = ImbalanceMaker(
    ImbalanceMakerParams(
        signal=SignalParams(enter_threshold=0.5, exit_threshold=0.0),
        qty=100,
        stop_ticks=2.0,
        max_hold_secs=300.0,
    ),
    tick_size=1.0,
    latency_secs=0.5,
)


def test_imbalance_posts_entry_at_bid_when_signal_fires() -> None:
    state = IMB.initial()
    state, actions = IMB.decide(state, _snap(0, 100, 101, bid_qty=900, ask_qty=100), 0.0, _own())
    assert actions == (Post(side=1, px=100.0, qty=100),)


def test_imbalance_stays_quiet_below_threshold() -> None:
    state = IMB.initial()
    state, actions = IMB.decide(state, _snap(0, 100, 101, bid_qty=550, ask_qty=450), 0.0, _own())
    assert actions == ()


def test_imbalance_cancels_entry_when_signal_dies() -> None:
    state = IMB.initial()
    state, _ = IMB.decide(state, _snap(0, 100, 101, bid_qty=900, ask_qty=100), 0.0, _own())
    resting = (OrderView(order_id=1, side=1, px=100.0, qty=100, active=True),)
    # book eases below exit=0 (but not past -enter) -> target 0, not a reversal
    state, actions = IMB.decide(
        state, _snap(5, 100, 101, bid_qty=450, ask_qty=550), 5.0, _own(orders=resting)
    )
    assert Cancel(order_id=1) in actions
    assert not any(isinstance(a, Post) for a in actions)


def test_imbalance_reprices_entry_when_touch_moves() -> None:
    state = IMB.initial()
    state, _ = IMB.decide(state, _snap(0, 100, 101, bid_qty=900, ask_qty=100), 0.0, _own())
    resting = (OrderView(order_id=1, side=1, px=100.0, qty=100, active=True),)
    state, actions = IMB.decide(
        state, _snap(1, 102, 103, bid_qty=900, ask_qty=100), 1.0, _own(orders=resting)
    )
    # old px is stale: cancel it, and do NOT post until the cancel lands
    assert actions == (Cancel(order_id=1),)
    dying = (OrderView(order_id=1, side=1, px=100.0, qty=100, active=True, pending_cancel=True),)
    state, actions = IMB.decide(
        state, _snap(2, 102, 103, bid_qty=900, ask_qty=100), 2.0, _own(orders=dying)
    )
    assert actions == ()  # still blocked by the in-flight cancel
    state, actions = IMB.decide(
        state, _snap(3, 102, 103, bid_qty=900, ask_qty=100), 3.0, _own(orders=())
    )
    assert actions == (Post(side=1, px=102.0, qty=100),)


def test_imbalance_posts_passive_exit_once_long() -> None:
    state = IMB.initial()
    state, _ = IMB.decide(state, _snap(0, 100, 101, bid_qty=900, ask_qty=100), 0.0, _own())
    state, actions = IMB.decide(
        state,
        _snap(1, 100, 101, bid_qty=900, ask_qty=100),
        1.0,
        _own(position=100, avg_px=100.0),
    )
    assert actions == (Post(side=-1, px=101.0, qty=100),)


def test_imbalance_taker_exits_on_stop() -> None:
    state = IMB.initial()
    state, _ = IMB.decide(state, _snap(0, 100, 101, bid_qty=900, ask_qty=100), 0.0, _own())
    # long from 100, mid now 98.0 <= 100 - 2 ticks -> stop
    exit_order = (OrderView(order_id=2, side=-1, px=101.0, qty=100, active=True),)
    state, actions = IMB.decide(
        state,
        _snap(1, 97.5, 98.5, bid_qty=900, ask_qty=100),
        1.0,
        _own(position=100, orders=exit_order, avg_px=100.0),
    )
    assert Cancel(order_id=2) in actions
    assert any(isinstance(a, TakerExit) for a in actions)
    # and the exit is paced: immediately asking again does not re-fire
    state, actions = IMB.decide(
        state,
        _snap(1.2, 97.5, 98.5, bid_qty=900, ask_qty=100),
        1.2,
        _own(position=100, avg_px=100.0),
    )
    assert actions == ()


def test_benchmark_cycles_bid_then_ask() -> None:
    bench = BenchmarkJoin(qty=100, max_hold_secs=300.0, latency_secs=0.5)
    state = bench.initial()
    state, actions = bench.decide(state, _snap(0, 100, 101), 0.0, _own())
    assert actions == (Post(side=1, px=100.0, qty=100),)
    state, actions = bench.decide(state, _snap(1, 100, 101), 1.0, _own(position=100))
    assert actions == (Post(side=-1, px=101.0, qty=100),)


def test_l1_imbalance() -> None:
    assert l1_imbalance(_snap(0, 100, 101, bid_qty=300, ask_qty=100)) == 0.5
    assert l1_imbalance(_snap(0, 100, 101, bid_qty=0, ask_qty=0)) is None
