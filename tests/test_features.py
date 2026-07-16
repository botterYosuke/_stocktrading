import numpy as np

from scalp_agent import features as F


def test_mid_spread_microprice():
    b = np.array([99.0, 100.0])
    a = np.array([101.0, 102.0])
    bq = np.array([300.0, 100.0])
    aq = np.array([100.0, 300.0])
    np.testing.assert_allclose(F.mid(b, a), [100.0, 101.0])
    np.testing.assert_allclose(F.spread(b, a), [2.0, 2.0])
    # 買い板が厚い → microprice は ask 側に寄る
    mp = F.microprice(b, a, bq, aq)
    assert mp[0] > 100.0
    assert mp[1] < 101.0


def test_imbalance_bounds_and_zero_depth():
    oi = F.imbalance(np.array([300.0, 0.0]), np.array([100.0, 0.0]))
    np.testing.assert_allclose(oi, [0.5, 0.0])


def test_ofi_l1_directions():
    # t1: bid 価格上昇 (+qty), ask 同値で qty 増 (-Δ)
    b_px = np.array([100.0, 101.0])
    b_qty = np.array([200.0, 500.0])
    a_px = np.array([102.0, 102.0])
    a_qty = np.array([300.0, 400.0])
    ofi = F.ofi_l1(b_px, b_qty, a_px, a_qty)
    assert ofi[0] == 0.0
    # e_bid = +500 (価格上昇), e_ask = 400-300 = 100 → ofi = 400
    assert ofi[1] == 400.0


def test_trailing_return_and_sum_time_window():
    ts = np.array([0.0, 1.0, 2.0, 10.0])
    px = np.array([100.0, 101.0, 103.0, 104.0])
    ret = F.trailing_return(ts, px, 2.0)
    # t=2.0 の 2 秒前 = t=0.0 → 103-100
    assert ret[2] == 3.0
    # t=10.0 の 2 秒窓内に過去点なし → 直近点は t=8 以前で最も近い t=2 → 104-103
    assert ret[3] == 1.0
    s = F.trailing_sum(ts, np.ones(4), 1.5)
    np.testing.assert_allclose(s, [1.0, 2.0, 2.0, 1.0])


def test_build_features_shapes():
    n = 50
    rng = np.random.default_rng(0)
    ts = np.cumsum(rng.uniform(0.01, 0.5, n))
    mid = 1000 + np.cumsum(rng.normal(0, 0.5, n))
    snap = {"ts": ts, "last_px": mid.copy(), "volume": np.arange(n, dtype=float)}
    for i in range(1, 6):
        snap[f"bid_px_{i}"] = mid - 0.5 * i
        snap[f"ask_px_{i}"] = mid + 0.5 * i
        snap[f"bid_qty_{i}"] = rng.integers(100, 1000, n).astype(float)
        snap[f"ask_qty_{i}"] = rng.integers(100, 1000, n).astype(float)
    feats = F.build_features(snap)
    for name, arr in feats.items():
        assert arr.shape == (n,), name
        assert np.isfinite(arr).all(), name
