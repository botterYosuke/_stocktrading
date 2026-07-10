from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest

from stocktrading.maker import (
    BoardSnap,
    Cancel,
    MakerCostParams,
    OwnView,
    Post,
    TakerExit,
    prepare_maker,
    run_maker_session,
)

T0 = datetime(2026, 7, 9, 10, 0, 0)

# latency 1.0s with snaps exactly 1s apart makes every schedule deterministic:
# an action decided while seeing snap i becomes effective at snap i+1.
FREE = MakerCostParams(
    maker_commission_bps=0.0,
    taker_commission_bps=0.0,
    latency_secs=1.0,
)


def _snap(
    secs: float,
    bid: float,
    ask: float,
    bid_qty: int = 1000,
    ask_qty: int = 1000,
    last_px: float | None = None,
    volume: int = 0,
    tradable: bool = True,
    bids2: tuple[tuple[float, int], ...] = (),
    asks2: tuple[tuple[float, int], ...] = (),
) -> BoardSnap:
    return BoardSnap(
        ts_event=T0 + timedelta(seconds=secs),
        bids=((bid, bid_qty),) + bids2,
        asks=((ask, ask_qty),) + asks2,
        last_px=last_px,
        volume=volume,
        tradable=tradable,
    )


@dataclass
class Scripted:
    """Deterministic strategy: emit the scripted actions at the i-th invocation.

    Also records (invocation, known position) and the visible resting orders so
    tests can assert what the strategy was allowed to know and when.
    """

    script: dict[int, tuple[object, ...]] = field(default_factory=dict)
    seen_positions: list[tuple[int, int]] = field(default_factory=list)
    seen_orders: list[tuple[int, tuple[tuple[int, bool], ...]]] = field(default_factory=list)

    def initial(self) -> int:
        return 0

    def decide(
        self, state: int, snap: BoardSnap, now: float, own: OwnView
    ) -> tuple[int, tuple[object, ...]]:
        self.seen_positions.append((state, own.position))
        self.seen_orders.append(
            (state, tuple((o.order_id, o.pending_cancel) for o in own.orders))
        )
        return state + 1, self.script.get(state, ())


def _run(snaps: list[BoardSnap], strategy, cost: MakerCostParams = FREE):
    sessions = prepare_maker(snaps)
    assert len(sessions) == 1
    return run_maker_session(sessions[0], strategy, cost)


# --- queue model -------------------------------------------------------------


def test_queue_depletes_only_by_traded_volume_then_fills() -> None:
    # Post buy@100 at snap0 -> active at snap1 with queue_ahead = 500 (snap1's
    # displayed size). 300 sh trade at 100 (snap2): queue 200. 300 more (snap3):
    # overflow 100 fills me. Forced flat at the final snap sells at the bid.
    snaps = [
        _snap(0, 100, 101, bid_qty=500, volume=1000),
        _snap(1, 100, 101, bid_qty=500, volume=1000),
        _snap(2, 100, 101, bid_qty=200, last_px=100, volume=1300),
        _snap(3, 100, 101, bid_qty=100, last_px=100, volume=1600),
        _snap(4, 100, 101, volume=1600),
    ]
    result = _run(snaps, Scripted({0: (Post(side=1, px=100.0, qty=100),)}))
    maker_fills = [f for f in result.fills if f.maker]
    assert len(maker_fills) == 1
    assert maker_fills[0].px == 100.0
    assert maker_fills[0].qty == 100
    # forced flat: one taker sell at 100 -> flat, zero cash net
    assert result.cash == pytest.approx(0.0)


def test_displayed_size_drop_without_volume_is_not_progress() -> None:
    # Displayed queue collapses 1000 -> 100 with no trades (cancellations ahead).
    # A conservative model must NOT advance my queue position: the later 300 sh
    # trade is less than my original 1000 queue, so I stay unfilled.
    snaps = [
        _snap(0, 100, 101, bid_qty=1000, volume=1000),
        _snap(1, 100, 101, bid_qty=1000, volume=1000),
        _snap(2, 100, 101, bid_qty=100, volume=1000),
        _snap(3, 100, 101, bid_qty=100, last_px=100, volume=1300),
        _snap(4, 100, 101, volume=1300),
    ]
    result = _run(snaps, Scripted({0: (Post(side=1, px=100.0, qty=100),)}))
    assert [f for f in result.fills if f.maker] == []
    assert result.missed_orders == 1
    assert result.missed_shares == 100


