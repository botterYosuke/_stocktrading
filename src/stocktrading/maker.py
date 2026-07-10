from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from itertools import groupby
from math import inf
from typing import Protocol

import duckdb

from .config import Settings

# Passive (maker) execution simulator over board snapshots.
#
# This is deliberately a separate module from the taker `backtest.py`: the fill
# model is fundamentally different. A taker backtest only needs the touch; a
# maker backtest needs a queue model, latency, cancels and an inferred trade
# stream, because the board feed has no trade-by-trade data -- trades are
# reconstructed from cumulative `volume` deltas and `last_px` between
# consecutive snapshots.
#
# The model is conservative by design (docs/architecture.md records the
# pre-registered protocol):
# - The queue ahead of an order is everything displayed at its price when the
#   order ARRIVES (decision time + latency), and it advances only on inferred
#   executions at that price -- never on cancellations ahead.
# - An order decided while seeing snapshot t reaches the exchange at the first
#   snapshot >= t + latency. Cancels pay the same latency, and fills beat
#   cancels inside the racing interval. Fill notifications pay it again on the
#   way back: the strategy never knows a fill sooner than latency after it
#   happened (the same no-same-snapshot-leakage rule the taker backtest uses).
# - Nothing fills across special-quote / auction snapshots; the giant itayose
#   volume prints at 11:29:59 / 12:29:59 / 15:29:59 therefore never advance a
#   queue. Resting orders are auto-cancelled when such a window begins.
# - Day-trade rule: sessions are per-day and independent; the position is
#   forced flat with a taker fill at the last tradable snapshot (before the
#   closing auction), and every piece of state restarts the next morning.

MORNING_END = time(11, 29, 0)  # stop before the morning-close itayose print
AFTERNOON_START = time(12, 31, 0)  # resume after the afternoon-open itayose
CLOSE_CUTOFF = time(15, 24, 0)  # stop before the closing auction
CONTINUOUS_SIGN = "0101"

Levels = tuple[tuple[float, int], ...]  # ((px, qty), ...), best level first


@dataclass(frozen=True, slots=True)
class BoardSnap:
    ts_event: datetime
    bids: Levels
    asks: Levels
    last_px: float | None
    volume: int
    tradable: bool  # continuous trading (signs 0101/0101, outside auction windows)

    @property
    def bid_px(self) -> float:
        return self.bids[0][0]

    @property
    def ask_px(self) -> float:
        return self.asks[0][0]

    @property
    def mid(self) -> float:
        return (self.bids[0][0] + self.asks[0][0]) / 2.0


@dataclass(frozen=True, slots=True)
class MakerSession:
    """One trading day of snapshots; the clock runs from the day's first snap."""

    snaps: tuple[BoardSnap, ...]
    elapsed: tuple[float, ...]
    mids: tuple[float, ...]  # precomputed L1 mids (the engine reads them every snap)
    flatten_idx: int  # last tradable snap: forced taker flat happens here
    tick_size: float


def in_continuous_window(ts: datetime) -> bool:
    t = ts.time()
    return t < MORNING_END or (AFTERNOON_START <= t < CLOSE_CUTOFF)


def infer_tick_size(snaps: Sequence[BoardSnap], sample: int = 2000) -> float:
    """Smallest positive gap between adjacent displayed levels (fallback 1.0)."""
    best = inf
    for snap in snaps[:sample]:
        for levels in (snap.bids, snap.asks):
            for (px_a, _), (px_b, _) in zip(levels, levels[1:]):
                gap = abs(px_b - px_a)
                if 0.0 < gap < best:
                    best = gap
    return best if best < inf else 1.0


def prepare_maker(snaps: Sequence[BoardSnap]) -> tuple[MakerSession, ...]:
    """Split snapshots into per-day sessions (days with no tradable snap are dropped)."""
    sessions: list[MakerSession] = []
    for _, day in groupby(snaps, key=lambda snap: snap.ts_event.date()):
        day_snaps = tuple(day)
        flatten_idx = next(
            (i for i in range(len(day_snaps) - 1, -1, -1) if day_snaps[i].tradable), None
        )
        if flatten_idx is None:
            continue
        origin = day_snaps[0].ts_event
        elapsed = tuple((snap.ts_event - origin).total_seconds() for snap in day_snaps)
        sessions.append(
            MakerSession(
                snaps=day_snaps,
                elapsed=elapsed,
                mids=tuple(snap.mid for snap in day_snaps),
                flatten_idx=flatten_idx,
                tick_size=infer_tick_size(day_snaps),
            )
        )
    return tuple(sessions)


