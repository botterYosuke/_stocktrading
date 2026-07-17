"""gen7 thrust 検出器 / ユニバースのテスト。

検出器は issue#1 (`down_thrust_scalp_01._down_thrust_metrics`) からの凍結移植なので、
移植が忠実であること (境界・因果・保守側への倒し方) を固定する。
"""
from __future__ import annotations

import numpy as np

from scalp_agent_bars.thrust.detect import (
    OWNER_MAE_BPS,
    SIDE_DOWN,
    SIDE_UP,
    TP_BPS,
    first_touch_race,
    forward_paths,
    rolling_median,
    thrust_signals,
)
from scalp_agent_bars.thrust.universe import pit_flags, universe_masks


def _flat(n: int, px: float = 1000.0, vol: float = 100.0):
    return np.full(n, px), np.full(n, vol)


def test_rolling_median_window_and_nan_head():
    x = np.array([1.0, 2, 3, 4, 5])
    out = rolling_median(x, 3)
    assert np.isnan(out[0]) and np.isnan(out[1])
    assert out[2] == 2.0 and out[3] == 3.0 and out[4] == 4.0


def test_no_signal_on_flat_series():
    close, vol = _flat(60)
    sig, _, _ = thrust_signals(close, vol, SIDE_DOWN)
    assert not sig.any()


def test_down_thrust_fires_on_drop_with_volume():
    close, vol = _flat(60)
    i = 40
    close[i] = 1000.0 * (1 - 0.006)      # 3 本で -0.6% <= -0.5%
    vol[i] = 100.0 * 4.0                 # 4x >= 3x median
    sig, cum_ret, vm = thrust_signals(close, vol, SIDE_DOWN)
    assert sig[i]
    assert cum_ret[i] < -0.005
    assert vm[i] >= 3.0
    assert sig.sum() == 1


def test_down_thrust_requires_both_conditions():
    # 値幅だけ (出来高が伴わない) -> 発火しない
    close, vol = _flat(60)
    close[40] = 1000.0 * (1 - 0.006)
    sig, _, _ = thrust_signals(close, vol, SIDE_DOWN)
    assert not sig.any()

    # 出来高だけ (値幅が伴わない) -> 発火しない
    close, vol = _flat(60)
    vol[40] = 100.0 * 5.0
    sig, _, _ = thrust_signals(close, vol, SIDE_DOWN)
    assert not sig.any()


def test_ret_threshold_boundary_is_inclusive():
    # cum_ret == -0.005 ちょうどは発火する (<= 比較)
    close, vol = _flat(60)
    close[40] = 1000.0 * (1 - 0.005)
    vol[40] = 100.0 * 4.0
    sig, _, _ = thrust_signals(close, vol, SIDE_DOWN)
    assert sig[40]

    # -0.004 は発火しない
    close, vol = _flat(60)
    close[40] = 1000.0 * (1 - 0.004)
    vol[40] = 100.0 * 4.0
    sig, _, _ = thrust_signals(close, vol, SIDE_DOWN)
    assert not sig.any()


def test_vol_threshold_boundary_is_inclusive():
    close, vol = _flat(60)
    close[40] = 1000.0 * (1 - 0.006)
    vol[40] = 100.0 * 3.0            # ちょうど 3x
    sig, _, _ = thrust_signals(close, vol, SIDE_DOWN)
    assert sig[40]


def test_up_thrust_is_exact_mirror():
    close, vol = _flat(60)
    close[40] = 1000.0 * (1 + 0.006)
    vol[40] = 100.0 * 4.0
    up, _, _ = thrust_signals(close, vol, SIDE_UP)
    down, _, _ = thrust_signals(close, vol, SIDE_DOWN)
    assert up[40]
    assert not down.any()


def test_warmup_bars_never_fire():
    # vol_window=20 / lookback=3 を満たさない先頭は発火しない (因果)
    close = np.full(19, 1000.0)
    vol = np.full(19, 100.0)
    close[-1] = 900.0
    vol[-1] = 10_000.0
    sig, _, _ = thrust_signals(close, vol, SIDE_DOWN)
    assert not sig.any()


def test_forward_path_entry_is_next_bar_close():
    n = 30
    close = np.full(n, 1000.0)
    close[21] = 990.0            # entry bar
    high = close + 1.0
    low = close - 1.0
    out = forward_paths(close, high, low, np.array([20]), SIDE_DOWN, horizon=3)
    assert out["entry_px"][0] == 990.0


def test_forward_path_short_profits_when_price_falls():
    n = 30
    close = np.full(n, 1000.0)
    close[21] = 1000.0           # entry
    close[22:] = 990.0           # -1% => short +100bps
    high = close + 0.0
    low = close - 0.0
    out = forward_paths(close, high, low, np.array([20]), SIDE_DOWN, horizon=3)
    assert np.isclose(out["ret_bps"][0, 0], 100.0)
    assert np.isclose(out["mfe_bps"][0], 100.0)


