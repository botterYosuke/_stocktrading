from __future__ import annotations

import unittest

try:
    import numpy as np
    import pandas as pd
    import talib  # noqa: F401

    _DEPS = True
except Exception:  # pragma: no cover - bare env without sci stack
    _DEPS = False


def _synthetic_ohlcv(n: int, seed: int = 7) -> "pd.DataFrame":
    """Deterministic OHLCV: a gentle random walk with intrabar high/low/volume."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 5.0, size=n)
    close = 1500.0 + np.cumsum(steps)
    open_ = np.concatenate([[close[0]], close[:-1]])
    spread = np.abs(rng.normal(0.0, 8.0, size=n)) + 1.0
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = rng.integers(1_000, 50_000, size=n).astype("float64")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}
    )


@unittest.skipUnless(_DEPS, "requires numpy/pandas/TA-Lib (run via `uv run --with TA-Lib`)")
class FeaturesIntradayTests(unittest.TestCase):
    def setUp(self) -> None:
        from features_intraday import FEATURES, calc_features

        self.FEATURES = FEATURES
        self.calc_features = calc_features

    def test_all_declared_features_present(self) -> None:
        df = self.calc_features(_synthetic_ohlcv(200))
        for col in self.FEATURES:
            self.assertIn(col, df.columns, f"missing feature column {col}")

    def test_causality_future_bars_do_not_change_past_features(self) -> None:
        """tutorial の核: 未来情報が混入しない。

        全系列で計算した行 t の特徴量は、t より後の bar を切り落としても不変で
        なければならない（TA-Lib は因果的＝左→右なので厳密一致するはず）。
        """
        full = _synthetic_ohlcv(300)
        n = 220  # compare features at the last row of the truncated frame
        feat_full = self.calc_features(full.iloc[: n + 1].copy())  # rows 0..n (no future)
        # baseline that additionally sees the future tail:
        feat_future = self.calc_features(full.copy())

        row_trunc = feat_full[self.FEATURES].iloc[n]
        row_future = feat_future[self.FEATURES].iloc[n]
        # NaN-aware exact comparison.
        pd.testing.assert_series_equal(
            row_trunc, row_future, check_names=False, rtol=0, atol=0
        )

    def test_bounded_oscillators_stay_in_range(self) -> None:
        df = self.calc_features(_synthetic_ohlcv(400)).dropna()
        self.assertTrue(((df["RSI"] >= 0) & (df["RSI"] <= 100)).all())
        self.assertTrue(((df["WILLR"] >= -100) & (df["WILLR"] <= 0)).all())
        self.assertTrue(((df["STOCH_slowk"] >= 0) & (df["STOCH_slowk"] <= 100)).all())
        self.assertTrue(((df["ULTOSC"] >= 0) & (df["ULTOSC"] <= 100)).all())

    def test_price_level_features_are_small_after_normalization(self) -> None:
        # (indicator - hilo)/close should be ~O(1e-2), not raw yen.
        df = self.calc_features(_synthetic_ohlcv(400)).dropna()
        for col in ("EMA", "MA", "WMA", "BBANDS_middleband"):
            self.assertLess(df[col].abs().median(), 0.5, f"{col} not normalized")

    def test_warmup_is_nan_then_finite(self) -> None:
        df = self.calc_features(_synthetic_ohlcv(400))
        # Earliest rows are warmup (NaN), the tail is fully populated.
        self.assertTrue(df[self.FEATURES].iloc[0].isna().any())
        self.assertTrue(np.isfinite(df[self.FEATURES].iloc[-1].to_numpy()).all())

    def test_input_columns_preserved(self) -> None:
        df = self.calc_features(_synthetic_ohlcv(120))
        for col in ("open", "high", "low", "close", "volume"):
            self.assertIn(col, df.columns)


if __name__ == "__main__":
    unittest.main()