def load_maker_snaps(
    settings: Settings, symbol: str, trade_date: date | None = None
) -> list[BoardSnap]:
    """Load one symbol's snapshots (all 10 levels) from silver, ordered by time."""
    date_glob = f"date={trade_date.isoformat()}" if trade_date else "date=*"
    glob = (settings.silver_root / date_glob / f"code={symbol}" / "*.parquet").as_posix()
    level_cols = ", ".join(
        f"{side}_px_{level}, {side}_qty_{level}"
        for level in range(2, 11)
        for side in ("bid", "ask")
    )
    con = duckdb.connect()
    try:
        rows = con.execute(
            "SELECT ts_event, bid_px, bid_qty, ask_px, ask_qty, "
            f"bid_sign, ask_sign, last_px, volume, {level_cols} "
            "FROM read_parquet($glob) ORDER BY ts_event",
            {"glob": glob},
        ).fetchall()
    finally:
        con.close()

    snaps: list[BoardSnap] = []
    prev_volume = 0
    for row in rows:
        ts, bid_px, bid_qty, ask_px, ask_qty, bid_sign, ask_sign, last_px, volume = row[:9]
        bids = [(bid_px, int(bid_qty))]
        asks = [(ask_px, int(ask_qty))]
        deeper = row[9:]
        for j in range(0, len(deeper), 4):
            bpx, bqty, apx, aqty = deeper[j : j + 4]
            if bpx and bqty and bpx > 0:
                bids.append((bpx, int(bqty)))
            if apx and aqty and apx > 0:
                asks.append((apx, int(aqty)))
        # A locked book (bid == ask) passes the silver >= filter but cannot be a
        # real continuous-trading state; treating it as tradable would let the
        # crossing rule fill both sides for free and would crash touch posts.
        tradable = (
            bid_sign == CONTINUOUS_SIGN
            and ask_sign == CONTINUOUS_SIGN
            and bid_px < ask_px
            and in_continuous_window(ts)
        )
        # A NULL volume must not read as 0: the next interval's delta would be
        # the whole day's volume and would wipe every queue in one step.
        prev_volume = int(volume) if volume is not None else prev_volume
        snaps.append(
            BoardSnap(
                ts_event=ts,
                bids=tuple(bids),
                asks=tuple(asks),
                last_px=last_px,
                volume=prev_volume,
                tradable=tradable,
            )
        )
    return snaps


# --- actions & views ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Post:
    side: int  # +1 buy, -1 sell
    px: float
    qty: int  # shares


@dataclass(frozen=True, slots=True)
class Cancel:
    order_id: int


@dataclass(frozen=True, slots=True)
class TakerExit:
    """Flatten the position the strategy currently KNOWS about, at the touch."""


Action = Post | Cancel | TakerExit


@dataclass(frozen=True, slots=True)
class OrderView:
    order_id: int
    side: int
    px: float
    qty: int  # remaining shares as KNOWN to the strategy (unnotified fills excluded)
    active: bool
    pending_cancel: bool = False  # cancel sent; the order can still fill until it lands


@dataclass(frozen=True, slots=True)
class OwnView:
    """What the strategy is allowed to know: everything here is latency-delayed."""

    position: int  # shares, from notified fills only
    orders: tuple[OrderView, ...]
    avg_px: float | None = None  # average-cost price of the notified open position


class Strategy(Protocol):
    def initial(self) -> object: ...

    def decide(
        self, state: object, snap: BoardSnap, now: float, own: OwnView
    ) -> tuple[object, tuple[Action, ...]]: ...


@dataclass(frozen=True, slots=True)
class Fill:
    ts: float  # session clock (elapsed seconds)
    order_id: int  # 0 for engine-generated taker fills (exits, forced flat)
    side: int
    px: float
    qty: int
    maker: bool
    mid: float  # mid at the fill snapshot


@dataclass(frozen=True)
class MakerCostParams:
    maker_commission_bps: float = 1.5
    taker_commission_bps: float = 1.5
    latency_secs: float = 0.5  # decision -> exchange, and fill -> notification
    adverse_horizon_secs: float = 10.0  # drift within H of an entry fill is "adverse"
    attribution: float = 1.0  # fraction of inferred at-price volume credited to my queue

    def __post_init__(self) -> None:
        for name in ("maker_commission_bps", "taker_commission_bps", "latency_secs"):
            if getattr(self, name) < 0.0:
                raise ValueError(f"{name} must be >= 0, got {getattr(self, name)}")
        if self.adverse_horizon_secs < 0.0:
            raise ValueError(f"adverse_horizon_secs must be >= 0, got {self.adverse_horizon_secs}")
        if not 0.0 < self.attribution <= 1.0:
            raise ValueError(f"attribution must be in (0, 1], got {self.attribution}")


