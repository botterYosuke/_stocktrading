from __future__ import annotations

import math

import pytest

from stocktrading.signals import (
    BASELINE,
    CHURN_CONTROLLED,
    SignalParams,
    SignalState,
    fold_states,
    initial_state,
    update,
)

BANG_BANG = SignalParams(enter_threshold=0.30, exit_threshold=0.30)


def _targets(params: SignalParams, observations: list[tuple[float, float | None]]) -> list[int]:
    return [s.target for s in fold_states(observations, params)]


# --- degenerate case: must reproduce the original threshold flip -------------


def test_no_hysteresis_no_smoothing_reproduces_threshold_flip() -> None:
    obs = [(0.0, 0.5), (1.0, -0.5), (2.0, 0.1), (3.0, -0.1), (4.0, None)]
    assert _targets(BANG_BANG, obs) == [1, -1, 0, 0, 0]


def test_exactly_at_entry_threshold_is_flat() -> None:
    assert _targets(BANG_BANG, [(0.0, 0.30)]) == [0]
    assert _targets(BANG_BANG, [(0.0, -0.30)]) == [0]


def test_missing_imbalance_before_any_data_is_flat() -> None:
    assert _targets(BANG_BANG, [(0.0, None), (1.0, None)]) == [0, 0]


def test_missing_imbalance_holds_the_previous_target() -> None:
    # A tick with no book quantity carries no information; it must not flatten us.
    obs = [(0.0, 0.9), (1.0, None), (2.0, 0.9)]
    assert _targets(BANG_BANG, obs) == [1, 1, 1]


# --- hysteresis -------------------------------------------------------------


def test_hysteresis_holds_the_position_between_exit_and_entry() -> None:
    params = SignalParams(enter_threshold=0.30, exit_threshold=0.10)
    # 0.20 is too weak to enter from flat, but strong enough to stay long.
    assert _targets(params, [(0.0, 0.20)]) == [0]
    assert _targets(params, [(0.0, 0.50), (1.0, 0.20)]) == [1, 1]


def test_hysteresis_exits_below_the_exit_threshold() -> None:
    params = SignalParams(enter_threshold=0.30, exit_threshold=0.10)
    assert _targets(params, [(0.0, 0.50), (1.0, 0.05)]) == [1, 0]
    # exactly at the exit threshold is not enough to stay
    assert _targets(params, [(0.0, 0.50), (1.0, 0.10)]) == [1, 0]


def test_hysteresis_reversal_requires_full_entry_conviction() -> None:
    params = SignalParams(enter_threshold=0.30, exit_threshold=0.10)
    # -0.20 clears the exit band but not the entry band -> flat, not short.
    assert _targets(params, [(0.0, 0.50), (1.0, -0.20)]) == [1, 0]
    # -0.40 clears entry -> direct flip long to short.
    assert _targets(params, [(0.0, 0.50), (1.0, -0.40)]) == [1, -1]


def test_hysteresis_is_symmetric_for_shorts() -> None:
    params = SignalParams(enter_threshold=0.30, exit_threshold=0.10)
    assert _targets(params, [(0.0, -0.50), (1.0, -0.20)]) == [-1, -1]
    assert _targets(params, [(0.0, -0.50), (1.0, -0.05)]) == [-1, 0]


def test_hysteresis_suppresses_churn_versus_bang_bang() -> None:
    # Imbalance rattles around the entry threshold; bang-bang flips on every tick.
    obs = [(float(i), 0.35 if i % 2 == 0 else 0.25) for i in range(10)]
    hyst = SignalParams(enter_threshold=0.30, exit_threshold=0.10)
    assert _targets(BANG_BANG, obs) == [1, 0] * 5
    assert _targets(hyst, obs) == [1] * 10


# --- minimum hold / cooldown ------------------------------------------------


def test_min_hold_blocks_an_early_exit() -> None:
    params = SignalParams(enter_threshold=0.30, exit_threshold=0.30, min_hold_secs=5.0)
    obs = [(0.0, 0.5), (1.0, 0.0), (4.9, 0.0), (5.0, 0.0)]
    # held through the flat signal until 5s elapsed since the entry decision
    assert _targets(params, obs) == [1, 1, 1, 0]