def test_forward_path_truncates_at_series_end_no_overnight():
    n = 23
    close = np.full(n, 1000.0)
    high = close.copy()
    low = close.copy()
    out = forward_paths(close, high, low, np.array([20]), SIDE_DOWN, horizon=10)
    # entry=21, 測れるのは index 22 の 1 本だけ
    assert out["n_bars"][0] == 1


def test_mae_uses_wick_not_close():
    n = 30
    close = np.full(n, 1000.0)
    high = close.copy()
    high[22] = 1010.0            # 髭だけ逆行 (short に不利)
    low = close.copy()
    out = forward_paths(close, high, low, np.array([20]), SIDE_DOWN, horizon=3)
    assert np.isclose(out["mae_bps"][0], -100.0)     # 髭で -100bps
    # ret_bps は (horizon, n_events)。h=1 (= bar 22) は [0, 0]
    assert np.isclose(out["ret_bps"][0, 0], 0.0)     # close 基準では 0


def test_race_stop_wins_when_both_touch_same_bar():
    n = 30
    entry = 1000.0
    close = np.full(n, entry)
    high = close.copy()
    low = close.copy()
    # bar 22: close は TP 到達、high は stop 到達 -> stop 優先 (保守)
    close[22] = entry * (1 - TP_BPS / 1e4 - 0.001)
    high[22] = entry * (1 + OWNER_MAE_BPS / 1e4 + 0.001)
    out = first_touch_race(close, high, low, np.array([20]), SIDE_DOWN, horizon=5)
    assert out[0] == -1


def test_race_tp_first():
    n = 30
    entry = 1000.0
    close = np.full(n, entry)
    close[22] = entry * (1 - TP_BPS / 1e4 - 0.0001)
    high = close.copy()
    low = close.copy()
    out = first_touch_race(close, high, low, np.array([20]), SIDE_DOWN, horizon=5)
    assert out[0] == 1


def test_race_timeout_when_neither_touched():
    n = 30
    close = np.full(n, 1000.0)
    high = close.copy()
    low = close.copy()
    out = first_touch_race(close, high, low, np.array([20]), SIDE_DOWN, horizon=5)
    assert out[0] == 0


def test_pit_flags_are_causal_no_lookahead():
    # 最終日に巨大なスパイク & 高値。その日のフラグは立ってはいけない
    n = 100
    days = np.array([f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)])
    adj = np.full(n, 1000.0)
    close = np.full(n, 1000.0)
    turn = np.full(n, 2e9)
    adj[-1] = 5000.0
    turn[-1] = 1e12
    f = pit_flags(days, adj, close, turn)
    assert not f["new_high"][-1]
    assert not f["spike"][-1]


def test_new_high_is_strict_break_not_tie():
    # 横ばい系列では「最高値更新」は一度も起きない (>= ではなく > であること)
    n = 100
    days = np.array([f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)])
    flat = np.full(n, 1000.0)
    f = pit_flags(days, flat, flat, np.full(n, 2e9))
    assert not f["new_high"].any()


def test_pit_flags_fire_on_day_after_event():
    n = 100
    days = np.array([f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)])
    adj = np.full(n, 1000.0)
    close = np.full(n, 1000.0)
    turn = np.full(n, 2e9)
    adj[80] = 5000.0             # 80 日目に高値更新
    turn[80] = 1e11              # 同日に代金スパイク
    f = pit_flags(days, adj, close, turn)
    assert f["new_high"][81]
    assert f["spike"][81]


def test_hard_constraint_excludes_illiquid_and_low_price():
    n = 100
    days = np.array([f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)])
    adj = np.full(n, 1000.0)
    close = np.full(n, 1000.0)
    turn = np.full(n, 1e6)       # 100 万円/日 = 流動性不足
    adj[80] = 5000.0
    turn[80] = 1e11
    m = universe_masks(pit_flags(days, adj, close, turn))
    assert not m["U1_newhigh_and_spike"].any()

    # 低位株も落ちる
    close_low = np.full(n, 100.0)
    turn2 = np.full(n, 2e9)
    adj2 = np.full(n, 100.0)
    adj2[80] = 500.0
    turn2[80] = 1e11
    m2 = universe_masks(pit_flags(days, adj2, close_low, turn2))
    assert not m2["U3_newhigh_only"].any()


def test_universe_nesting_u1_subset_of_u2_and_u3():
    n = 120
    days = np.array([f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)])
    rng = np.random.default_rng(0)
    adj = 1000.0 + np.cumsum(rng.normal(0, 5, n))
    close = adj.copy()
    turn = np.abs(rng.normal(5e9, 2e9, n))
    m = universe_masks(pit_flags(days, adj, close, turn))
    u1, u2, u3 = (m["U1_newhigh_and_spike"], m["U2_spike_only"], m["U3_newhigh_only"])
    assert (u1 & ~u2).sum() == 0
    assert (u1 & ~u3).sum() == 0