@dataclass(frozen=True)
class Decomposition:
    """Where the money went. Components sum exactly to net PnL (telescoping mid identity).

    net = spread_capture + taker_edge + adverse_selection + inventory_drift
          - commission_maker - commission_taker
    """

    spread_capture: float  # sum over maker fills of side*(mid - px)*qty  (>= 0 each)
    taker_edge: float  # same over taker fills (<= 0 each: crossing costs half a spread)
    adverse_selection: float  # mid drift while positioned, within H secs of an entry fill
    inventory_drift: float  # mid drift while positioned, after H secs
    commission_maker: float
    commission_taker: float


@dataclass(frozen=True)
class SessionResult:
    fills: tuple[Fill, ...]
    cash: float  # yen; spread effects included, commission excluded
    commission: float
    decomposition: Decomposition
    posted_shares: int
    filled_maker_shares: int
    filled_taker_shares: int
    missed_orders: int  # orders that died (cancel / auto-cancel / bell) with qty left
    missed_shares: int
    round_trips: int
    position_end: int
    turnover_yen: float
    time_in_market_secs: float
    inventory_share_secs: float  # integral of |position| dt
    max_drawdown: float  # most negative equity excursion from its running peak
    n_snaps: int

    @property
    def net(self) -> float:
        return self.cash - self.commission


@dataclass(slots=True)
class _Order:
    order_id: int
    side: int
    px: float
    qty: int  # remaining (actual)
    posted_qty: int
    effective_ts: float
    active_idx: int | None = None
    queue_ahead: float = 0.0
    cancel_ts: float | None = None
    retired_ts: float | None = None  # removed at the exchange; ack reaches strategy later
    fills: list[tuple[float, int]] = field(default_factory=list)  # (ts, qty)

    @property
    def working(self) -> bool:
        return self.retired_ts is None and self.qty > 0


def _displayed_at(levels: Levels, px: float) -> int:
    for level_px, level_qty in levels:
        if level_px == px:
            return level_qty
    return 0


def _queue_on_arrival(snap: BoardSnap, side: int, px: float) -> float:
    """Everything displayed at my price when I arrive is ahead of me.

    A price inside the spread (or between displayed levels) has no displayed
    size -> queue 0. A price deeper than the visible book is unknowable ->
    infinite queue (only trade-through can ever fill it). Conservative both ways.
    """
    levels = snap.bids if side > 0 else snap.asks
    displayed = _displayed_at(levels, px)
    if displayed:
        return float(displayed)
    deepest = levels[-1][0]
    if (side > 0 and px < deepest) or (side < 0 and px > deepest):
        return inf
    return 0.0


