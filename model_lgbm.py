"""C8: LightGBM y_buy/y_sell models + leakage-safe OOS prediction (#8 Phase 0c).

Faithful port of the crypto tutorial's model block (``example/tutorial.ipynb``
cell 14): two ``lgb.LGBMRegressor`` — one for ``y_buy``, one for ``y_sell`` — and
a ``my_cross_val_predict`` that stitches per-fold out-of-sample predictions back
into the panel.

The ONE deliberate divergence from the tutorial is the CV split. The tutorial
uses a naive row ``KFold``; on a *cross-sectional* panel that leaks
catastrophically — every timestamp stacks ~``top_n`` codes that share the same
market-wide move, so a random row split puts the same instant in both train and
val (#8 R1). Phase 0c therefore groups folds by ``panel['timestamp']`` so an
entire cross-section lands wholly in train or wholly in val. ``oos_predict``
*asserts* this disjointness for the grouped splitter — the hard R1 regression
guard. ``make_splitter`` also exposes ``timeseries`` (honest, time-ordered) and
``naive_kfold`` (the leaky baseline) so the leakage smoke (#8 C9 pre-reg #4) is a
one-line swap: if the grouped/timeseries edge collapses vs naive_kfold, the edge
was leakage → REJECT.

This module is **additive**: it does not touch the LSTM ``model_manager`` path.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from features_intraday import FEATURES

# Tutorial cell 14 defaults: lgb.LGBMRegressor(n_jobs=-1, random_state=1).
_DEFAULT_PARAMS: dict = {"n_jobs": -1, "random_state": 1}


def _make_regressor(params: dict | None = None):
    import lightgbm as lgb

    merged = dict(_DEFAULT_PARAMS)
    if params:
        merged.update(params)
    return lgb.LGBMRegressor(**merged)


def train_models(panel: pd.DataFrame, *, params: dict | None = None):
    """Fit production models on the WHOLE panel (tutorial: full-data fit).

    Returns ``(model_buy, model_sell)`` fitted on ``panel[FEATURES]`` against
    ``y_buy`` / ``y_sell``. Not used for evaluation (that is ``oos_predict``);
    this is the model you would persist for live inference.
    """
    X = panel[FEATURES].to_numpy(dtype="float64")
    model_buy = _make_regressor(params).fit(X, panel["y_buy"].to_numpy(dtype="float64"))
    model_sell = _make_regressor(params).fit(X, panel["y_sell"].to_numpy(dtype="float64"))
    return model_buy, model_sell


def predict_with(models, panel: pd.DataFrame) -> pd.DataFrame:
    """Apply already-fitted ``(model_buy, model_sell)`` to ``panel``.

    Adds ``y_pred_buy`` / ``y_pred_sell`` to a copy. This is the 1-shot held-out
    path: fit once on the dev window with ``train_models``, then apply ONCE here
    to the frozen held-out window (pre-registration #5). Features are shared
    verbatim via ``FEATURES`` so train and inference cannot drift.
    """
    model_buy, model_sell = models
    X = panel[FEATURES].to_numpy(dtype="float64")
    out = panel.copy()
    out["y_pred_buy"] = model_buy.predict(X)
    out["y_pred_sell"] = model_sell.predict(X)
    return out


def make_splitter(kind: str = "grouped", *, n_splits: int = 5):
    """Return a ``(kind, sklearn-splitter)`` pair.

    - ``grouped``    : ``GroupKFold`` — folds split on the timestamp group (R1-safe).
    - ``timeseries`` : ``TimeSeriesSplit`` — honest expanding walk-forward.
    - ``naive_kfold``: ``KFold`` — the leaky baseline for the leakage smoke ONLY.
    """
    from sklearn.model_selection import GroupKFold, KFold, TimeSeriesSplit

    if kind == "grouped":
        return (kind, GroupKFold(n_splits=n_splits))
    if kind == "timeseries":
        return (kind, TimeSeriesSplit(n_splits=n_splits))
    if kind == "naive_kfold":
        return (kind, KFold(n_splits=n_splits, shuffle=True, random_state=1))
    raise ValueError(f"unknown splitter kind {kind!r}")


def _make_splits(kind, sk, panel: pd.DataFrame):
    """Materialise the (train_idx, val_idx) folds for a panel."""
    if kind == "grouped":
        # Group by timestamp so a whole cross-section stays on one side.
        groups = pd.factorize(panel["timestamp"], sort=True)[0]
        return list(sk.split(panel, groups=groups))
    return list(sk.split(panel))


def _assert_no_timestamp_leakage(panel: pd.DataFrame, splits) -> None:
    """Hard R1 guard: no timestamp may appear in both train and val of a fold."""
    ts = panel["timestamp"].to_numpy()
    for fold, (train_idx, val_idx) in enumerate(splits):
        train_ts = set(ts[train_idx].tolist())
        val_ts = set(ts[val_idx].tolist())
        overlap = train_ts & val_ts
        if overlap:
            raise AssertionError(
                f"R1 leakage in fold {fold}: {len(overlap)} timestamp(s) span "
                f"train and val (e.g. {sorted(overlap)[:3]})"
            )


def _cv_predict(X, y, splits, params: dict | None):
    """Tutorial ``my_cross_val_predict``: OOS preds, NaN where no fold covered."""
    y_pred = np.full(y.shape, np.nan, dtype="float64")
    for train_idx, val_idx in splits:
        est = _make_regressor(params)
        est.fit(X[train_idx], y[train_idx])
        y_pred[val_idx] = est.predict(X[val_idx])
    return y_pred


def oos_predict(
    panel: pd.DataFrame,
    *,
    splitter=None,
    params: dict | None = None,
) -> pd.DataFrame:
    """Add ``y_pred_buy`` / ``y_pred_sell`` OOS columns to a copy of ``panel``.

    ``splitter`` is a ``make_splitter(...)`` pair; default is the R1-safe
    ``grouped`` split. For the grouped split the timestamp-disjointness is
    asserted before any model is fit (fail fast on leakage). Rows no fold
    predicted are left NaN (TimeSeriesSplit's first block) — callers dropna,
    matching the tutorial.
    """
    if splitter is None:
        splitter = make_splitter("grouped")
    kind, sk = splitter
    splits = _make_splits(kind, sk, panel)
    if kind == "grouped":
        _assert_no_timestamp_leakage(panel, splits)

    X = panel[FEATURES].to_numpy(dtype="float64")
    out = panel.copy()
    out["y_pred_buy"] = _cv_predict(X, panel["y_buy"].to_numpy(dtype="float64"), splits, params)
    out["y_pred_sell"] = _cv_predict(X, panel["y_sell"].to_numpy(dtype="float64"), splits, params)
    return out
