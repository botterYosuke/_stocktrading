"""gen1b (板非依存・分足/日足の兄弟 family) の回帰テスト。

- minute_bars / daily_ohlc の正本定義
- 特徴量の因果性 (完了バーのみ・NaN 規律・前日日足の欠損挙動)
- 決定グリッド: エントリ窓・as-of / next-PUSH の lookahead 禁止
- 凍結格子 120 セル完全一致・config_hash 固定
"""
import numpy as np
import pytest

from conftest import DAY_BASE, synth_exec_day
from scalp_agent_bars import config as bcfg
from scalp_agent_bars.bars import daily_ohlc, minute_bars
from scalp_agent_bars.config import TAUS, grid_cells_full
from scalp_agent_bars.dataset import bar_decision_grid, build_table_from_exec
from scalp_agent_bars.features import (
    FEATURE_NAMES,
    build_bar_features,
    feature_schema_hash,
)
from scalp_agent.execution import SIDE_FIELDS
from scalp_agent.labels import EXIT_NONE


def _t(tod: float) -> float:
    return DAY_BASE + tod


# ---- minute_bars -------------------------------------------------------------

def test_minute_bars_ohlc_and_volume():
    ts = np.array([_t(9 * 3600 + s) for s in (1.0, 30.0, 59.9, 61.0, 200.0)])
    px = np.array([100.0, 102.0, 99.0, 101.0, 103.0])
    cum = np.array([500.0, 600.0, 900.0, 1000.0, 1200.0])
    bars = minute_bars(ts, px, cum)
    # 3 バー: [09:00,09:01), [09:01,09:02), [09:03,09:04)
    assert np.allclose(
        bars["end_ts"],
        [_t(9 * 3600 + 60), _t(9 * 3600 + 120), _t(9 * 3600 + 240)],
    )
    assert np.allclose(bars["open"], [100.0, 101.0, 103.0])
    assert np.allclose(bars["high"], [102.0, 101.0, 103.0])
    assert np.allclose(bars["low"], [99.0, 101.0, 103.0])
    assert np.allclose(bars["close"], [99.0, 101.0, 103.0])
    # 先頭バーは日初からの累積、以降は差分
    assert np.allclose(bars["vol"], [900.0, 100.0, 200.0])


def test_minute_bars_drops_invalid_last_px():
    ts = np.array([_t(9 * 3600 + s) for s in (1.0, 2.0, 3.0)])
    px = np.array([np.nan, 0.0, 100.0])
    cum = np.array([10.0, 20.0, 30.0])
    bars = minute_bars(ts, px, cum)
    assert len(bars["close"]) == 1
    assert bars["close"][0] == 100.0
    empty = minute_bars(ts, np.array([np.nan, 0.0, -1.0]), cum)
    assert len(empty["close"]) == 0


def test_daily_ohlc():
    ts = np.array([_t(9 * 3600), _t(10 * 3600), _t(15 * 3600)])
    px = np.array([100.0, 110.0, 105.0])
    d = daily_ohlc(ts, px)
    assert d == {"open": 100.0, "high": 110.0, "low": 100.0, "close": 105.0}
    assert daily_ohlc(ts, np.array([0.0, np.nan, -1.0])) is None


# ---- 特徴量 -------------------------------------------------------------------

def _simple_bars(n: int = 40) -> dict[str, np.ndarray]:
    close = 100.0 + np.arange(n) * 0.5
    return {
        "end_ts": _t(9 * 3600) + (np.arange(n) + 1) * 60.0,
        "open": close - 0.2,
        "high": close + 0.3,
        "low": close - 0.4,
        "close": close,
        "vol": np.full(n, 100.0),
    }


def test_bar_features_returns_and_causality():
    bars = _simple_bars()
    feats = build_bar_features(bars, None)
    assert tuple(feats.keys()) == FEATURE_NAMES
    close = bars["close"]
    # ret_1b は厳密に (close_i - close_{i-1}) / close_{i-1}
    expect = (close[5] - close[4]) / close[4] * 1e4
    assert np.isclose(feats["ret_1b_bps"][5], expect)
    # 窓が足りない先頭は NaN (0 に潰さない)
    assert np.isnan(feats["ret_15b_bps"][:15]).all()
    assert np.isfinite(feats["ret_15b_bps"][15])
    assert np.isnan(feats["range_15b_bps"][:14]).all()
    # closeloc は [0, 1]
    cl = feats["closeloc_5b"]
    ok = np.isfinite(cl)
    assert ok.any() and (cl[ok] >= 0).all() and (cl[ok] <= 1).all()
    # 出来高比: 過去 VOL_MED_MIN_BARS 本未満は NaN、以降は 1.0 (定数出来高)
    assert np.isnan(feats["volr_1b"][: bcfg.VOL_MED_MIN_BARS]).all()
    assert np.allclose(feats["volr_1b"][bcfg.VOL_MED_MIN_BARS:], 1.0)
    # 前日日足なし → 日足系は全行 NaN
    for k in ("gap_bps", "prev_ret_bps", "prev_range_bps"):
        assert np.isnan(feats[k]).all()


def test_bar_features_prev_day_context():
    bars = _simple_bars(10)
    prev = {"open": 98.0, "high": 103.0, "low": 97.0, "close": 99.0}
    feats = build_bar_features(bars, prev)
    sess_open = bars["open"][0]
    assert np.allclose(feats["gap_bps"], (sess_open - 99.0) / 99.0 * 1e4)
    assert np.allclose(feats["prev_ret_bps"], (99.0 - 98.0) / 98.0 * 1e4)
    assert np.allclose(feats["prev_range_bps"], (103.0 - 97.0) / 99.0 * 1e4)


