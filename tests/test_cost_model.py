from __future__ import annotations

import unittest

from cost_model import CostModel, CostParams


class TickSizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cm = CostModel(CostParams.default())

    def test_jpx_tick_table_bands_within_universe_price_band(self) -> None:
        # Universe price band is 700 < close < 6000; the relevant ticks are
        # 1 (<=3000), 5 (<=5000), 10 (<=10000). Boundaries are inclusive-upper.
        self.assertEqual(self.cm.tick_size(700.0), 1.0)
        self.assertEqual(self.cm.tick_size(3000.0), 1.0)   # 3000以下 -> 1
        self.assertEqual(self.cm.tick_size(3000.5), 5.0)   # >3000   -> 5
        self.assertEqual(self.cm.tick_size(5000.0), 5.0)   # 5000以下 -> 5
        self.assertEqual(self.cm.tick_size(5000.5), 10.0)  # >5000   -> 10

    def test_tick_table_is_monotonic_non_decreasing(self) -> None:
        prices = [100, 1000, 3000, 5000, 10000, 30000, 50000, 300000]
        ticks = [self.cm.tick_size(p) for p in prices]
        self.assertEqual(ticks, sorted(ticks))


class CostFracTests(unittest.TestCase):
    def setUp(self) -> None:
        # Explicit params so the arithmetic is exact and independent of defaults.
        self.params = CostParams(
            commission_rate=0.0005,   # 5 bps per side
            slippage_ticks=1.0,       # 1 tick per side
            borrow_fee_annual=0.0365, # 3.65%/yr -> 0.01%/day
            tick_table=CostParams.default().tick_table,
        )
        self.cm = CostModel(self.params)

    def test_slippage_frac_uses_tick_at_price(self) -> None:
        # price=1000 -> tick=1 -> 1 tick / 1000 = 0.001 per side
        self.assertAlmostEqual(self.cm.slippage_frac(1000.0), 0.001)
        # price=4000 -> tick=5 -> 5/4000 = 0.00125 per side
        self.assertAlmostEqual(self.cm.slippage_frac(4000.0), 0.00125)

    def test_one_way_cost_is_commission_plus_slippage(self) -> None:
        # price=1000 -> 0.0005 + 0.001 = 0.0015
        self.assertAlmostEqual(self.cm.one_way_cost_frac(1000.0), 0.0015)

    def test_long_round_trip_has_no_borrow_fee(self) -> None:
        # round trip = 2 * one_way; LONG pays no 貸株料 regardless of holding days
        expected = 2 * 0.0015
        self.assertAlmostEqual(
            self.cm.round_trip_cost_frac(1000.0, "LONG", holding_days=5.0), expected
        )

    def test_short_round_trip_adds_borrow_fee_by_holding_days(self) -> None:
        # SHORT adds borrow_fee_annual * holding_days/365 on top of round trip.
        rt = 2 * 0.0015
        borrow = 0.0365 * (10.0 / 365.0)  # = 0.001
        self.assertAlmostEqual(
            self.cm.round_trip_cost_frac(1000.0, "SHORT", holding_days=10.0),
            rt + borrow,
        )

    def test_no_maker_rebate_cost_is_always_non_negative(self) -> None:
        # Crypto tutorial could have negative (rebate) fees; JP equities cannot.
        for price in (700.0, 1234.0, 5999.0):
            for side in ("LONG", "SHORT"):
                self.assertGreater(
                    self.cm.round_trip_cost_frac(price, side, holding_days=1.0), 0.0
                )

    def test_friction_floor_equals_round_trip_cost(self) -> None:
        # The friction floor (min gross edge to break even) is the round-trip cost.
        for side in ("LONG", "SHORT"):
            self.assertAlmostEqual(
                self.cm.friction_floor(2000.0, side, holding_days=1.0),
                self.cm.round_trip_cost_frac(2000.0, side, holding_days=1.0),
            )

    def test_invalid_side_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.cm.round_trip_cost_frac(1000.0, "BUY", holding_days=1.0)

    def test_non_positive_price_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.cm.tick_size(0.0)


class ConfigLoadTests(unittest.TestCase):
    def test_from_config_dict_overrides_defaults(self) -> None:
        cfg = {
            "cost": {
                "commission_rate": 0.001,
                "slippage_ticks": 2.0,
                "borrow_fee_annual": 0.02,
                # tick_table omitted -> falls back to default JPX table
            }
        }
        cm = CostModel.from_config_dict(cfg)
        self.assertEqual(cm.params.commission_rate, 0.001)
        self.assertEqual(cm.params.slippage_ticks, 2.0)
        self.assertEqual(cm.params.borrow_fee_annual, 0.02)
        self.assertEqual(cm.params.tick_table, CostParams.default().tick_table)

    def test_from_config_dict_missing_cost_section_uses_defaults(self) -> None:
        cm = CostModel.from_config_dict({})
        self.assertEqual(cm.params, CostParams.default())


if __name__ == "__main__":
    unittest.main()
