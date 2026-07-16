"""gen4 cheap gate モデル。線形 (ridge 閉形式) + LightGBM lambdarank。

どちらも教師は (day, tod) 内の横断百分位 (h{h}_pct)。
- linear: pct − 0.5 を回帰。nan 特徴は 0 (= 横断平均) に落とす。
- lgbm_rank: pct を五分位整数 0..4 に量子化し lambdarank。グループ = (day, tod)。
"""
from __future__ import annotations

import numpy as np

from scalp_agent_bars.xsec.config import (
    LGBM_RANK_PARAMS,
    LGBM_RANK_ROUNDS,
    RANK_LABEL_BINS,
    RIDGE_LAMBDA,
)


def _fill_nan(x: np.ndarray) -> np.ndarray:
    out = x.copy()
    out[~np.isfinite(out)] = 0.0
    return out


def train_linear(x: np.ndarray, pct: np.ndarray) -> np.ndarray:
    """ridge 閉形式。戻り値 = 係数 (bias なし: 特徴・教師とも中心化済み)。"""
    xf = _fill_nan(x)
    y = pct - 0.5
    lam = RIDGE_LAMBDA
    a = xf.T @ xf + lam * np.eye(xf.shape[1])
    b = xf.T @ y
    return np.linalg.solve(a, b)


def predict_linear(coef: np.ndarray, x: np.ndarray) -> np.ndarray:
    return _fill_nan(x) @ coef


def train_lgbm_rank(x: np.ndarray, pct: np.ndarray, group_sizes: np.ndarray):
    import lightgbm as lgb

    label = np.minimum((pct * RANK_LABEL_BINS).astype(np.int32), RANK_LABEL_BINS - 1)
    dset = lgb.Dataset(
        x, label=label, group=group_sizes.astype(np.int64),
        params={"max_bin": LGBM_RANK_PARAMS["max_bin"]},
    )
    return lgb.train(LGBM_RANK_PARAMS, dset, num_boost_round=LGBM_RANK_ROUNDS)


def predict_lgbm(booster, x: np.ndarray) -> np.ndarray:
    return booster.predict(x)
