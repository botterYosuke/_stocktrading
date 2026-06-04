"""C9: backtest + p-mean accept/reject gate (#8 Phase 0c).

Port of the crypto tutorial's evaluation block (``example/tutorial.ipynb``
cell 16): the position-constrained numba backtest, ``scipy.stats.ttest_1samp``,
and the p-mean / type-1-error test. The accept/reject thresholds are frozen
here as a PRE-REGISTRATION constant block (handoff §8, grill-me discipline) so
the gate is auditable and cannot drift after the OOS number is seen.

--- The cross-sectional adaptation (a deliberate, documented divergence) -------
The tutorial backtest is a SINGLE-SERIES position-carrying simulator (one asset,
position carried bar-to-bar). Our panel is cross-sectional: each timestamp stacks
~``top_n`` codes. Running the single-series sim over the stacked rows would carry
a position across *different stocks* — nonsense. So:

  * ``backtest(...)`` is kept verbatim from the tutorial as a tested single-series
    utility (use it per-instrument if you want the position-carrying P&L).
  * the GATE instead uses the metric handoff §3 mandates — the time-ordered,
    per-timestamp portfolio return over the ``y_pred>0``-selected bars
    ("予測がプラスのときだけトレード"). Realised ``y_buy``/``y_sell`` are already
    net of round-trip cost (see ``labels.py``), so the selected-return stream is
    a net return series; ``ttest_1samp`` + ``calc_p_mean`` run on it directly.

--- Ground truth that frames the verdict (handoff §3) -------------------------
Raw ``y_buy``/``y_sell`` means are BOTH negative after JP costs (no maker rebate
+ 貸株料). The naive (trade-every-bar) strategy is expected to be REJECTED. The
live question is solely whether selecting ``y_pred>0`` bars lifts the net mean
above zero with a p-mean type-1 error rate ≤ 1e-5.

This module is **additive** — it does not touch ``model_manager`` / ``signals_writer``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

try:  # numba optional: jit in production, identical pure-Python fallback here.
    from numba import njit
except Exception:  # pragma: no cover - exercised on toolchain-less envs
    def njit(*args, **kwargs):
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]

        def _decorate(func):
            return func

        return _decorate


# --- PRE-REGISTRATION: frozen accept/reject lines (do NOT tune post-hoc) -------
P_MEAN_N = 5  # tutorial cell 16 default
TYPE1_ERROR_MAX = 1e-5  # gate: calc_p_mean_type1_error_rate must be <= this
# A side passes only if its selected net mean return is strictly positive AND its
# p-mean type-1 error clears TYPE1_ERROR_MAX. Buy and SHORT sides are judged
# separately (R3): SHORT bears 貸株料 so its friction floor is strictly higher.


@njit
def backtest(cl, buy_entry, sell_entry, buy_cost, sell_cost):
    """Tutorial single-series position-constrained backtest (verbatim logic).

    ``cl`` close prices; ``buy_entry``/``sell_entry`` boolean entry signals;
    ``buy_cost``/``sell_cost`` per-bar entry/exit cost fractions. Max |position|
    is 1.0. Exits run before entries. Returns ``(cum_ret, positions)``.
    """
    n = cl.size
    y = cl.copy() * 0.0
    poss = cl.copy() * 0.0
    ret = 0.0
    pos = 0.0
    for i in range(n):
        prev_pos = pos
        # exit
        if buy_cost[i]:
            vol = np.maximum(0.0, -prev_pos)
            ret -= buy_cost[i] * vol
            pos += vol
        if sell_cost[i]:
            vol = np.maximum(0.0, prev_pos)
            ret -= sell_cost[i] * vol
            pos -= vol
        # entry
        if buy_entry[i] and buy_cost[i]:
            vol = np.minimum(1.0, 1.0 - prev_pos) * buy_entry[i]
            ret -= buy_cost[i] * vol
            pos += vol
        if sell_entry[i] and sell_cost[i]:
            vol = np.minimum(1.0, prev_pos + 1.0) * sell_entry[i]
            ret -= sell_cost[i] * vol
            pos -= vol
        if i + 1 < n:
            ret += pos * (cl[i + 1] / cl[i] - 1.0)
        y[i] = ret
        poss[i] = pos
    return y, poss


def calc_p_mean(x, n: int) -> float:
    """Tutorial p-mean: split ``x`` into ``n`` ordered chunks, one-sided t-test
    each (p only counts when t>0, else 1), return the mean p. ``x`` MUST be
    time-ordered for the chunking to be meaningful."""
    from scipy.stats import ttest_1samp

    x = np.asarray(x, dtype="float64")
    ps = []
    for i in range(n):
        x2 = x[i * x.size // n : (i + 1) * x.size // n]
        # Degenerate chunk -> contributes p=1 (tutorial intent). Use peak-to-peak
        # rather than the tutorial's ``np.std(x2)==0``: std of bit-identical
        # floats is ~1e-18 (catastrophic cancellation), not 0, which would feed
        # near-constant data to the t-test and yield a SPURIOUS tiny p. ``ptp``
        # is exactly 0 for identical values, so the guard can't be fooled.
        if x2.size == 0 or np.ptp(x2) == 0:
            ps.append(1.0)
        else:
            t, p = ttest_1samp(x2, 0)
            ps.append(p if t > 0 else 1.0)
    return float(np.mean(ps))


def calc_p_mean_type1_error_rate(p_mean: float, n: int) -> float:
    """Tutorial type-1 error rate of the p-mean test = (p_mean*n)^n / n!."""
    return (p_mean * n) ** n / math.factorial(n)


def selected_returns(panel: pd.DataFrame) -> pd.DataFrame:
    """Time-ordered per-timestamp portfolio returns over y_pred>0-selected bars.

    For each timestamp: ``buy`` = mean ``y_buy`` over rows with ``y_pred_buy>0``
    (0 if none selected), ``sell`` likewise, ``combined`` = buy+sell. Index is
    the sorted unique timestamp; this is the net-return stream fed to the gate.
    """
    df = panel.copy()
    buy_sel = df["y_pred_buy"] > 0
    sell_sel = df["y_pred_sell"] > 0
    df["_buy_r"] = np.where(buy_sel, df["y_buy"], np.nan)
    df["_sell_r"] = np.where(sell_sel, df["y_sell"], np.nan)
    grp = df.groupby("timestamp", sort=True)
    out = pd.DataFrame(
        {
            "buy": grp["_buy_r"].mean().fillna(0.0),
            "sell": grp["_sell_r"].mean().fillna(0.0),
        }
    )
    out["combined"] = out["buy"] + out["sell"]
    return out


@dataclass
class SideResult:
    side: str  # "buy" | "sell" | "combined"
    n_periods: int
    mean_ret: float
    t_stat: float
    p_value: float
    p_mean: float
    type1_error_rate: float
    friction_floor: float  # reference break-even (per-side round-trip cost frac)
    passes: bool


@dataclass
class GateResult:
    verdict: str  # "GO" | "REJECT" | "DOWNGRADE"
    reason: str
    sides: dict = field(default_factory=dict)  # side -> SideResult
    leakage_smoke: dict = field(default_factory=dict)  # kind -> combined mean_ret

    def __str__(self) -> str:  # human-readable gate report
        lines = [f"VERDICT: {self.verdict} — {self.reason}"]
        for s in self.sides.values():
            lines.append(
                f"  [{s.side:8}] n={s.n_periods} mean={s.mean_ret:+.6f} "
                f"t={s.t_stat:+.2f} p={s.p_value:.3g} p_mean={s.p_mean:.3g} "
                f"err={s.type1_error_rate:.3g} floor~{s.friction_floor:.5f} "
                f"-> {'PASS' if s.passes else 'fail'}"
            )
        if self.leakage_smoke:
            sm = "  ".join(f"{k}={v:+.6f}" for k, v in self.leakage_smoke.items())
            lines.append(f"  leakage-smoke(combined mean): {sm}")
        return "\n".join(lines)


def _side_result(side: str, r: np.ndarray, floor: float, n: int) -> SideResult:
    from scipy.stats import ttest_1samp

    r = np.asarray(r, dtype="float64")
    if r.size < n or np.std(r) == 0:
        return SideResult(side, r.size, float(np.mean(r) if r.size else 0.0),
                          0.0, 1.0, 1.0, 1.0, floor, False)
    t, p = ttest_1samp(r, 0)
    pm = calc_p_mean(r, n)
    err = calc_p_mean_type1_error_rate(pm, n)
    mean_ret = float(np.mean(r))
    passes = (mean_ret > 0.0) and (err <= TYPE1_ERROR_MAX)
    return SideResult(side, r.size, mean_ret, float(t), float(p), pm, err, floor, passes)


def _median_selected_price(panel: pd.DataFrame, pred_col: str) -> float:
    sel = panel[panel[pred_col] > 0]
    if len(sel) == 0:
        return float("nan")
    return float(sel["close"].median())


def evaluate_gate(
    panel_with_preds: pd.DataFrame,
    cost_model,
    *,
    n: int = P_MEAN_N,
    holding_days: float = 1.0 / 24.0,
) -> GateResult:
    """Run the pre-registered accept/reject gate on an OOS-predicted panel.

    Drops rows with NaN predictions (tutorial parity), builds the time-ordered
    per-timestamp selected-return streams, and judges buy / sell / combined
    against the frozen thresholds. ``combined`` drives the verdict.
    """
    panel = panel_with_preds.dropna(subset=["y_pred_buy", "y_pred_sell"]).copy()
    streams = selected_returns(panel)

    buy_floor = cost_model.friction_floor(
        _median_selected_price(panel, "y_pred_buy"), "LONG", holding_days
    )
    sell_floor = cost_model.friction_floor(
        _median_selected_price(panel, "y_pred_sell"), "SHORT", holding_days
    )
    sides = {
        "buy": _side_result("buy", streams["buy"].to_numpy(), buy_floor, n),
        "sell": _side_result("sell", streams["sell"].to_numpy(), sell_floor, n),
        "combined": _side_result(
            "combined", streams["combined"].to_numpy(), max(buy_floor, sell_floor), n
        ),
    }

    combined = sides["combined"]
    if combined.passes:
        verdict, reason = "GO", "combined selected net return clears p-mean gate"
    elif combined.mean_ret <= 0:
        verdict, reason = "REJECT", "combined selected net return <= 0 (naive-cost wall)"
    else:
        verdict, reason = (
            "DOWNGRADE",
            f"combined net>0 but type-1 error {combined.type1_error_rate:.3g} > {TYPE1_ERROR_MAX:g}",
        )
    return GateResult(verdict=verdict, reason=reason, sides=sides)
