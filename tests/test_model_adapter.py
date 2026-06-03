from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import date

from data_source import DailyBar

# model_manager pulls in numpy/pandas/sklearn (the brain deps). They live only in
# the Python 3.12 + TensorFlow brain venv, not in the stdlib-only tooling env, so
# these checks self-skip when the deps are absent and run in the brain venv.
_HAS_BRAIN_DEPS = all(
    importlib.util.find_spec(m) is not None for m in ("numpy", "pandas", "sklearn")
)


@unittest.skipUnless(_HAS_BRAIN_DEPS, "brain deps (numpy/pandas/sklearn) not installed")
class ModelManagerLazyImportTests(unittest.TestCase):
    def test_import_does_not_pull_in_tensorflow(self) -> None:
        """B2-1: model_manager must import without loading TensorFlow."""
        sys.modules.pop("tensorflow", None)
        import model_manager  # noqa: F401

        self.assertNotIn("tensorflow", sys.modules)

    def test_daily_bars_to_frame_shape_and_values(self) -> None:
        """B2-2: DailyBar list adapts to the feature-pipeline DataFrame shape."""
        import model_manager

        bars = [
            DailyBar(date=date(2026, 1, 5), code="7203", open=100.0, high=110.0, low=90.0, close=105.0, volume=1000),
            DailyBar(date=date(2026, 1, 6), code="7203", open=105.0, high=115.0, low=95.0, close=108.0, volume=2000),
        ]
        df = model_manager.daily_bars_to_frame(bars)

        self.assertEqual(list(df.columns), ["date", "open", "high", "low", "close", "volume"])
        self.assertEqual(df["close"].tolist(), [105.0, 108.0])
        self.assertEqual(df["date"].tolist(), [date(2026, 1, 5), date(2026, 1, 6)])

    def test_predict_empty_is_safe(self) -> None:
        """B2 empty-guard: predict returns an empty [date,code,pred] frame (no crash)
        when no code clears the 0.7 threshold."""
        import numpy as np
        import pandas as pd

        import model_manager

        mm = model_manager.ModelManager()
        mm.codes = ["7203", "6758"]
        dict_df = {c: pd.DataFrame(np.zeros((40, 5))) for c in mm.codes}

        class _FakeModel:
            def predict(self, x, verbose=0):
                return np.array([[0.1]])  # below threshold -> zero candidates

        out = mm.predict(_FakeModel(), dict_df, "2021-06-30")

        self.assertEqual(list(out.columns), ["date", "code", "pred"])
        self.assertEqual(len(out), 0)


if __name__ == "__main__":
    unittest.main()