def run_maker_session(
    session: MakerSession, strategy: Strategy, cost: MakerCostParams
) -> SessionResult:
    snaps = session.snaps
    elapsed = session.elapsed
    mids = session.mids
    latency = cost.latency_secs
    horizon = cost.adverse_horizon_secs
    maker_rate = cost.maker_commission_bps / 10_000.0
    taker_rate = cost.taker_commission_bps / 10_000.0

    orders: dict[int, _Order] = {}
    next_order_id = 1
    # At most ONE taker-exit intent is in flight: a re-issued TakerExit replaces
    # the pending one (intents coalesce). Without this, a position held into a
    # non-tradable window would stack one full-size exit per re-issue and the
    # reopen would execute them all, flipping the position.
    pending_exit: tuple[float, int] | None = None  # (effective_ts, signed shares)
    fills: list[Fill] = []
    notify_ptr = 0  # fills[:notify_ptr] are known to the strategy
    known_position = 0
    known_cost = 0.0  # average-cost basis of the known position

    position = 0  # shares (actual)
    cash = 0.0
    commission_maker = 0.0
    commission_taker = 0.0
    spread_capture = 0.0
    taker_edge = 0.0
    adverse = 0.0
    inventory = 0.0
    last_entry_ts: float | None = None

    posted_shares = 0
    filled_maker = 0
    filled_taker = 0
    turnover = 0.0
    missed_orders = 0
    missed_shares = 0
    round_trips = 0
    time_in_market = 0.0
    inventory_share_secs = 0.0
    equity_peak = 0.0
    max_drawdown = 0.0

    strategy_state = strategy.initial()

    def apply_fill(idx: int, order_id: int, side: int, px: float, qty: int, maker: bool) -> None:
        nonlocal position, cash, commission_maker, commission_taker
        nonlocal spread_capture, taker_edge, filled_maker, filled_taker
        nonlocal round_trips, last_entry_ts, turnover
        if qty <= 0:
            return
        mid = mids[idx]
        old = position
        position += side * qty
        cash -= side * px * qty
        notional = px * qty
        turnover += notional
        if maker:
            commission_maker += notional * maker_rate
            spread_capture += side * (mid - px) * qty
            filled_maker += qty
        else:
            commission_taker += notional * taker_rate
            taker_edge += side * (mid - px) * qty
            filled_taker += qty
        if abs(position) > abs(old):
            last_entry_ts = elapsed[idx]
        if old != 0 and (position == 0 or (position > 0) != (old > 0)):
            round_trips += 1
        fills.append(
            Fill(
                ts=elapsed[idx],
                order_id=order_id,
                side=side,
                px=px,
                qty=qty,
                maker=maker,
                mid=mid,
            )
        )

    def retire(order: _Order, ts: float) -> None:
        """Remove the order at the exchange; the strategy learns one latency later."""
        nonlocal missed_orders, missed_shares
        if order.retired_ts is not None:
            return
        if order.qty > 0:
            missed_orders += 1
            missed_shares += order.qty
        order.retired_ts = ts

    for i, snap in enumerate(snaps):
        now = elapsed[i]

        # 1. mark-to-market drift over the interval we just lived through
        if i > 0 and position != 0:
            dt = now - elapsed[i - 1]
            drift = position * (mids[i] - mids[i - 1])
            if last_entry_ts is not None and (elapsed[i - 1] - last_entry_ts) <= horizon:
                adverse += drift
            else:
                inventory += drift
            time_in_market += dt
            inventory_share_secs += abs(position) * dt

        # 2. inferred executions over the interval advance queues / fill orders.
        #    Both endpoints must be continuous-trading snaps: auction prints
        #    (huge dv) must never count.
        if i > 0 and snap.tradable and snaps[i - 1].tradable and orders:
            dv = max(snap.volume - snaps[i - 1].volume, 0)
            if dv > 0 and snap.last_px is not None:
                trade_px = snap.last_px
                for order in orders.values():
                    if order.active_idx is None or order.active_idx >= i or not order.working:
                        continue
                    through = (order.side > 0 and trade_px < order.px) or (
                        order.side < 0 and trade_px > order.px
                    )
                    if through:
                        qty = order.qty
                        order.qty = 0
                        order.fills.append((now, qty))
                        apply_fill(i, order.order_id, order.side, order.px, qty, maker=True)
                    elif trade_px == order.px:
                        order.queue_ahead -= dv * cost.attribution
                        if order.queue_ahead < 0:
                            available = int(-order.queue_ahead)
                            qty = min(order.qty, available)
                            if qty > 0:
                                order.qty -= qty
                                order.queue_ahead += qty
                                order.fills.append((now, qty))
                                apply_fill(
                                    i, order.order_id, order.side, order.px, qty, maker=True
                                )

        # 3. the opposite touch crossing a live order's price means it was consumed
        if snap.tradable and orders:
            for order in orders.values():
                if order.active_idx is None or not order.working:
                    continue
                crossed = (order.side > 0 and snap.ask_px <= order.px) or (
                    order.side < 0 and snap.bid_px >= order.px
                )
                if crossed:
                    qty = order.qty
                    order.qty = 0
                    order.fills.append((now, qty))
                    apply_fill(i, order.order_id, order.side, order.px, qty, maker=True)

        # 4. cancels take effect at the exchange (after fills: fills beat cancels)
        for order in orders.values():
            if order.retired_ts is None and order.cancel_ts is not None and order.cancel_ts <= now:
                retire(order, order.cancel_ts)

        # 5. entering a non-continuous window kills every live order
        if not snap.tradable:
            for order in orders.values():
                if order.active_idx is not None:
                    retire(order, now)

        # 6. pending posts reach the exchange (only on a continuous snap)
        if snap.tradable:
            for order in orders.values():
                if (
                    order.active_idx is not None
                    or order.retired_ts is not None
                    or order.effective_ts > now
                ):
                    continue
                order.active_idx = i
                crossed = (order.side > 0 and order.px >= snap.ask_px) or (
                    order.side < 0 and order.px <= snap.bid_px
                )
                if crossed:
                    # Arrived marketable (the book moved during latency): it
                    # takes liquidity, pessimistically at my own limit price.
                    qty = order.qty
                    order.qty = 0
                    order.fills.append((now, qty))
                    apply_fill(i, order.order_id, order.side, order.px, qty, maker=False)
                else:
                    order.queue_ahead = _queue_on_arrival(snap, order.side, order.px)

            # 6.5 the pending taker exit executes at the touch
            if pending_exit is not None and pending_exit[0] <= now:
                shares = pending_exit[1]
                pending_exit = None
                if shares != 0:
                    side = -1 if shares > 0 else 1
                    px = snap.bid_px if side < 0 else snap.ask_px
                    apply_fill(i, 0, side, px, abs(shares), maker=False)

        # 7. the bell: cancel everything, force flat with a taker fill, stop
        if i == session.flatten_idx:
            for order in orders.values():
                retire(order, now)
            pending_exit = None
            if position != 0:
                side = -1 if position > 0 else 1
                px = snap.bid_px if side < 0 else snap.ask_px
                apply_fill(i, 0, side, px, abs(position), maker=False)
            equity = cash - commission_maker - commission_taker
            equity_peak = max(equity_peak, equity)
            max_drawdown = min(max_drawdown, equity - equity_peak)
            break

        # equity / drawdown tracking (after this snap's events)
        equity = cash - commission_maker - commission_taker + position * mids[i]
        equity_peak = max(equity_peak, equity)
        max_drawdown = min(max_drawdown, equity - equity_peak)

        # 8. notify the strategy of fills that are at least `latency` old
        while notify_ptr < len(fills) and fills[notify_ptr].ts + latency <= now:
            fill = fills[notify_ptr]
            old_known = known_position
            known_position += fill.side * fill.qty
            # average-cost accounting for the known position
            if old_known == 0 or (fill.side > 0) == (old_known > 0):
                known_cost += fill.px * fill.qty
            elif abs(known_position) < abs(old_known) and known_position * old_known >= 0:
                known_cost -= (known_cost / abs(old_known)) * fill.qty
            else:  # crossed through zero: remainder opens at the fill price
                known_cost = fill.px * abs(known_position)
            if known_position == 0:
                known_cost = 0.0
            notify_ptr += 1

        # Retired orders stay visible (pending_cancel) until the ack has had time
        # to travel back; deleting them instantly would let the strategy repost
        # one latency leg earlier than reality allows.
        acked = [
            order_id
            for order_id, order in orders.items()
            if order.retired_ts is not None and order.retired_ts + latency <= now
        ]
        for order_id in acked:
            del orders[order_id]

        views = []
        for order in orders.values():
            known_filled = sum(qty for ts, qty in order.fills if ts + latency <= now)
            known_remaining = order.posted_qty - known_filled
            if known_remaining > 0:
                views.append(
                    OrderView(
                        order_id=order.order_id,
                        side=order.side,
                        px=order.px,
                        qty=known_remaining,
                        active=order.active_idx is not None,
                        pending_cancel=order.cancel_ts is not None
                        or order.retired_ts is not None,
                    )
                )
        own = OwnView(
            position=known_position,
            orders=tuple(views),
            avg_px=known_cost / abs(known_position) if known_position else None,
        )

        strategy_state, actions = strategy.decide(strategy_state, snap, now, own)
        for action in actions:
            if isinstance(action, Post):
                marketable = snap.tradable and (
                    (action.side > 0 and action.px >= snap.ask_px)
                    or (action.side < 0 and action.px <= snap.bid_px)
                )
                if marketable:
                    raise ValueError(
                        f"marketable post: side={action.side} px={action.px} "
                        f"against bid={snap.bid_px} ask={snap.ask_px} -- post at or "
                        "behind the touch, or use TakerExit"
                    )
                if action.qty <= 0:
                    raise ValueError(f"post qty must be > 0, got {action.qty}")
                orders[next_order_id] = _Order(
                    order_id=next_order_id,
                    side=1 if action.side > 0 else -1,
                    px=action.px,
                    qty=action.qty,
                    posted_qty=action.qty,
                    effective_ts=now + latency,
                )
                posted_shares += action.qty
                next_order_id += 1
            elif isinstance(action, Cancel):
                order = orders.get(action.order_id)
                if order is not None and order.cancel_ts is None:
                    order.cancel_ts = now + latency
            elif isinstance(action, TakerExit):
                pending_exit = (now + latency, known_position)  # replaces, never stacks
            else:
                raise TypeError(f"unknown action: {action!r}")

    return SessionResult(
        fills=tuple(fills),
        cash=cash,
        commission=commission_maker + commission_taker,
        decomposition=Decomposition(
            spread_capture=spread_capture,
            taker_edge=taker_edge,
            adverse_selection=adverse,
            inventory_drift=inventory,
            commission_maker=commission_maker,
            commission_taker=commission_taker,
        ),
        posted_shares=posted_shares,
        filled_maker_shares=filled_maker,
        filled_taker_shares=filled_taker,
        missed_orders=missed_orders,
        missed_shares=missed_shares,
        round_trips=round_trips,
        position_end=position,
        turnover_yen=turnover,
        time_in_market_secs=time_in_market,
        inventory_share_secs=inventory_share_secs,
        max_drawdown=max_drawdown,
        n_snaps=len(snaps),
    )


