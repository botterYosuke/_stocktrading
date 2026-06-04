from __future__ import annotations

import unittest
from datetime import datetime, timedelta

try:
    import numpy as np
    import pandas as pd  # noqa: F401
    import talib  # noqa: F401

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


def _synthetic_bars(code: str, n: int, seed: int):
    """A list of MinuteBar at sequential 15-min timestamps shared across codes
    (same seed-independent timestamps -> genuine cross-section)."""
    from minute_data_source import MinuteBar

    rng = np.random.default_rng(seed)
    close = 1500.0 + np.cumsum(rng.normal(0.0, 5.0, size=n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    spread = np.abs(rng.normal(0.0, 8.0, size=n)) + 1.0
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    vol = rng.integers(1_000, 50_000, size=n)
    t0 = datetime(2024, 1, 9, 9, 0)
    bars = []
    for i in range(n):
        bars.append(
            MinuteBar(
                timestamp=t0 + timedelta(minutes=15 * i),
                code=code,
                open=float(open_[i]),
                high=float(high[i]),
                low=float(low[i]),
                close=float(close[i]),
                volume=int(vol[i]),
                value=float(vol[i]) * float(close[i]),
            )
        )
    return bars


@unittest.skipUnless(_DEPS, "requires numpy/pandas/TA-Lib")
class AssemblePanelTests(unittest.TestCase):
    def setUp(self) -> None:
        from panel_builder import assemble_panel

        self.assemble_panel = assemble_panel
        self.panel_by_code = {
            "7203": _synthetic_bars("7203", 120, seed=1),
            "6758": _synthetic_bars("6758", 120, seed=2),
            "9984": _synthetic_bars("9984", 120, seed=3),
        }

    def test_panel_has_code_timestamp_features_and_targets(self) -> None:
        from features_intraday import FEATURES

        panel = self.assemble_panel(self.panel_by_code, _zero_cost())
        for col in ["code", "timestamp", "y_buy", "y_sell", *FEATURES]:
            self.assertIn(col, panel.columns)
        self.assertGreater(len(panel), 0)

    def test_cross_section_multiple_codes_per_timestamp(self) -> None:
        """R1 premise: a timestamp groups several codes -> CV must split on
        timestamp, not on stacked row."""
        panel = self.assemble_panel(self.panel_by_code, _zero_cost())
        per_ts = panel.groupby("timestamp")["code"].nunique()
        self.assertGreater(per_ts.max(), 1)
        self.assertEqual(set(panel["code"]), {"7203", "6758", "9984"})

    def test_sorted_by_timestamp_then_code(self) -> None:
        panel = self.assemble_panel(self.panel_by_code, _zero_cost())
        expected = panel.sort_values(["timestamp", "code"], kind="stable").reset_index(drop=True)
        pd.testing.assert_frame_equal(panel, expected)

    def test_dropna_leaves_no_feature_or_target_nan(self) -> None:
        from features_intraday import FEATURES

        panel = self.assemble_panel(self.panel_by_code, _zero_cost(), dropna=True)
        self.assertFalse(panel[FEATURES + ["y_buy", "y_sell"]].isna().any().any())

    def test_min_bars_skips_short_series(self) -> None:
        codes = dict(self.panel_by_code)
        codes["1301"] = _synthetic_bars("1301", 10, seed=9)  # too short
        panel = self.assemble_panel(codes, _zero_cost(), min_bars=64)
        self.assertNotIn("1301", set(panel["code"]))

    def test_empty_input_returns_typed_empty_panel(self) -> None:
        from features_intraday import FEATURES

        panel = self.assemble_panel({}, _zero_cost())
        self.assertEqual(len(panel), 0)
        for col in ["code", "timestamp", "y_buy", "y_sell", *FEATURES]:
            self.assertIn(col, panel.columns)


try:
    import pyarrow  # noqa: F401

    _PARQUET = _DEPS
except Exception:  # pragma: no cover
    _PARQUET = False


@unittest.skipUnless(_PARQUET, "requires pandas/pyarrow")
class PanelCacheTests(unittest.TestCase):
    def test_cache_hit_returns_parquet_without_building(self) -> None:
        """If the parquet cache exists, build_panel returns it verbatim and does
        not touch the data layer (passes a bogus cache_dir to prove no I/O)."""
        import tempfile
        from pathlib import Path

        from panel_builder import build_panel

        cached = pd.DataFrame({"code": ["7203"], "timestamp": [0], "y_buy": [0.01]})
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "panel.parquet")
            cached.to_parquet(path, index=False)
            out = build_panel(
                start="2024-01-01", end="2024-01-31", cost_model=_zero_cost(),
                cache_dir="/nonexistent/should/not/be/read", panel_cache=path,
            )
        pd.testing.assert_frame_equal(out, cached)


if __name__ == "__main__":
    unittest.main()
