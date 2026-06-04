from __future__ import annotations

import unittest

try:
    import numpy as np
    import pandas as pd
    import talib  # noqa: F401

    _DEPS = True
except Exception:  # pragma: no cover
    _DEPS = False


def _synthetic_ohlcv(n: int, seed: int = 11) -> "pd.DataFrame":
    rng = np.random.default_rng(seed)
    close = 1500.0 + np.cumsum(rng.normal(0.0, 5.0, size=n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    spread = np.abs(rng.normal(0.0, 8.0, size=n)) + 1.0
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = rng.integers(1_000, 50_000, size=n).astype("float64")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}
    )


@unittest.skipUnless(_DEPS, "requires numpy/pandas/TA-Lib")
class ForceEntryPriceTests(unittest.TestCase):
    def test_fill_price_and_time_match_hand_computed(self) -> None:
        from labels import calc_force_entry_price

        entry = np.array([100.0, 100.0, 100.0, 100.0])
        lo = np.array([200.0, 200.0, 50.0, 200.0])
        fep, fet = calc_force_entry_price(entry, lo, 1.0)
        # i=0 fills at j=2 (lo=50 < standing limit entry[1]=100), price=100, waited 2 bars
        self.assertAlmostEqual(fep[0], 100.0)
        self.assertAlmostEqual(fet[0], 2.0)
        # i=1 fills at j=2, waited 1 bar
        self.assertAlmostEqual(fep[1], 100.0)
        self.assertAlmostEqual(fet[1], 1.0)
        # i=2, i=3 never fill -> NaN
        self.assertTrue(np.isnan(fep[2]) and np.isnan(fep[3]))
        self.assertTrue(np.isnan(fet[2]) and np.isnan(fet[3]))


@unittest.skipUnless(_DEPS, "requires numpy/pandas/TA-Lib")
class OneWayCostVecTests(unittest.TestCase):
    def test_vectorized_matches_scalar_cost_model(self) -> None:
        from cost_model import CostModel, CostParams
        from labels import _one_way_cost_vec

        cm = CostModel(CostParams.default())
        prices = np.array([700.0, 1500.0, 3000.0, 3001.0, 5000.0, 5999.0])
        vec = _one_way_cost_vec(cm, prices)
        for p, v in zip(prices, vec):
            self.assertAlmostEqual(v, cm.one_way_cost_frac(float(p)))

    def test_non_positive_price_maps_to_nan(self) -> None:
        from cost_model import CostModel, CostParams
        from labels import _one_way_cost_vec

        cm = CostModel(CostParams.default())
        out = _one_way_cost_vec(cm, np.array([0.0, -5.0, np.nan, 1000.0]))
        self.assertTrue(np.isnan(out[0]) and np.isnan(out[1]) and np.isnan(out[2]))
        self.assertTrue(np.isfinite(out[3]))


@unittest.skipUnless(_DEPS, "requires numpy/pandas/TA-Lib")
class ComputeLabelsTests(unittest.TestCase):
    def _zero_cost(self):
        from cost_model import CostModel, CostParams

        return CostModel(
            CostParams(
                commission_rate=0.0,
                slippage_ticks=0.0,
                borrow_fee_annual=0.0,
                tick_table=CostParams.default().tick_table,
            )
        )

    def test_produces_tutorial_label_columns(self) -> None:
        from labels import compute_labels

        df = compute_labels(_synthetic_ohlcv(300), self._zero_cost())
        for col in (
            "buy_price", "sell_price", "buy_fep", "buy_fet", "sell_fep", "sell_fet",
            "buy_executed", "sell_executed", "y_buy", "y_sell", "buy_cost", "sell_cost",
        ):
            self.assertIn(col, df.columns)

    def test_unexecuted_rows_have_zero_y(self) -> None:
        from labels import compute_labels

        df = compute_labels(_synthetic_ohlcv(300), self._zero_cost())
        self.assertTrue((df.loc[df["buy_executed"] == 0.0, "y_buy"] == 0.0).all())
        self.assertTrue((df.loc[df["sell_executed"] == 0.0, "y_sell"] == 0.0).all())

    def test_cost_strictly_reduces_y_by_round_trip(self) -> None:
        """No maker rebate: a positive cost can only LOWER y, by exactly the
        round-trip cost on executed rows (the JP-equity adaptation of `2*fee`)."""
        from cost_model import CostModel, CostParams
        from labels import compute_labels

        params = CostParams(
            commission_rate=0.0005,
            slippage_ticks=1.0,
            borrow_fee_annual=0.0365,
            tick_table=CostParams.default().tick_table,
        )
        base = _synthetic_ohlcv(300)
        y0 = compute_labels(base.copy(), self._zero_cost())
        y1 = compute_labels(base.copy(), CostModel(params))

        # executed long rows with a finite exit price
        mask = (y0["buy_executed"] == 1.0) & np.isfinite(y0["y_buy"]) & np.isfinite(y1["y_buy"])
        self.assertGreater(mask.sum(), 0)
        diff = (y0.loc[mask, "y_buy"] - y1.loc[mask, "y_buy"]).to_numpy()
        # difference == round-trip cost = 2 * one-way at buy_price
        from labels import _one_way_cost_vec

        expected = 2.0 * _one_way_cost_vec(
            CostModel(params), y0.loc[mask, "buy_price"].to_numpy()
        )
        np.testing.assert_allclose(diff, expected, rtol=1e-9, atol=1e-12)
        # cost can only hurt
        self.assertTrue((diff >= -1e-12).all())

    def test_short_round_trip_includes_borrow_fee(self) -> None:
        from cost_model import CostModel, CostParams
        from labels import _one_way_cost_vec, compute_labels

        params = CostParams(
            commission_rate=0.0005,
            slippage_ticks=1.0,
            borrow_fee_annual=0.0365,
            tick_table=CostParams.default().tick_table,
        )
        base = _synthetic_ohlcv(300)
        hold = 2.0  # days, exaggerate borrow so it is measurable
        y0 = compute_labels(base.copy(), self._zero_cost(), holding_days=hold)
        y1 = compute_labels(base.copy(), CostModel(params), holding_days=hold)

        mask = (y0["sell_executed"] == 1.0) & np.isfinite(y0["y_sell"]) & np.isfinite(y1["y_sell"])
        self.assertGreater(mask.sum(), 0)
        diff = (y0.loc[mask, "y_sell"] - y1.loc[mask, "y_sell"]).to_numpy()
        borrow = 0.0365 * (hold / 365.0)
        expected = 2.0 * _one_way_cost_vec(
            CostModel(params), y0.loc[mask, "sell_price"].to_numpy()
        ) + borrow
        np.testing.assert_allclose(diff, expected, rtol=1e-9, atol=1e-12)


if __name__ == "__main__":
    unittest.main()