def test_min_hold_blocks_an_early_reversal() -> None:
    params = SignalParams(enter_threshold=0.30, exit_threshold=0.30, min_hold_secs=5.0)
    obs = [(0.0, 0.5), (1.0, -0.9), (6.0, -0.9)]
    assert _targets(params, obs) == [1, 1, -1]


def test_cooldown_blocks_re_entry_after_going_flat() -> None:
    params = SignalParams(enter_threshold=0.30, exit_threshold=0.30, cooldown_secs=3.0)
    obs = [(0.0, 0.5), (1.0, 0.0), (2.0, 0.9), (4.0, 0.9)]
    # flat at t=1; cooldown runs to t=4 before a new position is allowed
    assert _targets(params, obs) == [1, 0, 0, 1]


def test_cooldown_does_not_block_the_first_entry() -> None:
    params = SignalParams(enter_threshold=0.30, exit_threshold=0.30, cooldown_secs=60.0)
    assert _targets(params, [(0.0, 0.9)]) == [1]


def test_min_hold_and_cooldown_are_independent_gates() -> None:
    params = SignalParams(
        enter_threshold=0.30, exit_threshold=0.30, min_hold_secs=2.0, cooldown_secs=10.0
    )
    obs = [(0.0, 0.9), (1.0, 0.0), (2.0, 0.0), (5.0, 0.9), (12.0, 0.9)]
    # exit gated to t=2 by min_hold; re-entry gated to t=12 by cooldown from t=2
    assert _targets(params, obs) == [1, 1, 0, 0, 1]


# --- time-decayed smoothing -------------------------------------------------


def test_zero_halflife_means_no_smoothing() -> None:
    params = SignalParams(enter_threshold=0.30, exit_threshold=0.30, halflife_secs=0.0)
    states = fold_states([(0.0, 0.9), (1.0, -0.9)], params)
    assert [s.smoothed for s in states] == [0.9, -0.9]


def test_first_observation_seeds_the_average() -> None:
    params = SignalParams(enter_threshold=0.30, exit_threshold=0.30, halflife_secs=1.0)
    assert fold_states([(0.0, 0.4)], params)[0].smoothed == 0.4


def test_smoothing_matches_the_textbook_ema_at_a_constant_tick_rate() -> None:
    # With regular spacing the unit-mass average converges to s' = d*s + (1-d)*x.
    halflife, dt = 2.0, 0.5
    params = SignalParams(enter_threshold=0.30, exit_threshold=0.30, halflife_secs=halflife)
    decay = 0.5 ** (dt / halflife)

    obs = [(i * dt, 1.0) for i in range(200)] + [(200 * dt, 0.0)]
    states = fold_states(obs, params)
    # after 200 identical observations the average has saturated at 1.0
    assert states[-2].smoothed == pytest.approx(1.0, abs=1e-9)
    # and the next observation moves it by exactly (1 - decay)
    assert states[-1].smoothed == pytest.approx(decay * 1.0 + (1 - decay) * 0.0, abs=1e-6)


def test_simultaneous_observations_still_move_the_average() -> None:
    # 2.8% of recorded ticks share a timestamp; exp(-dt/tau) would discard them.
    params = SignalParams(enter_threshold=0.30, exit_threshold=0.30, halflife_secs=1.0)
    seeded = fold_states([(0.0, 1.0)], params)[0]
    after = update(seeded, ts=0.0, imbalance=0.0, params=params)
    assert 0.0 < after.smoothed < 1.0


def test_a_long_gap_resets_the_average_to_the_new_observation() -> None:
    # The lunch break is a ~2100s hole; stale pre-break imbalance must not leak across.
    params = SignalParams(enter_threshold=0.30, exit_threshold=0.30, halflife_secs=1.0)
    seeded = fold_states([(0.0, 1.0)], params)[0]
    after = update(seeded, ts=2100.0, imbalance=-1.0, params=params)
    assert after.smoothed == pytest.approx(-1.0, abs=1e-9)


def test_smoothing_absorbs_a_one_tick_spike() -> None:
    params = SignalParams(enter_threshold=0.30, exit_threshold=0.30, halflife_secs=5.0)
    obs = [(i * 0.1, 0.0) for i in range(50)] + [(5.0, 1.0), (5.1, 0.0)]
    assert _targets(params, obs) == [0] * 52
    # ...while the unsmoothed signal takes the bait on the spike tick
    assert _targets(BANG_BANG, obs) == [0] * 50 + [1, 0]