def test_volume_decrease_is_clamped() -> None:
    snaps = [
        _snap(0, 100, 101, bid_qty=100, volume=1000),
        _snap(1, 100, 101, bid_qty=100, volume=1000),
        _snap(2, 100, 101, bid_qty=100, last_px=100, volume=400),  # glitch: dv < 0
        _snap(3, 100, 101, volume=400),
    ]
    result = _run(snaps, Scripted({0: (Post(side=1, px=100.0, qty=100),)}))
    assert [f for f in result.fills if f.maker] == []


def test_half_attribution_credits_half_the_volume() -> None:
    # queue 500; 600 sh trade at my price but only 300 are credited at
    # attribution=0.5 -> still 200 ahead of me, no fill.
    cost = MakerCostParams(
        maker_commission_bps=0.0, taker_commission_bps=0.0, latency_secs=1.0, attribution=0.5
    )
    snaps = [
        _snap(0, 100, 101, bid_qty=500, volume=1000),
        _snap(1, 100, 101, bid_qty=500, volume=1000),
        _snap(2, 100, 101, bid_qty=100, last_px=100, volume=1600),
        _snap(3, 100, 101, volume=1600),
    ]
    result = _run(snaps, Scripted({0: (Post(side=1, px=100.0, qty=100),)}), cost)
    assert [f for f in result.fills if f.maker] == []


# --- through / crossing fills ------------------------------------------------


def test_trade_through_my_price_fills_fully() -> None:
    # Trades print BELOW my bid: price priority says my order was consumed first.
    snaps = [
        _snap(0, 100, 101, bid_qty=99999, volume=1000),
        _snap(1, 100, 101, bid_qty=99999, volume=1000),
        _snap(2, 99, 100, bid_qty=500, last_px=99, volume=1200),
        _snap(3, 99, 100, volume=1200),
    ]
    result = _run(snaps, Scripted({0: (Post(side=1, px=100.0, qty=100),)}))
    maker_fills = [f for f in result.fills if f.maker]
    assert len(maker_fills) == 1
    assert maker_fills[0].px == 100.0


def test_opposite_touch_crossing_my_price_fills_fully() -> None:
    # The displayed ask drops to my bid price with no trade prints: a displayed
    # ask <= my live bid cannot coexist with it, so I must have been filled.
    snaps = [
        _snap(0, 100, 101, bid_qty=99999, volume=1000),
        _snap(1, 100, 101, bid_qty=99999, volume=1000),
        _snap(2, 99, 100, bid_qty=500, volume=1000),
        _snap(3, 99, 100, volume=1000),
    ]
    result = _run(snaps, Scripted({0: (Post(side=1, px=100.0, qty=100),)}))
    maker_fills = [f for f in result.fills if f.maker]
    assert len(maker_fills) == 1
    assert maker_fills[0].px == 100.0


def test_arriving_marketable_after_latency_is_a_taker_fill_at_my_price() -> None:
    # Posted at the bid while seeing snap0, but the market fell during latency;
    # at activation the ask (99) is below my price: immediate taker fill at MY
    # price (pessimistic -- no price improvement), classified taker.
    snaps = [
        _snap(0, 100, 101, volume=1000),
        _snap(1, 98, 99, volume=1000),
        _snap(2, 98, 99, volume=1000),
    ]
    result = _run(snaps, Scripted({0: (Post(side=1, px=100.0, qty=100),)}))
    taker_fills = [f for f in result.fills if not f.maker and f.side == 1]
    assert len(taker_fills) == 1
    assert taker_fills[0].px == 100.0


# --- latency and leakage -----------------------------------------------------


def test_fills_before_activation_do_not_count() -> None:
    # The 600-sh trade at snap1 happens in the interval BEFORE my order becomes
    # active (it activates at snap1, joining the queue seen there): no credit.
    snaps = [
        _snap(0, 100, 101, bid_qty=500, volume=1000),
        _snap(1, 100, 101, bid_qty=500, last_px=100, volume=1600),
        _snap(2, 100, 101, bid_qty=500, volume=1600),
        _snap(3, 100, 101, volume=1600),
    ]
    result = _run(snaps, Scripted({0: (Post(side=1, px=100.0, qty=100),)}))
    assert [f for f in result.fills if f.maker] == []