@dataclass(frozen=True)
class MakerResult:
    """Aggregate over sessions (sessions are independent; sums are exact)."""

    symbol: str
    trade_date: date | None
    n_sessions: int
    n_snaps: int
    fills_maker: int
    fills_taker: int
    posted_shares: int
    filled_maker_shares: int
    filled_taker_shares: int
    missed_orders: int
    missed_shares: int
    cash: float
    commission: float
    decomposition: Decomposition
    round_trips: int
    turnover_yen: float
    time_in_market_secs: float
    inventory_share_secs: float
    max_drawdown: float  # worst single-session drawdown

    @property
    def net(self) -> float:
        return self.cash - self.commission

    @property
    def net_per_trip(self) -> float:
        return self.net / self.round_trips if self.round_trips else 0.0

    @property
    def fill_rate(self) -> float:
        return self.filled_maker_shares / self.posted_shares if self.posted_shares else 0.0

    @property
    def avg_hold_secs(self) -> float:
        return self.time_in_market_secs / self.round_trips if self.round_trips else 0.0

    @property
    def breakeven_bps_per_side(self) -> float:
        """Commission (bps/side) at which this run's gross PnL is exactly consumed."""
        return self.cash / self.turnover_yen * 10_000.0 if self.turnover_yen else 0.0


