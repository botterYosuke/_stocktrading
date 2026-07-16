"""凍結格子・日付漏洩禁止・config hash の固定 (07-13 validation を開く前提条件)。"""
import itertools

import pytest

from scalp_agent.config import (
    HORIZONS_S,
    IS_TRAIN_DAYS,
    IS_VAL_DAYS,
    MULTS,
    OOS_DAYS,
    TAUS,
    assert_days_role,
    assert_no_day_leakage,
    config_hash,
    grid_cells_full,
)
from scalp_agent.features import FEATURE_NAMES, feature_schema_hash

# 2026-07-16 グリルで固定した値。変更 = 凍結後の設定改変であり、意図的な
# 新 family 開始のときだけ両ハッシュとこのテストを同時に更新する。
PINNED_CONFIG_HASH = "28eb2ba6cf0c22d718bba5e744bcc2d22c05e14e7447759650ce6b4f3d4db866"
PINNED_SCHEMA_HASH = "a05fcbf696024d42f1154845fae313890690eda8ac096de2170f77beb58f39de"


def test_grid_is_exactly_120_cells():
    cells = grid_cells_full()
    assert len(cells) == 120
    expected = set(itertools.product(
        (5.0, 15.0, 30.0, 60.0, 120.0, 300.0),
        (1.5, 2.0, 2.5, 3.0),
        (0.40, 0.50, 0.60, 0.70, 0.80),
    ))
    assert set(cells) == expected


def test_taus_are_five_discrete_points_not_a_range():
    assert TAUS == (0.40, 0.50, 0.60, 0.70, 0.80)


def test_mult_one_is_not_in_grid():
    assert 1.0 not in MULTS


def test_day_roles_are_disjoint():
    assert_no_day_leakage()
    assert set(IS_TRAIN_DAYS) == {"2026-07-09"}
    assert set(IS_VAL_DAYS) == {"2026-07-13"}
    assert set(OOS_DAYS) == {"2026-07-14"}


def test_role_assertion_rejects_oos_day_in_training():
    with pytest.raises(AssertionError):
        assert_days_role(["2026-07-14"], "train")
    with pytest.raises(AssertionError):
        assert_days_role(["2026-07-09", "2026-07-14"], "train+val")
    assert_days_role(["2026-07-09"], "train")
    assert_days_role(["2026-07-14"], "oos")


def test_lgbm_config_hash_is_pinned():
    assert config_hash() == PINNED_CONFIG_HASH


def test_feature_schema_hash_is_pinned():
    assert feature_schema_hash() == PINNED_SCHEMA_HASH
    assert len(FEATURE_NAMES) == 19
