from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# Pure, dependency-free signal logic. The SAME functions are meant to drive both
# the Python order-book backtest (over silver/gold) and the backcast marimo cell
# (live via the kabu adapter), so keep them free of I/O and framework imports.
#
# The signal is a state machine, not a per-tick map: smoothing, hysteresis and
# the hold/cooldown gates all depend on what happened before. State is passed in
# and a new state is returned, so `update` stays a pure function and the caller
# (backtest or live loop) owns the fold.

Observation = tuple[float, float | None]  # (seconds on a monotone clock, L1 imbalance)


@dataclass(frozen=True, slots=True)
class SignalParams:
    """Churn-aware parameters for the top-of-book imbalance signal.

    Defaults are deliberately neutral: they reproduce the plain threshold flip
    (no smoothing, no hysteresis, no gating). Named presets live below.
    """

    enter_threshold: float = 0.30  # |imbalance| needed to open a position from flat
    exit_threshold: float = 0.30  # |imbalance| needed to keep it (<= enter -> hysteresis)
    halflife_secs: float = 0.0  # time-decay halflife of the smoothed imbalance; 0 = raw
    min_hold_secs: float = 0.0  # once positioned, refuse to change target for this long
    cooldown_secs: float = 0.0  # once flat, refuse to re-open for this long

    def __post_init__(self) -> None:
        if not 0.0 <= self.enter_threshold <= 1.0:
            raise ValueError(f"enter_threshold must be in [0, 1], got {self.enter_threshold}")
        if not 0.0 <= self.exit_threshold <= 1.0:
            raise ValueError(f"exit_threshold must be in [0, 1], got {self.exit_threshold}")
        if self.exit_threshold > self.enter_threshold:
            raise ValueError(
                f"exit_threshold ({self.exit_threshold}) must not exceed "
                f"enter_threshold ({self.enter_threshold})"
            )
        for name in ("halflife_secs", "min_hold_secs", "cooldown_secs"):
            if getattr(self, name) < 0.0:
                raise ValueError(f"{name} must be >= 0, got {getattr(self, name)}")


@dataclass(frozen=True, slots=True)
class SignalState:
    """Everything the signal needs to carry from one observation to the next."""

    target: int = 0  # desired position in units: +1 long, -1 short, 0 flat
    smoothed: float | None = None  # time-decayed mean imbalance; None until first data
    mass: float = 0.0  # decayed observation count backing `smoothed`
    last_ts: float | None = None  # clock of the newest observation folded into `smoothed`
    target_ts: float | None = None  # clock of the last change to `target`


def initial_state() -> SignalState:
    return SignalState()


def _decay(dt: float, halflife_secs: float) -> float:
    """Weight retained by existing observations after `dt` seconds."""
    if halflife_secs <= 0.0:
        return 0.0
    return 0.5 ** (dt / halflife_secs)


def _smooth(state: SignalState, ts: float, imbalance: float, params: SignalParams) -> SignalState:
    """Fold one observation into a time-decayed mean where each tick carries unit mass.

        mass' = mass * decay + 1
        mean' = (mean * mass * decay + x) / mass'

    At a constant tick rate this is exactly the textbook EMA `d*mean + (1-d)*x`,
    but it also stays well-defined where that form breaks down on this data:
    duplicate timestamps (dt = 0) still admit the new tick with a normal weight
    instead of ignoring it, and a long hole such as the lunch break decays the
    old mass away so the mean restarts from the fresh observation.
    """
    if state.smoothed is None or state.last_ts is None:
        return SignalState(
            target=state.target,
            smoothed=imbalance,
            mass=1.0,
            last_ts=ts,
            target_ts=state.target_ts,
        )

    # Clamp: a non-monotone clock must never inflate mass (decay > 1).
    dt = max(ts - state.last_ts, 0.0)
    decay = _decay(dt, params.halflife_secs)
    mass = state.mass * decay + 1.0
    return SignalState(
        target=state.target,
        smoothed=(state.smoothed * state.mass * decay + imbalance) / mass,
        mass=mass,
        last_ts=max(state.last_ts, ts),
        target_ts=state.target_ts,
    )