def run_maker(
    sessions: Sequence[MakerSession],
    symbol: str,
    strategy: Strategy | Callable[[MakerSession], Strategy],
    cost: MakerCostParams,
    trade_date: date | None = None,
) -> MakerResult:
    """Run every session; `strategy` may be a factory taking the session.

    Pass a factory whenever the strategy depends on per-session facts such as
    `tick_size` -- a single instance built from one day's tick silently corrupts
    multi-day runs when the price crosses a tick-band boundary.
    """
    results = [
        run_maker_session(
            session,
            strategy(session) if not hasattr(strategy, "initial") else strategy,
            cost,
        )
        for session in sessions
    ]
    return MakerResult(
        symbol=symbol,
        trade_date=trade_date,
        n_sessions=len(results),
        n_snaps=sum(r.n_snaps for r in results),
        fills_maker=sum(1 for r in results for f in r.fills if f.maker),
        fills_taker=sum(1 for r in results for f in r.fills if not f.maker),
        posted_shares=sum(r.posted_shares for r in results),
        filled_maker_shares=sum(r.filled_maker_shares for r in results),
        filled_taker_shares=sum(r.filled_taker_shares for r in results),
        missed_orders=sum(r.missed_orders for r in results),
        missed_shares=sum(r.missed_shares for r in results),
        cash=sum(r.cash for r in results),
        commission=sum(r.commission for r in results),
        decomposition=Decomposition(
            spread_capture=sum(r.decomposition.spread_capture for r in results),
            taker_edge=sum(r.decomposition.taker_edge for r in results),
            adverse_selection=sum(r.decomposition.adverse_selection for r in results),
            inventory_drift=sum(r.decomposition.inventory_drift for r in results),
            commission_maker=sum(r.decomposition.commission_maker for r in results),
            commission_taker=sum(r.decomposition.commission_taker for r in results),
        ),
        round_trips=sum(r.round_trips for r in results),
        turnover_yen=sum(r.turnover_yen for r in results),
        time_in_market_secs=sum(r.time_in_market_secs for r in results),
        inventory_share_secs=sum(r.inventory_share_secs for r in results),
        max_drawdown=min((r.max_drawdown for r in results), default=0.0),
    )