def test_smoothing_treats_a_backwards_timestamp_as_simultaneous() -> None:
    # A clock that goes backwards must not resurrect decayed mass (decay > 1).
    params = SignalParams(enter_threshold=0.30, exit_threshold=0.30, halflife_secs=1.0)
    seeded = fold_states([(10.0, 1.0)], params)[0]
    backwards = update(seeded, ts=9.0, imbalance=0.0, params=params)
    simultaneous = update(seeded, ts=10.0, imbalance=0.0, params=params)
    assert backwards.smoothed == pytest.approx(simultaneous.smoothed)
    assert backwards.last_ts == 10.0  # the clock never regresses


def test_a_backwards_clock_does_not_gate_a_target_change() -> None:
    # `_smooth` clamps dt; the hold/cooldown gate must clamp elapsed the same way,
    # or a negative elapsed silently blocks changes even with the gates disabled.
    params = SignalParams(enter_threshold=0.30, exit_threshold=0.30)
    long_state = fold_states([(10.0, 0.9)], params)[0]
    assert long_state.target == 1
    assert update(long_state, ts=9.0, imbalance=-0.9, params=params).target == -1


def test_missing_imbalance_does_not_decay_the_average() -> None:
    params = SignalParams(enter_threshold=0.30, exit_threshold=0.30, halflife_secs=1.0)
    states = fold_states([(0.0, 0.9), (100.0, None)], params)
    assert states[1].smoothed == states[0].smoothed


# --- purity / validation ----------------------------------------------------


def test_update_does_not_mutate_the_input_state() -> None:
    params = SignalParams(enter_threshold=0.30, exit_threshold=0.10, halflife_secs=1.0)
    before = fold_states([(0.0, 0.9)], params)[0]
    snapshot = (before.target, before.smoothed, before.mass, before.last_ts, before.target_ts)
    update(before, ts=1.0, imbalance=-0.9, params=params)
    assert (before.target, before.smoothed, before.mass, before.last_ts, before.target_ts) == snapshot


def test_update_is_deterministic() -> None:
    params = SignalParams(enter_threshold=0.30, exit_threshold=0.10, halflife_secs=1.0)
    state = fold_states([(0.0, 0.9)], params)[0]
    assert update(state, 1.0, 0.2, params) == update(state, 1.0, 0.2, params)


def test_initial_state_is_flat_and_empty() -> None:
    state = initial_state()
    assert state == SignalState(target=0, smoothed=None, mass=0.0, last_ts=None, target_ts=None)


def test_exit_threshold_above_entry_is_rejected() -> None:
    with pytest.raises(ValueError, match="exit_threshold"):
        SignalParams(enter_threshold=0.20, exit_threshold=0.30)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"enter_threshold": -0.1},
        {"enter_threshold": 1.5},
        {"exit_threshold": -0.1},
        {"halflife_secs": -1.0},
        {"min_hold_secs": -1.0},
        {"cooldown_secs": -1.0},
    ],
)
def test_out_of_range_params_are_rejected(kwargs: dict[str, float]) -> None:
    base = {"enter_threshold": 0.30, "exit_threshold": 0.10}
    with pytest.raises(ValueError):
        SignalParams(**{**base, **kwargs})


def test_baseline_preset_is_the_plain_threshold_flip() -> None:
    # The pre-churn-work signal, kept reproducible: no smoothing, no gates.
    assert BASELINE.enter_threshold == BASELINE.exit_threshold
    assert (BASELINE.halflife_secs, BASELINE.min_hold_secs, BASELINE.cooldown_secs) == (0, 0, 0)


def test_churn_controlled_preset_holds_until_the_book_flips() -> None:
    p = CHURN_CONTROLLED
    assert p.exit_threshold < p.enter_threshold  # hysteresis
    assert p.halflife_secs > 0  # smoothing
    # exit_threshold == 0 -> a long is only closed once the smoothed book turns negative
    assert _targets(p, [(0.0, 0.9), (1.0, 0.05)]) == [1, 1]


def test_smoothed_average_stays_within_the_imbalance_range() -> None:
    params = SignalParams(enter_threshold=0.30, exit_threshold=0.10, halflife_secs=3.0)
    obs = [(i * 0.1, math.sin(i) ) for i in range(500)]
    assert all(-1.0 <= s.smoothed <= 1.0 for s in fold_states(obs, params))
