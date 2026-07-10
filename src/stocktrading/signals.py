from __future__ import annotations

from dataclasses import dataclass

# Pure, dependency-free signal logic. The SAME function is meant to drive both
# the Python order-book backtest (over silver/gold) and the backcast marimo cell
# (live via the kabu adapter), so keep it free of I/O and framework imports.


@dataclass(frozen=True)
class SignalParams:
    threshold: float = 0.30  # L1 imbalance magnitude required to take a side


def imbalance_target(imbalance: float | None, params: SignalParams) -> int:
    """Target position in units from top-of-book imbalance.

    +1 = long, -1 = short, 0 = flat. Imbalance is (bid_qty - ask_qty) /
    (bid_qty + ask_qty) in [-1, +1]; buy pressure (positive) leans long.
    """
    if imbalance is None:
        return 0
    if imbalance > params.threshold:
        return 1
    if imbalance < -params.threshold:
        return -1
    return 0