def _hysteresis(smoothed: float, position: int, params: SignalParams) -> int:
    """Target position from the smoothed imbalance, given where we already are.

    Opening needs `enter_threshold`; staying only needs `exit_threshold`. A direct
    reversal is treated as an opening, so it needs full entry conviction too.
    """
    if position > 0:
        if smoothed < -params.enter_threshold:
            return -1
        return 1 if smoothed > params.exit_threshold else 0
    if position < 0:
        if smoothed > params.enter_threshold:
            return 1
        return -1 if smoothed < -params.exit_threshold else 0
    if smoothed > params.enter_threshold:
        return 1
    if smoothed < -params.enter_threshold:
        return -1
    return 0


def update(
    state: SignalState,
    ts: float,
    imbalance: float | None,
    params: SignalParams,
) -> SignalState:
    """Advance the signal by one observation and return the new state.

    `ts` is seconds on any monotone clock; only differences matter. A `None`
    imbalance (empty book on both sides) carries no information: the mean is left
    untouched and the previous target stands rather than being flattened.

    Everything here is decided from information available at `ts`; the caller is
    responsible for executing the resulting target no earlier than the next tick.
    """
    smoothed_state = state if imbalance is None else _smooth(state, ts, imbalance, params)
    if smoothed_state.smoothed is None:
        return smoothed_state

    desired = _hysteresis(smoothed_state.smoothed, smoothed_state.target, params)
    if desired == smoothed_state.target:
        return smoothed_state

    # Leaving a position is gated by min_hold; opening one is gated by cooldown.
    # Both clocks run from the last target change, i.e. from decision time -- the
    # live signal cannot see its own fills, so the backtest must not either.
    # Elapsed is clamped for the same reason `_smooth` clamps dt: a backwards
    # clock must not gate a change that a monotone clock would have allowed.
    gate = params.min_hold_secs if smoothed_state.target != 0 else params.cooldown_secs
    if smoothed_state.target_ts is not None:
        elapsed = max(ts - smoothed_state.target_ts, 0.0)
        if elapsed < gate:
            return smoothed_state

    return SignalState(
        target=desired,
        smoothed=smoothed_state.smoothed,
        mass=smoothed_state.mass,
        last_ts=smoothed_state.last_ts,
        target_ts=ts,
    )


def fold_states(observations: Iterable[Observation], params: SignalParams) -> list[SignalState]:
    """Run the signal over a whole series, returning the state after each observation.

    This is the single authority for target positions: the backtest and the gold
    writer both fold through here, so they can never drift apart.
    """
    state = initial_state()
    states: list[SignalState] = []
    for ts, imbalance in observations:
        state = update(state, ts, imbalance, params)
        states.append(state)
    return states


# Named presets.
#
# BASELINE is the pre-churn-work signal, kept so its (bad) numbers stay reproducible.
#
# CHURN_CONTROLLED is the best configuration found by `cli sweep` over 9984/285A/5803
# on 2026-07-09 among those taking >= 100 round trips. It removes 99.9% of the
# baseline loss and cuts fills ~278x -- but it is NOT profitable, and it is not a
# preset to trade. Its edge per round trip is still negative, because the signal
# only predicts ~0.2-0.3 yen of drift against a spread of 1.2+ yen. See
# docs/architecture.md; `exit_threshold=0` means "hold until the book flips sign".
BASELINE = SignalParams(enter_threshold=0.30, exit_threshold=0.30)
CHURN_CONTROLLED = SignalParams(
    enter_threshold=0.80,
    exit_threshold=0.00,
    halflife_secs=1.0,
    min_hold_secs=0.0,  # redundant once hysteresis and smoothing are in place
    cooldown_secs=0.0,
)