def test_strategy_learns_of_fill_only_after_latency() -> None:
    # Fill lands at snap2 (through-trade); with 1s latency the strategy must
    # still see position 0 at snap2 and 100 only from snap3.
    strategy = Scripted({0: (Post(side=1, px=100.0, qty=100),)})
    snaps = [
        _snap(0, 100, 101, bid_qty=500, volume=1000),
        _snap(1, 100, 101, bid_qty=500, volume=1000),
        _snap(2, 99, 100, bid_qty=500, last_px=99, volume=1300),
        _snap(3, 99, 100, volume=1300),
        _snap(4, 99, 100, volume=1300),
    ]
    _run(snaps, strategy)
    known = dict(strategy.seen_positions)
    assert known[2] == 0
    assert known[3] == 100


def test_fill_during_cancel_latency_still_happens() -> None:
    # Cancel decided at snap2 is effective at snap3; the through-trade in the
    # (snap2, snap3] interval fills me first. Fills beat cancels.
    snaps = [
        _snap(0, 100, 101, bid_qty=500, volume=1000),
        _snap(1, 100, 101, bid_qty=500, volume=1000),
        _snap(2, 100, 101, bid_qty=500, volume=1000),
        _snap(3, 99, 100, bid_qty=500, last_px=99, volume=1400),
        _snap(4, 99, 100, volume=1400),
    ]
    result = _run(snaps, Scripted({0: (Post(side=1, px=100.0, qty=100),), 2: (Cancel(order_id=1),)}))
    assert len([f for f in result.fills if f.maker]) == 1


# --- auctions / session edges ------------------------------------------------


def test_no_fills_across_untradable_snapshots() -> None:
    # Huge volume prints while the book is in special-quote state (auction):
    # no queue credit, and the resting order is auto-cancelled at window entry.
    snaps = [
        _snap(0, 100, 101, bid_qty=100, volume=1000),
        _snap(1, 100, 101, bid_qty=100, volume=1000),
        _snap(2, 100, 100, bid_qty=100, last_px=100, volume=99000, tradable=False),
        _snap(3, 100, 101, bid_qty=100, last_px=100, volume=99000),
        _snap(4, 100, 101, volume=99000),
    ]
    result = _run(snaps, Scripted({0: (Post(side=1, px=100.0, qty=100),)}))
    assert result.fills == ()
    assert result.missed_orders == 1


def test_forced_flat_at_last_tradable_snap_pays_taker_commission() -> None:
    cost = MakerCostParams(
        maker_commission_bps=0.0, taker_commission_bps=10.0, latency_secs=1.0
    )
    snaps = [
        _snap(0, 100, 101, bid_qty=100, volume=1000),
        _snap(1, 100, 101, bid_qty=100, volume=1000),
        _snap(2, 100, 101, bid_qty=100, last_px=100, volume=1200),
        _snap(3, 102, 103, volume=1200),
        _snap(4, 102, 103, volume=1200, tradable=False),
    ]
    result = _run(snaps, Scripted({0: (Post(side=1, px=100.0, qty=100),)}), cost)
    # entry fill @100 (snap2), forced flat at snap3 (last tradable) sells @102
    assert result.cash == pytest.approx(200.0)
    assert result.commission == pytest.approx(102 * 100 * 10 / 10_000)
    assert result.round_trips == 1
    assert result.position_end == 0


def test_sessions_split_by_day_and_each_ends_flat() -> None:
    day1 = [
        _snap(0, 100, 101, bid_qty=100, volume=1000),
        _snap(1, 100, 101, bid_qty=100, volume=1000),
        _snap(2, 100, 101, bid_qty=100, last_px=100, volume=1500),
        _snap(3, 100, 101, volume=1500),
    ]
    day2 = [
        _snap(86400 + 0, 200, 201, volume=0),
        _snap(86400 + 1, 200, 201, volume=0),
    ]
    sessions = prepare_maker(day1 + day2)
    assert len(sessions) == 2
    for session in sessions:
        result = run_maker_session(session, Scripted({0: (Post(side=1, px=session.snaps[0].bids[0][0], qty=100),)}), FREE)
        assert result.position_end == 0


# --- taker exit --------------------------------------------------------------


