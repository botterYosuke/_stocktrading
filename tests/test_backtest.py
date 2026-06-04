from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta

try:
    import numpy as np
    import pandas as pd
    import scipy  # noqa: F401

    _DEPS = True
except Exception:  # pragma: no cover
    _DEPS = False


def _zero_cost():
    from cost_model import CostModel, CostParams

    return CostModel(
        CostParams(
            commission_rate=0.0,
            slippage_ticks=0.0,
            borrow_fee_annual=0.0,
            tick_table=CostParams.default().tick_table,
        )
    )


def _pred_panel(returns, *, seed=0):
    """A 1-code-per-timestamp OOS-predicted panel where the buy side is always
    selected (y_pred_buy=1) with the given per-timestamp y_buy ``returns``, and
    the sell side is never selected (y_pred_sell=-1)."""
    t0 = datetime(2024, 1, 9, 9, 0)
    rows = []
    for i, r in enumerate(returns):
        rows.append(
            {
                "code": "7203",
                "timestamp": t0 + timedelta(minutes=15 * i),
                "close": 1500.0,
                "y_buy": float(r),
                "y_sell": -float(r),
                "y_pred_buy": 1.0,
                "y_pred_sell": -1.0,
            }
        )
    return pd.DataFrame(rows)


@unittest.skipUnless(_DEPS, "requires numpy/pandas/scipy")
class BacktestTraceTests(unittest.TestCase):
    def test_single_long_trade_accumulates_price_move_minus_cost(self) -> None:
        from backtest import backtest

        cl = np.array([100.0, 110.0])
        buy_entry = np.array([1.0, 1.0])
        sell_entry = np.array([0.0, 0.0])
        buy_cost = np.array([0.001, 0.001])  # entry only fires on costed bars
        sell_cost = np.array([0.0, 0.0])
        y, poss = backtest(cl, buy_entry, sell_entry, buy_cost, sell_cost)
        # entry cost -0.001 at i0, then +1.0*(110/100-1)=+0.1 -> 0.099
        self.assertAlmostEqual(y[-1], 0.099, places=9)
        self.assertEqual(poss[-1], 1.0)

    def test_no_entry_when_cost_is_zero(self) -> None:
        """Tutorial quirk: entry is gated on a non-zero (i.e. executed) cost."""
        from backtest import backtest

        cl = np.array([100.0, 110.0])
        y, poss = backtest(
            cl, np.array([1.0, 1.0]), np.array([0.0, 0.0]),
            np.array([0.0, 0.0]), np.array([0.0, 0.0]),
        )
        self.assertEqual(poss[-1], 0.0)
        self.assertEqual(y[-1], 0.0)


@unittest.skipUnless(_DEPS, "requires numpy/pandas/scipy")
class PMeanTests(unittest.TestCase):
    def test_zero_variance_chunk_contributes_p_one(self) -> None:
        from backtest import calc_p_mean

        # constant series -> every chunk has std 0 -> p_mean == 1.0
        self.assertEqual(calc_p_mean(np.full(50, 0.01), 5), 1.0)

    def test_type1_error_rate_matches_formula(self) -> None:
        from backtest import calc_p_mean_type1_error_rate

        for p_mean, n in [(1.0, 5), (0.01, 5), (0.2, 3)]:
            self.assertAlmostEqual(
                calc_p_mean_type1_error_rate(p_mean, n),
                (p_mean * n) ** n / math.factorial(n),
                places=12,
            )

    def test_strong_positive_signal_has_tiny_p_mean(self) -> None:
        from backtest import calc_p_mean

        x = 0.01 + 0.001 * np.sin(np.arange(60))  # all positive, low variance
        self.assertLess(calc_p_mean(x, 5), 1e-3)


@unittest.skipUnless(_DEPS, "requires numpy/pandas/scipy")
class SelectedReturnsTests(unittest.TestCase):
    def test_only_positive_pred_rows_are_selected_and_aggregated(self) -> None:
        from backtest import selected_returns

        t0 = datetime(2024, 1, 9, 9, 0)
        rows = [
            # ts0: one code selected on buy (0.02), one not selected (-pred)
            {"code": "A", "timestamp": t0, "close": 1500.0, "y_buy": 0.02,
             "y_sell": 0.0, "y_pred_buy": 0.5, "y_pred_sell": -1.0},
            {"code": "B", "timestamp": t0, "close": 1500.0, "y_buy": 0.10,
             "y_sell": 0.0, "y_pred_buy": -0.5, "y_pred_sell": -1.0},
        ]
        out = selected_returns(pd.DataFrame(rows))
        # only A's buy is selected -> mean over selected = 0.02
        self.assertAlmostEqual(out.loc[t0, "buy"], 0.02, places=9)
        self.assertAlmostEqual(out.loc[t0, "sell"], 0.0, places=9)


@unittest.skipUnless(_DEPS, "requires numpy/pandas/scipy")
class EvaluateGateTests(unittest.TestCase):
    def test_negative_selected_return_is_rejected(self) -> None:
        from backtest import evaluate_gate

        panel = _pred_panel(-0.001 + 0.0001 * np.sin(np.arange(60)))
        res = evaluate_gate(panel, _zero_cost())
        self.assertEqual(res.verdict, "REJECT")
        self.assertFalse(res.sides["combined"].passes)

    def test_strong_positive_selected_return_is_go(self) -> None:
        from backtest import evaluate_gate

        panel = _pred_panel(0.01 + 0.001 * np.sin(np.arange(60)))
        res = evaluate_gate(panel, _zero_cost())
        self.assertEqual(res.verdict, "GO")
        self.assertTrue(res.sides["combined"].passes)
        self.assertLessEqual(res.sides["combined"].type1_error_rate, 1e-5)

    def test_nan_predictions_are_dropped(self) -> None:
        from backtest import evaluate_gate

        panel = _pred_panel(0.01 + 0.001 * np.sin(np.arange(60)))
        panel.loc[0:5, "y_pred_buy"] = np.nan  # first block unpredicted
        res = evaluate_gate(panel, _zero_cost())
        # still evaluates on the remaining rows without raising
        self.assertIn(res.verdict, {"GO", "REJECT", "DOWNGRADE"})


if __name__ == "__main__":
    unittest.main()
