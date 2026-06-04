from __future__ import annotations

import unittest
from datetime import datetime, timedelta

# Two tiers: the R1 leakage guard + split logic need only numpy/pandas/sklearn
# (runs on this dev machine); fitting LightGBM additionally needs libomp, which
# is absent here (cf. numba/llvmlite, handoff §6) — those tests skip gracefully.
try:
    import numpy as np
    import pandas as pd
    import sklearn  # noqa: F401

    _SCI = True
except Exception:  # pragma: no cover
    _SCI = False

try:
    import lightgbm  # noqa: F401

    _LGBM = _SCI
except Exception:  # pragma: no cover
    _LGBM = False


def _synthetic_panel(n_ts: int = 40, codes=("7203", "6758", "9984"), seed: int = 0):
    """A cross-sectional panel: each timestamp stacks every code (genuine R1
    cross-section). Features are random; targets carry a faint learnable signal
    so OOS predictions are not degenerate."""
    from features_intraday import FEATURES

    rng = np.random.default_rng(seed)
    t0 = datetime(2024, 1, 9, 9, 0)
    rows = []
    for i in range(n_ts):
        ts = t0 + timedelta(minutes=15 * i)
        for code in codes:
            feats = {f: float(rng.normal()) for f in FEATURES}
            signal = feats[FEATURES[0]] * 0.001
            rows.append(
                {
                    "code": code,
                    "timestamp": ts,
                    "y_buy": signal + float(rng.normal(0, 0.0005)),
                    "y_sell": -signal + float(rng.normal(0, 0.0005)),
                    **feats,
                }
            )
    panel = pd.DataFrame(rows).sort_values(["timestamp", "code"], kind="stable")
    return panel.reset_index(drop=True)


@unittest.skipUnless(_SCI, "requires numpy/pandas/scikit-learn")
class MakeSplitterTests(unittest.TestCase):
    def test_kinds_and_unknown(self) -> None:
        from model_lgbm import make_splitter

        for kind in ("grouped", "timeseries", "naive_kfold"):
            k, sk = make_splitter(kind, n_splits=3)
            self.assertEqual(k, kind)
            self.assertIsNotNone(sk)
        with self.assertRaises(ValueError):
            make_splitter("nonsense")


@unittest.skipUnless(_SCI, "requires numpy/pandas/scikit-learn")
class LeakageGuardTests(unittest.TestCase):
    """R1: the timestamp-disjointness guard is the whole point of Phase 0c CV."""

    def test_grouped_split_has_no_timestamp_in_both_train_and_val(self) -> None:
        from model_lgbm import _assert_no_timestamp_leakage, _make_splits, make_splitter

        panel = _synthetic_panel()
        kind, sk = make_splitter("grouped", n_splits=5)
        splits = _make_splits(kind, sk, panel)
        # Direct property: every fold's train/val timestamps are disjoint.
        ts = panel["timestamp"].to_numpy()
        for train_idx, val_idx in splits:
            self.assertTrue(set(ts[train_idx].tolist()).isdisjoint(set(ts[val_idx].tolist())))
        # And the guard accepts it.
        _assert_no_timestamp_leakage(panel, splits)

    def test_guard_raises_on_a_leaky_naive_split(self) -> None:
        """A shuffled row KFold puts the same timestamp on both sides — the guard
        MUST catch it. This proves the assertion bites rather than being inert."""
        from model_lgbm import _assert_no_timestamp_leakage, _make_splits, make_splitter

        panel = _synthetic_panel()
        kind, sk = make_splitter("naive_kfold", n_splits=5)
        splits = _make_splits(kind, sk, panel)
        with self.assertRaises(AssertionError):
            _assert_no_timestamp_leakage(panel, splits)

    @unittest.skipUnless(_LGBM, "requires lightgbm (libomp)")
    def test_oos_predict_grouped_is_leakage_checked(self) -> None:
        from model_lgbm import oos_predict

        panel = _synthetic_panel()
        out = oos_predict(panel)  # default grouped: must not raise
        self.assertIn("y_pred_buy", out.columns)
        self.assertIn("y_pred_sell", out.columns)


@unittest.skipUnless(_LGBM, "requires lightgbm (libomp)")
class OosPredictTests(unittest.TestCase):
    def test_grouped_predicts_every_row(self) -> None:
        from model_lgbm import make_splitter, oos_predict

        panel = _synthetic_panel()
        out = oos_predict(panel, splitter=make_splitter("grouped", n_splits=5))
        # GroupKFold covers every row exactly once -> no NaN predictions.
        self.assertFalse(out["y_pred_buy"].isna().any())
        self.assertFalse(out["y_pred_sell"].isna().any())
        self.assertEqual(len(out), len(panel))

    def test_timeseries_leaves_first_block_unpredicted(self) -> None:
        from model_lgbm import make_splitter, oos_predict

        panel = _synthetic_panel()
        out = oos_predict(panel, splitter=make_splitter("timeseries", n_splits=5))
        # TimeSeriesSplit never predicts the earliest block -> some NaN.
        self.assertTrue(out["y_pred_buy"].isna().any())

    def test_predict_with_applies_fitted_models_once(self) -> None:
        from model_lgbm import predict_with, train_models

        dev = _synthetic_panel(seed=1)
        held = _synthetic_panel(seed=2)
        out = predict_with(train_models(dev), held)
        self.assertEqual(len(out), len(held))
        self.assertFalse(out["y_pred_buy"].isna().any())
        self.assertFalse(out["y_pred_sell"].isna().any())

    def test_train_models_returns_two_fitted_regressors(self) -> None:
        from model_lgbm import train_models

        panel = _synthetic_panel()
        mb, ms = train_models(panel)
        from features_intraday import FEATURES

        X = panel[FEATURES].to_numpy(dtype="float64")
        self.assertEqual(mb.predict(X).shape[0], len(panel))
        self.assertEqual(ms.predict(X).shape[0], len(panel))


if __name__ == "__main__":
    unittest.main()
