"""正規化特徴量: trailing median の因果性・欠損規則・スキーマ整合。"""
import numpy as np

from scalp_agent.features import (
    FEATURE_NAMES,
    _safe_ratio,
    build_features_normalized,
    trailing_median_1hz,
)

H9 = 9 * 3600.0


def test_trailing_median_uses_only_completed_past_seconds():
    ts = np.array([0.5, 1.5, 2.5, 3.5])
    x = np.array([10.0, 20.0, 30.0, 40.0])
    med = trailing_median_1hz(ts, x, window_s=300.0, min_s=2)
    assert np.isnan(med[0])            # 過去秒なし
    assert np.isnan(med[1])            # 経過 1 秒 < min_s=2
    assert med[2] == 15.0              # median(10, 20) — 現在秒の 30 は見ない
    assert med[3] == 20.0              # median(10, 20, 30)


def test_trailing_median_same_second_last_value_wins():
    ts = np.array([0.2, 0.8, 1.5])
    x = np.array([5.0, 7.0, 9.0])
    med = trailing_median_1hz(ts, x, window_s=300.0, min_s=1)
    assert med[2] == 7.0               # 秒 0 の最終値 7 (9 は現在秒 → 不使用)


def test_safe_ratio_returns_nan_not_zero_on_bad_denominator():
    num = np.array([1.0, 1.0, 1.0])
    den = np.array([0.0, np.nan, 2.0])
    out = _safe_ratio(num, den)
    assert np.isnan(out[0]) and np.isnan(out[1]) and out[2] == 0.5


def test_build_features_normalized_shape_and_names():
    n = 50
    rng = np.random.default_rng(3)
    snap = {"ts": H9 + np.arange(n) * 1.0, "last_px": np.full(n, 100.2),
            "volume": np.zeros(n)}
    for i in range(1, 6):
        snap[f"bid_px_{i}"] = np.full(n, 100.0 - 0.1 * i) + rng.normal(0, 0.01, n)
        snap[f"ask_px_{i}"] = np.full(n, 100.5 + 0.1 * i) + rng.normal(0, 0.01, n)
        snap[f"bid_qty_{i}"] = rng.uniform(100, 1000, n)
        snap[f"ask_qty_{i}"] = rng.uniform(100, 1000, n)
    feats = build_features_normalized(snap)
    assert tuple(feats.keys()) == FEATURE_NAMES
    for v in feats.values():
        assert len(v) == n
    # 立ち上がり (median 窓 min_s 未満) の depth_ratio は NaN 埋め (0 埋め禁止)
    assert np.isnan(feats["depth_bid_ratio"][0])