def test_feature_names_use_no_board_columns():
    """特徴量名に板由来の概念 (spread/imbalance/depth/ofi/micro) が無いこと。"""
    banned = ("spread", "imb", "depth", "ofi", "micro", "bid", "ask")
    for name in FEATURE_NAMES:
        assert not any(b in name for b in banned), name


# ---- 決定グリッド・テーブル組立 -------------------------------------------------

def test_bar_decision_grid_entry_window():
    ex_ts = np.array([_t(s) for s in np.arange(9 * 3600, 15 * 3600, 30.0)])
    bar_ends = np.array([
        _t(9 * 3600 + 60),            # 09:01 → 採用
        _t(11 * 3600 + 30 * 60),      # 11:30 ちょうど → 前場外 (除外)
        _t(12 * 3600 + 30 * 60),      # 12:30 ちょうど → 後場 (採用)
        _t(14 * 3600 + 55 * 60),      # 14:55 ちょうど → 除外 (エントリ窓は 14:55 未満)
    ])
    didx, b_ts, bar_idx = bar_decision_grid(ex_ts, bar_ends)
    assert np.allclose(b_ts, [bar_ends[0], bar_ends[2]])
    assert np.array_equal(bar_idx, [0, 2])
    # as-of: timestamp <= t の最後の PUSH
    for d, t in zip(didx, b_ts):
        assert ex_ts[d] <= t
        assert d == len(ex_ts) - 1 or ex_ts[d + 1] > t


def test_build_table_no_lookahead_and_grid():
    ex = synth_exec_day(seed=11, morning=(9 * 3600 + 1800, 10 * 3600 + 1800))
    table = build_table_from_exec(ex, None)
    b_ts = table["b_ts"]
    assert len(b_ts) > 10
    # 決定境界は分境界で厳密に単調増加
    assert np.allclose(np.mod(b_ts, 60.0), 0.0)
    assert (np.diff(b_ts) > 0).all()
    # 120 セル分の列が完全に揃う
    cells = grid_cells_full()
    assert len(cells) == 120
    cks = {bcfg.cell_key(h, m) for h, m, _ in cells}
    assert len(cks) == 24
    for ck in cks:
        assert f"y_{ck}" in table and f"yv_{ck}" in table
        for sp in ("L", "S"):
            for f in SIDE_FIELDS:
                assert f"{ck}_{sp}_{f}" in table
    for name in FEATURE_NAMES:
        assert len(table[f"f_{name}"]) == len(b_ts)
    # lookahead 禁止: エントリ約定は決定境界より厳密に後、かつ latency ガード内
    for ck in sorted(cks)[:4]:
        for sp in ("L", "S"):
            reason = table[f"{ck}_{sp}_reason"]
            ets = table[f"{ck}_{sp}_entry_ts"]
            live = reason != EXIT_NONE
            if live.any():
                assert (ets[live] > b_ts[live]).all()
                assert (ets[live] - b_ts[live] <= bcfg.ENTRY_MAX_LATENCY_S + 1e-9).all()
            # exit はエントリより後
            xts = table[f"{ck}_{sp}_exit_ts"]
            if live.any():
                assert (xts[live] > ets[live]).all()


def test_features_are_as_of_bar_close():
    """決定行 d の特徴量は、その境界で確定したバー (end_ts == b_ts) のもの。"""
    ex = synth_exec_day(seed=13, morning=(9 * 3600 + 1800, 10 * 3600))
    bars = minute_bars(ex["ts"], ex["last_px"], ex["volume"])
    feats = build_bar_features(bars, None)
    table = build_table_from_exec(ex, None)
    didx, b_ts, bar_idx = bar_decision_grid(ex["ts"], bars["end_ts"])
    assert np.allclose(table["b_ts"], b_ts)
    assert np.allclose(bars["end_ts"][bar_idx], b_ts)
    got = table["f_ret_1b_bps"]
    want = feats["ret_1b_bps"][bar_idx]
    assert np.allclose(got, want, equal_nan=True)


# ---- 凍結設定 -----------------------------------------------------------------

def test_gen1b_frozen_grid_and_days():
    assert bcfg.HORIZONS_S == (60.0, 120.0, 180.0, 300.0, 600.0, 900.0)
    assert bcfg.MULTS == (1.5, 2.0, 2.5, 3.0)
    assert TAUS == (0.40, 0.50, 0.60, 0.70, 0.80)
    assert len(grid_cells_full()) == 120
    bcfg.assert_no_day_leakage()
    assert bcfg.PREV_DAY["2026-07-09"] is None
    assert bcfg.PREV_DAY["2026-07-13"] == "2026-07-09"
    with pytest.raises(KeyError):
        from scalp_agent_bars.dataset import prev_day_of

        prev_day_of("2026-07-10")  # 台帳外の日を暗黙に使わない


def test_gen1b_config_hash_frozen():
    """設定変更 = 新 family の意図的開始。ハッシュ固定で不意の変更を検出する。"""
    assert bcfg.config_hash() == bcfg.config_hash()
    assert feature_schema_hash() == feature_schema_hash()
    # 値の固定は初回 sweep 前に確定する (test_sweep_config と同じ流儀)
    assert bcfg.config_hash() == GEN1B_CONFIG_HASH
    assert feature_schema_hash() == GEN1B_FEATURE_HASH


GEN1B_CONFIG_HASH = "2d25ce07205dce80801198511ed3313b49f37960f8833358fd7c201bbfb6beb3"
GEN1B_FEATURE_HASH = "fec7cbbf79a36cc8d359ff47dd76409191cb1adf7d277755ee831441cc02a9b8"
