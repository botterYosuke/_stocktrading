"""gen5 (同時刻 lag 横断ランキング) の pure 関数テスト。"""
from __future__ import annotations

import numpy as np
import pytest

from scalp_agent_bars.xsec import tod_lag as tl


# ---- pivot_series ----------------------------------------------------------------

def _tiny_rows():
    """2 銘柄 × 2 tod × 3 日の完全格子。値 = code*100 + tod番号*10 + day番号。"""
    codes, tods, days, vals = [], [], [], []
    for d_i, d in enumerate(["2024-04-01", "2024-04-02", "2024-04-03"]):
        for t_i, t in enumerate([34200.0, 50400.0]):
            for c_i, c in enumerate(["1301", "1305"]):
                codes.append(c)
                tods.append(t)
                days.append(d)
                vals.append(c_i * 100.0 + t_i * 10.0 + d_i)
    return (np.array(codes), np.array(tods), np.array(days),
            np.array(vals, dtype=np.float64))


def test_pivot_series_layout():
    code, tod, day, vals = _tiny_rows()
    mat, code_idx, tod_idx, day_idx, n_tod = tl.pivot_series(code, tod, day, vals)
    assert n_tod == 2
    assert mat.shape == (4, 3)   # 2 codes × 2 tods, 3 days
    # 行の値が (code, tod, day) の正しいセルへ入る
    for r in range(len(code)):
        assert mat[code_idx[r] * n_tod + tod_idx[r], day_idx[r]] == vals[r]
    # 欠測セルは NaN
    code2 = np.append(code, "1301")
    tod2 = np.append(tod, 34200.0)
    day2 = np.append(day, "2024-04-04")   # 新しい日は 1301|34200 しか観測が無い
    vals2 = np.append(vals, 999.0)
    mat2, *_ = tl.pivot_series(code2, tod2, day2, vals2)
    assert np.isnan(mat2).sum() == 3      # 4 日目の残り 3 系列


# ---- ewma_lag_signal ---------------------------------------------------------------

def test_ewma_is_causal():
    """sig[:, d] は day d 以降の値に依存しない (lag >= 1 のみ)。"""
    rng = np.random.default_rng(0)
    mat = rng.normal(size=(3, 60))
    base = tl.ewma_lag_signal(mat, 5.0, lag_max=10, min_valid=1)
    spiked = mat.copy()
    spiked[:, 30] += 100.0
    out = tl.ewma_lag_signal(spiked, 5.0, lag_max=10, min_valid=1)
    # day 30 までは不変、day 31 以降 (lag 圏内) は変わる
    np.testing.assert_allclose(out[:, : 31], base[:, : 31])
    assert not np.allclose(out[:, 31], base[:, 31])


def test_ewma_weights_half_life():
    """lag L の重みは 0.5^(L/hl)。2 観測での加重平均を手計算と照合。"""
    mat = np.full((1, 3), np.nan)
    mat[0, 0] = 10.0   # day2 から見て lag 2
    mat[0, 1] = 20.0   # day2 から見て lag 1
    sig = tl.ewma_lag_signal(mat, 5.0, lag_max=40, min_valid=1)
    w1, w2 = 0.5 ** (1 / 5.0), 0.5 ** (2 / 5.0)
    assert sig[0, 2] == pytest.approx((w1 * 20.0 + w2 * 10.0) / (w1 + w2))
    # day0 は lag が 1 本も無い → NaN、day1 は lag1 のみ
    assert np.isnan(sig[0, 0])
    assert sig[0, 1] == pytest.approx(10.0)


def test_ewma_min_valid_masks():
    mat = np.full((1, 50), np.nan)
    mat[0, :10] = 1.0   # 有効観測は 10 本のみ
    sig = tl.ewma_lag_signal(mat, 5.0, lag_max=40, min_valid=20)
    assert np.isnan(sig[0, 30])
    sig2 = tl.ewma_lag_signal(mat, 5.0, lag_max=40, min_valid=10)
    assert sig2[0, 30] == pytest.approx(1.0)


def test_ewma_lag_beyond_history():
    """行列より長い lag_max でも落ちない (lag >= n_days は無視)。"""
    mat = np.ones((2, 5))
    sig = tl.ewma_lag_signal(mat, 5.0, lag_max=40, min_valid=1)
    assert sig[0, 4] == pytest.approx(1.0)


# ---- permutation 対照 ---------------------------------------------------------------

def test_perm_scores_identity_and_rotation():
    code, tod, day, vals = _tiny_rows()
    mat, code_idx, tod_idx, day_idx, n_tod = tl.pivot_series(code, tod, day, vals)
    sig = tl.ewma_lag_signal(mat, 5.0, lag_max=2, min_valid=1)
    ident = tl.perm_scores(sig, code_idx, tod_idx, day_idx, n_tod,
                           tl.identity_perm(n_tod))
    # identity = 自分の系列の signal
    np.testing.assert_allclose(
        ident, sig[code_idx * n_tod + tod_idx, day_idx])
    # rotation shift=1 は「別 tod の lag 系列」= tod を入れ替えた gather
    rot = tl.perm_scores(sig, code_idx, tod_idx, day_idx, n_tod,
                         tl.rotation_perm(n_tod, 1))
    swapped = sig[code_idx * n_tod + ((tod_idx + 1) % n_tod), day_idx]
    np.testing.assert_allclose(rot, swapped, equal_nan=True)
    # 完全格子の day>=1 では identity と rotation が一致しない (値が tod 依存)
    fin = np.isfinite(ident) & np.isfinite(rot)
    assert fin.any()
    assert not np.allclose(ident[fin], rot[fin])


def test_all_tod_permutations_excludes_identity():
    perms = tl.all_tod_permutations(5)
    assert len(perms) == 119
    assert tuple(range(5)) not in perms
    perms3 = tl.all_tod_permutations(3)
    assert len(perms3) == 5


def test_config_hash_stable():
    assert tl.config_hash() == tl.config_hash()
    assert len(tl.config_hash()) == 64
