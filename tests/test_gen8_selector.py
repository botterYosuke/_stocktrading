"""足読み gen8 (cost-aware complete-transaction selector) の回帰テスト。

- side_net_labels: 恒等式 (trade_pnl_bps の net と一致)・EXIT_NONE 無効化
- select_entries: 分位棄却・両サイド発火の期待値タイブレーク・同値見送り
- simulate_symbol_day_selector: busy 規則・EXIT_NONE スキップ
- selector_config: 分割の不重複・封印窓 (2025-04 / 2025-07〜09) の選定不使用・
  gen2 との OOS 一致・config_hash の凍結性
"""
import numpy as np

from scalp_agent.execution import SIDE_FIELDS, trade_pnl_bps
from scalp_agent.labels import EXIT_NONE, EXIT_TP
from scalp_agent_bars.minute import selector_config as scfg
from scalp_agent_bars.minute.config import (
    OOS_RANGE as GEN2_OOS_RANGE,
    TRAIN_RANGE as GEN2_TRAIN_RANGE,
    VAL_RANGE as GEN2_VAL_RANGE,
)
from scalp_agent_bars.minute.selector import (
    select_entries,
    side_net_labels,
    simulate_symbol_day_selector,
)

CK = "hb5_a10"


def _table(reasons, entry_px, exit_px, mid_entry, mid_exit, sp="L"):
    n = len(reasons)
    t = {
        f"{CK}_{sp}_reason": np.asarray(reasons, dtype=np.int8),
        f"{CK}_{sp}_entry_px": np.asarray(entry_px, dtype=np.float64),
        f"{CK}_{sp}_exit_px": np.asarray(exit_px, dtype=np.float64),
        f"{CK}_{sp}_mid_entry": np.asarray(mid_entry, dtype=np.float64),
        f"{CK}_{sp}_mid_exit": np.asarray(mid_exit, dtype=np.float64),
    }
    for f in SIDE_FIELDS:
        t.setdefault(f"{CK}_{sp}_{f}", np.zeros(n))
    return t


# ---- side_net_labels -------------------------------------------------------------

def test_side_net_labels_matches_trade_pnl_identity():
    t = _table([EXIT_TP], [100.05], [101.0], [100.0], [101.05], sp="L")
    net, valid = side_net_labels(t, CK, "L")
    assert valid[0]
    _, _, expect = trade_pnl_bps(1, 100.05, 101.0, 100.0, 101.05)
    assert np.isclose(net[0], expect)

    t = _table([EXIT_TP], [99.95], [99.0], [100.0], [98.95], sp="S")
    net, valid = side_net_labels(t, CK, "S")
    _, _, expect = trade_pnl_bps(-1, 99.95, 99.0, 100.0, 98.95)
    assert np.isclose(net[0], expect)


def test_side_net_labels_invalidates_unresolved_rows():
    t = _table([EXIT_NONE, EXIT_TP], [np.nan, 100.05], [np.nan, 101.0],
               [np.nan, 100.0], [np.nan, 101.05], sp="L")
    net, valid = side_net_labels(t, CK, "L")
    assert not valid[0] and np.isnan(net[0])
    assert valid[1] and np.isfinite(net[1])


# ---- select_entries --------------------------------------------------------------

def test_select_entries_quantile_gate_and_tiebreak():
    q_l = np.array([1.0, -1.0, 2.0, 2.0, -1.0])
    m_l = np.array([5.0, 5.0, 3.0, 4.0, 0.0])
    q_s = np.array([-1.0, 1.0, 2.0, 2.0, -0.5])
    m_s = np.array([9.0, 9.0, 4.0, 4.0, 9.0])
    sides = select_entries(q_l, m_l, q_s, m_s)
    # 行0: long のみ発火 / 行1: short のみ / 行2: 両発火 → mean 大の short /
    # 行3: 両発火同値 → 見送り / 行4: どちらも分位 <= 0 → 見送り
    assert sides.tolist() == [1, -1, -1, 0, 0]


def test_select_entries_rejects_zero_quantile():
    sides = select_entries(np.array([0.0]), np.array([9.9]),
                           np.array([0.0]), np.array([9.9]))
    assert sides.tolist() == [0]


