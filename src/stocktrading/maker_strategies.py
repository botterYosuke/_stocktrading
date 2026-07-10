from __future__ import annotations

from dataclasses import dataclass

from .maker import Action, BoardSnap, Cancel, OwnView, Post, TakerExit
from .signals import SignalParams, SignalState, initial_state, update

# Maker strategies. Pure state machines in the signals.py mold: state in,
# (state, actions) out, no I/O -- the simulator owns the loop and the strategy
# only ever sees latency-delayed knowledge of its own orders (OwnView).
#
# Shared discipline: at most ONE working order per side, repriced by
# cancel-then-repost (which pays latency twice and loses the queue -- that is
# the real cost of chasing, so it must be modeled, not optimized away), and a
# paced TakerExit so a slow fill notification can't trigger a double exit.


def l1_imbalance(snap: BoardSnap) -> float | None:
    bid_qty = snap.bids[0][1]
    ask_qty = snap.asks[0][1]
    total = bid_qty + ask_qty
    return (bid_qty - ask_qty) / total if total > 0 else None


def _reconcile(own: OwnView, desired: tuple[int, float, int] | None) -> tuple[Action, ...]:
    """Steer the resting orders toward exactly `desired` = (side, px, qty).

    Cancels everything else. Posts only once no order -- including one whose
    cancel is still in flight -- occupies the desired side, so a cancel/replace
    race can never leave two live orders that both fill.
    """
    actions: list[Action] = []
    have_desired = False
    side_blocked = False
    for order in own.orders:
        if order.pending_cancel:
            if desired is not None and order.side == desired[0]:
                side_blocked = True
            continue
        if (
            desired is not None
            and not have_desired
            and order.side == desired[0]
            and order.px == desired[1]
        ):
            have_desired = True
            continue
        actions.append(Cancel(order_id=order.order_id))
        if desired is not None and order.side == desired[0]:
            side_blocked = True
    if desired is not None and not have_desired and not side_blocked:
        actions.append(Post(side=desired[0], px=desired[1], qty=desired[2]))
    return tuple(actions)


def _cancel_all(own: OwnView) -> tuple[Action, ...]:
    return tuple(Cancel(order_id=o.order_id) for o in own.orders if not o.pending_cancel)


# --- benchmark: unconditional join-the-bid ------------------------------------
#
# The fill-model gate from the pre-registered protocol, NOT a trading idea:
# always rest a buy at the best bid (chasing it), sell at the best ask once
# long, taker-exit after max_hold_secs. Run at zero commission it measures the
# fill dynamics themselves; if this shows materially positive net/trip, the
# fill model is leaking optimism and must be fixed before any strategy result
# is read.


@dataclass(frozen=True, slots=True)
class _CycleState:
    entry_seen_ts: float | None = None
    exit_issued_ts: float | None = None


@dataclass(frozen=True)
class BenchmarkJoin:
    qty: int = 100
    max_hold_secs: float = 300.0
    latency_secs: float = 0.5  # pacing only: reissue a TakerExit at most every 3x

    def initial(self) -> _CycleState:
        return _CycleState()

    def decide(
        self, state: _CycleState, snap: BoardSnap, now: float, own: OwnView
    ) -> tuple[_CycleState, tuple[Action, ...]]:
        if own.position == 0:
            desired = (1, snap.bid_px, self.qty) if snap.tradable else None
            return _CycleState(), _reconcile(own, desired)

        entry_ts = state.entry_seen_ts if state.entry_seen_ts is not None else now
        overdue = now - entry_ts >= self.max_hold_secs or own.position < 0
        if overdue:
            if state.exit_issued_ts is None or now - state.exit_issued_ts >= 3 * self.latency_secs:
                return _CycleState(entry_ts, now), _cancel_all(own) + (TakerExit(),)
            return _CycleState(entry_ts, state.exit_issued_ts), ()

        desired = (-1, snap.ask_px, own.position) if snap.tradable else None
        return _CycleState(entry_ts, state.exit_issued_ts), _reconcile(own, desired)


