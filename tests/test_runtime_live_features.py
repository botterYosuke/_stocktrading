"""逐次特徴量エンジン (runtime.live_features) とバッチ正本の等価性固定。

- 行ローカル特徴量・trailing median: 完全一致 (同じ値・同じ NaN パターン)
- 窓集計 (ret/vol/ofi/mlofi): 総和の丸め順序差のみ許容 (rtol=1e-9)
"""
import numpy as np

from conftest import iter_rows, synth_exec_day
from scalp_agent.features import FEATURE_NAMES, build_features_normalized, trailing_median_1hz
from scalp_agent.runtime.live_features import CausalMedian1Hz, LiveFeatureEngine


def _live_matrix(snap):
    eng = LiveFeatureEngine()
    return np.asarray([eng.update(row) for row in iter_rows(snap)])


def _batch_matrix(snap):
    feats = build_features_normalized(snap)
    return np.column_stack([feats[k] for k in FEATURE_NAMES])


def _assert_equiv(snap):
    live = _live_matrix(snap)
    batch = _batch_matrix(snap)
    assert live.shape == batch.shape
    for j, name in enumerate(FEATURE_NAMES):
        np.testing.assert_allclose(
            live[:, j], batch[:, j], rtol=1e-9, atol=1e-12, equal_nan=True,
            err_msg=f"feature {name} mismatch",
        )


def test_live_features_match_batch_on_synthetic_day(synth_snap):
    _assert_equiv(synth_snap)


def test_live_features_match_batch_other_seeds():
    for seed in (11, 23):
        _assert_equiv(synth_exec_day(seed=seed))


def test_live_features_nan_qty_propagates_like_batch():
    snap = synth_exec_day(seed=5, afternoon=None)
    # L3 の数量を途中から欠損させる → mlofi 窓集計はバッチ同様 NaN に落ちて戻らない
    snap = {k: v.copy() for k, v in snap.items()}
    n = len(snap["ts"])
    snap["bid_qty_3"][n // 2] = np.nan
    _assert_equiv(snap)


def test_causal_median_matches_batch_exactly():
    rng = np.random.default_rng(3)
    snap = synth_exec_day(seed=3, afternoon=None)
    ts = snap["ts"]
    x = rng.integers(100, 50000, size=len(ts)).astype(np.float64)
    x[rng.random(len(ts)) < 0.05] = np.nan  # 欠損値の前方補完もバッチと同値
    batch = trailing_median_1hz(ts, x, 300.0, 60)
    med = CausalMedian1Hz(300.0, 60)
    live = np.asarray([med.update(float(t), float(v)) for t, v in zip(ts, x)])
    np.testing.assert_array_equal(np.isnan(live), np.isnan(batch))
    mask = ~np.isnan(batch)
    np.testing.assert_array_equal(live[mask], batch[mask])