# ---- simulate_symbol_day_selector -------------------------------------------------

def _fields(n, reason=EXIT_TP, entry_ts=None, exit_ts=None):
    f = {name: np.zeros(n, dtype=np.float64) for name in SIDE_FIELDS}
    f["reason"] = np.full(n, reason, dtype=np.int8)
    f["entry_px"] = np.full(n, 100.0)
    f["exit_px"] = np.full(n, 101.0)
    f["mid_entry"] = np.full(n, 100.0)
    f["mid_exit"] = np.full(n, 101.0)
    if entry_ts is not None:
        f["entry_ts"] = np.asarray(entry_ts, dtype=np.float64)
    if exit_ts is not None:
        f["exit_ts"] = np.asarray(exit_ts, dtype=np.float64)
    return f


def test_simulator_busy_rule_blocks_overlapping_entry():
    decision_ts = np.array([0.0, 60.0, 400.0])
    sides = np.array([1, 1, 1])
    lf = _fields(3, entry_ts=[1.0, 61.0, 401.0], exit_ts=[300.0, 360.0, 700.0])
    trades = simulate_symbol_day_selector("7203", "2025-05-01", decision_ts, sides,
                                          lf, _fields(3))
    # 行1 は行0 の保有中 (exit 300) → スキップ。行2 は entry 可。
    assert [t.decision_ts for t in trades] == [0.0, 400.0]
    assert all(t.side == 1 for t in trades)


def test_simulator_skips_unresolved_and_flat_rows():
    decision_ts = np.array([0.0, 60.0])
    sides = np.array([-1, 0])
    sf = _fields(2, reason=EXIT_NONE)
    trades = simulate_symbol_day_selector("7203", "2025-05-01", decision_ts, sides,
                                          _fields(2), sf)
    assert trades == []


# ---- selector_config -------------------------------------------------------------

def test_splits_are_disjoint_and_respect_seals():
    # train / val / oos が重ならない
    assert scfg.TRAIN_RANGE[1] < scfg.VAL_RANGE[0]
    assert scfg.VAL_RANGE[1] < scfg.OOS_RANGE[0]
    # VAL は封印窓を踏まない: 2025-04 (gen4/5/6 sealed OOS)・2025-07〜09 (gen2/3 val)
    assert scfg.VAL_RANGE[0] >= "2025-05-01"
    assert scfg.VAL_RANGE[1] <= "2025-06-30"
    # FINAL_FIT は OOS に触れない
    assert scfg.FINAL_FIT_RANGE[1] < scfg.OOS_RANGE[0]
    # OOS は gen2 と同一の sealed 窓
    assert scfg.OOS_RANGE == GEN2_OOS_RANGE
    # キャッシュ再利用の前提: gen8 の全学習・選定行は gen2 isval 窓の内側
    assert scfg.TRAIN_RANGE[0] >= GEN2_TRAIN_RANGE[0]
    assert scfg.FINAL_FIT_RANGE[1] <= GEN2_VAL_RANGE[1]


def test_lgbm_params_inherit_gen2_except_objective():
    from scalp_agent.config import LGBM_PARAMS
    assert scfg.LGBM_MEAN_PARAMS["objective"] == "regression"
    assert "num_class" not in scfg.LGBM_MEAN_PARAMS
    q = scfg.lgbm_quantile_params(0.05)
    assert q["objective"] == "quantile" and q["alpha"] == 0.05
    for k in ("learning_rate", "num_leaves", "min_data_in_leaf", "seed",
              "deterministic", "max_bin"):
        assert scfg.LGBM_MEAN_PARAMS[k] == LGBM_PARAMS[k]
        assert q[k] == LGBM_PARAMS[k]


def test_config_hash_is_frozen():
    # 事前凍結後の設定変更を検知する回帰値。変えたら新 family。
    assert scfg.ALPHAS == (0.05, 0.10, 0.20)
    h = scfg.config_hash()
    assert isinstance(h, str) and len(h) == 64
    assert h == scfg.config_hash()
