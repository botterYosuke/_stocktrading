from __future__ import annotations

from stocktrading.signals import SignalParams, imbalance_target


def test_imbalance_target_thresholds() -> None:
    params = SignalParams(threshold=0.30)
    assert imbalance_target(0.5, params) == 1
    assert imbalance_target(-0.5, params) == -1
    assert imbalance_target(0.1, params) == 0
    assert imbalance_target(-0.1, params) == 0
    assert imbalance_target(None, params) == 0


def test_imbalance_target_boundary_is_flat() -> None:
    params = SignalParams(threshold=0.30)
    # exactly at the threshold is not enough to take a side
    assert imbalance_target(0.30, params) == 0