def test_taker_exit_flattens_known_position_at_the_touch() -> None:
    snaps = [
        _snap(0, 100, 101, bid_qty=100, volume=1000),
        _snap(1, 100, 101, bid_qty=100, volume=1000),
        _snap(2, 100, 101, bid_qty=100, last_px=100, volume=1500),
        _snap(3, 100, 101, volume=1500),
        _snap(4, 105, 106, volume=1500),
        _snap(5, 105, 106, volume=1500),
        _snap(6, 105, 106, volume=1500),
    ]
    # fill at snap2, known at snap3, exit decided at snap3, effective snap4 @bid 105
    result = _run(snaps, Scripted({0: (Post(side=1, px=100.0, qty=100),), 3: (TakerExit(),)}))
    assert result.cash == pytest.approx((105 - 100) * 100)
    taker_fills = [f for f in result.fills if not f.maker]
    assert len(taker_fills) == 1
    assert taker_fills[0].px == 105.0


def test_repeated_taker_exits_coalesce_to_one_fill() -> None:
    # A position is held into a non-tradable window and the strategy keeps
    # re-issuing TakerExit (it cannot know the first one has not executed).
    # At the reopen exactly ONE exit fires: intents replace, they do not stack.
    snaps = [
        _snap(0, 100, 101, bid_qty=100, volume=1000),
        _snap(1, 100, 101, bid_qty=100, volume=1000),
        _snap(2, 100, 101, bid_qty=100, last_px=100, volume=1500),  # entry fill
        _snap(3, 100, 101, volume=1500, tradable=False),
        _snap(4, 100, 101, volume=1500, tradable=False),
        _snap(5, 100, 101, volume=1500),
        _snap(6, 100, 101, volume=1500),
        _snap(7, 100, 101, volume=1500),
    ]
    script = {
        0: (Post(side=1, px=100.0, qty=100),),
        3: (TakerExit(),),
        4: (TakerExit(),),
    }
    result = _run(snaps, Scripted(script))
    taker_fills = [f for f in result.fills if not f.maker]
    assert sum(f.qty for f in taker_fills) == 100
    assert result.position_end == 0


def test_cancel_acknowledgement_pays_return_latency() -> None:
    # Cancel decided at snap2 lands at the exchange at snap3, but the strategy
    # must keep seeing the order (pending_cancel) until snap4 = ack + latency;
    # only then may it repost. Seeing the cancel land instantly would gain a
    # latency leg of queue position on every reprice.
    strategy = Scripted({0: (Post(side=1, px=100.0, qty=100),), 2: (Cancel(order_id=1),)})
    snaps = [
        _snap(0, 100, 101, bid_qty=500, volume=1000),
        _snap(1, 100, 101, bid_qty=500, volume=1000),
        _snap(2, 100, 101, bid_qty=500, volume=1000),
        _snap(3, 100, 101, bid_qty=500, volume=1000),
        _snap(4, 100, 101, bid_qty=500, volume=1000),
        _snap(5, 100, 101, bid_qty=500, volume=1000),
    ]
    _run(snaps, strategy)
    orders_seen = dict(strategy.seen_orders)
    assert orders_seen[3] == ((1, True),)  # cancel effective at exchange, ack in flight
    assert orders_seen[4] == ()  # ack arrived: now the strategy may repost


# --- decomposition -----------------------------------------------------------


def test_decomposition_components_sum_to_net_pnl() -> None:
    cost = MakerCostParams(
        maker_commission_bps=2.0, taker_commission_bps=8.0, latency_secs=1.0
    )
    snaps = [
        _snap(0, 100, 101, bid_qty=100, volume=1000),
        _snap(1, 100, 101, bid_qty=100, volume=1000),
        _snap(2, 100, 101, bid_qty=100, last_px=100, volume=1500),
        _snap(3, 101, 102, volume=1500),
        _snap(4, 103, 104, volume=1500),
        _snap(5, 102, 103, volume=1500),
        _snap(6, 102, 103, volume=1500, tradable=False),
    ]
    result = _run(snaps, Scripted({0: (Post(side=1, px=100.0, qty=100),)}), cost)
    d = result.decomposition
    total = (
        d.spread_capture
        + d.taker_edge
        + d.adverse_selection
        + d.inventory_drift
        - d.commission_maker
        - d.commission_taker
    )
    assert total == pytest.approx(result.cash - result.commission)
    # entry was a maker fill at 100 with mid 100.5 -> half a spread captured
    assert d.spread_capture == pytest.approx(0.5 * 100)


def test_marketable_post_at_decision_time_is_rejected() -> None:
    snaps = [
        _snap(0, 100, 101, volume=0),
        _snap(1, 100, 101, volume=0),
    ]
    with pytest.raises(ValueError, match="marketable"):
        _run(snaps, Scripted({0: (Post(side=1, px=101.0, qty=100),)}))
