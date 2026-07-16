import numpy as np

from scalp_agent.labels import LabelSpec, forward_mid, make_labels


def test_forward_mid_uses_first_snapshot_after_horizon():
    ts = np.array([0.0, 1.0, 5.0, 20.0])
    mid = np.array([100.0, 101.0, 105.0, 120.0])
    fwd, valid = forward_mid(ts, mid, 4.0)
    # t=0 → t>=4 の最初 = t=5 → 105
    assert fwd[0] == 105.0
    # t=5 → t>=9 の最初 = t=20 → 120
    assert fwd[2] == 120.0
    # 末尾はホライズン内に次データ無し → 無効
    assert not valid[3]
    assert valid[:3].all()


def test_make_labels_threshold_in_spread_units():
    ts = np.array([0.0, 1.0, 2.0, 3.0])
    mid = np.array([100.0, 100.0, 103.0, 96.0])
    spread = np.full(4, 2.0)
    spec = LabelSpec(horizon_s=0.5, threshold_spread_mult=1.5)  # 閾値 = 3.0
    y, valid = make_labels(ts, mid, spread, spec)
    # t=0 → fwd=mid[1]=100, move=0 → 0
    assert y[0] == 0
    # t=1 → fwd=103, move=+3 >= 3 → +1
    assert y[1] == 1
    # t=2 → fwd=96, move=-7 → -1
    assert y[2] == -1
    assert not valid[3] and y[3] == 0