# --- v1: imbalance-gated passive entry ----------------------------------------
#
# FROZEN (2026-07-10): this family FAILED its pre-registered protocol and is NOT
# a tuning target. At zero commission every sampled configuration lost on all 6
# dev symbols, and the frozen best config lost on 50/50 symbols (-189 yen/trip);
# its net/trip equals the unconditional benchmark's (-113 vs -114 on 9984), i.e.
# the signal gate does not improve fill-conditional expectancy at all. Kept as a
# negative control and fill-model regression target. See docs/architecture.md.
#
# Original idea: the taker phase established that L1 imbalance predicts 1-20
# ticks of drift but nowhere near enough to pay the spread. Here the same signal
# gates which side of the book we rest on: enter passively WITH the imbalance
# (earning the spread instead of paying it), exit passively at the opposite
# touch, and give up via taker only on a hard stop, a signal flip, or a time stop.


@dataclass(frozen=True)
class ImbalanceMakerParams:
    signal: SignalParams
    qty: int = 100
    stop_ticks: float = 3.0  # taker-exit when mid moves this far against the entry
    max_hold_secs: float = 300.0
    improve_ticks: int = 0  # post this many ticks inside the spread (0 = join the touch)

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError(f"qty must be > 0, got {self.qty}")
        if self.stop_ticks <= 0.0:
            raise ValueError(f"stop_ticks must be > 0, got {self.stop_ticks}")
        if self.max_hold_secs <= 0.0:
            raise ValueError(f"max_hold_secs must be > 0, got {self.max_hold_secs}")
        if self.improve_ticks < 0:
            raise ValueError(f"improve_ticks must be >= 0, got {self.improve_ticks}")


@dataclass(frozen=True, slots=True)
class _ImbState:
    signal: SignalState
    entry_seen_ts: float | None = None
    exit_issued_ts: float | None = None


@dataclass(frozen=True)
class ImbalanceMaker:
    params: ImbalanceMakerParams
    tick_size: float = 1.0  # per-session; caller sets it from MakerSession
    latency_secs: float = 0.5

    def initial(self) -> _ImbState:
        return _ImbState(signal=initial_state())

    def _passive_px(self, snap: BoardSnap, side: int) -> float:
        """Price improved up to improve_ticks inside the spread, never marketable.

        With no room (or improve 0) this is exactly the touch price straight from
        the snapshot, so float comparisons against displayed levels stay exact;
        an improved price sits strictly between levels, where the queue is empty.
        """
        tick = self.tick_size
        room = int(round((snap.ask_px - snap.bid_px) / tick)) - 1
        eff = min(self.params.improve_ticks, max(room, 0))
        if eff == 0:
            return snap.bid_px if side > 0 else snap.ask_px
        raw = snap.bid_px + eff * tick if side > 0 else snap.ask_px - eff * tick
        return round(raw, 4)

    def decide(
        self, state: _ImbState, snap: BoardSnap, now: float, own: OwnView
    ) -> tuple[_ImbState, tuple[Action, ...]]:
        signal = update(state.signal, now, l1_imbalance(snap), self.params.signal)
        target = signal.target

        if own.position == 0:
            if target == 0 or not snap.tradable:
                return _ImbState(signal), _cancel_all(own)
            px = self._passive_px(snap, target)
            return _ImbState(signal), _reconcile(own, (target, px, self.params.qty))

        held = 1 if own.position > 0 else -1
        entry_ts = state.entry_seen_ts if state.entry_seen_ts is not None else now
        stopped = (
            own.avg_px is not None
            and held * (snap.mid - own.avg_px) <= -self.params.stop_ticks * self.tick_size
        )
        give_up = (
            stopped
            or target == -held
            or now - entry_ts >= self.params.max_hold_secs
        )
        if give_up:
            if state.exit_issued_ts is None or now - state.exit_issued_ts >= 3 * self.latency_secs:
                return _ImbState(signal, entry_ts, now), _cancel_all(own) + (TakerExit(),)
            return _ImbState(signal, entry_ts, state.exit_issued_ts), ()

        if not snap.tradable:
            return _ImbState(signal, entry_ts, state.exit_issued_ts), ()
        px = self._passive_px(snap, -held)
        desired = (-held, px, abs(own.position))
        return _ImbState(signal, entry_ts, state.exit_issued_ts), _reconcile(own, desired)
