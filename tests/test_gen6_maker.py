"""gen6 maker fill シムのテスト。

owner 承認 4 修正の検証: penetration 境界 (touch ≠ fill)・約定価格 = 指値・
発注 bar 不算入・同一 bar exit 禁止・fill 時刻→exit 対応・resting window 取消。
"""
from __future__ import annotations

import numpy as np
import pytest

from scalp_agent_bars.xsec import maker as mk
from scalp_agent_bars.xsec.features import symbol_day_rows

TOD_0930 = 9 * 3600.0 + 30 * 60.0


def flat_bars(n: int = 120, px: float = 1000.0, start: float = 9 * 3600.0):
    """09:00 から 1 分刻み n 本、全 OHLC = px の bars dict。"""
    st = start + 60.0 * np.arange(n)
    ones = np.full(n, px)
    return {
        "start_tod": st,
        "open": ones.copy(), "high": ones.copy(),
        "low": ones.copy(), "close": ones.copy(),
        "value": np.full(n, 1e6),
    }


def row_for(bars, tod=TOD_0930):
    rows = mk.maker_day_rows(bars)
    assert rows is not None
    by_tod = {r["tod"]: r for r in rows}
    return by_tod[tod]


# ---- penetration 境界 (owner 修正 1: touch ≠ fill) --------------------------------

def test_touch_does_not_fill():
    bars = flat_bars()
    bars["low"][32] = 1000.0   # 指値 1000 (join) にタッチのみ
    r = row_for(bars)
    assert not r["mk_join5_L_fill"]


def test_one_tick_penetration_fills_at_limit():
    bars = flat_bars()
    bars["low"][32] = 999.0    # 1000 − 1 tick まで突き抜け
    r = row_for(bars)
    assert r["mk_join5_L_fill"]
    assert r["mk_join5_L_entry_px"] == 1000.0          # 約定価格 = 指値 (修正 2)
    assert r["mk_join5_L_fill_tod"] == bars["start_tod"][32]


def test_m1_depth_needs_deeper_penetration():
    bars = flat_bars()
    bars["low"][32] = 999.0    # join は fill、m1 (指値 999) は 998 が必要
    r = row_for(bars)
    assert r["mk_join5_L_fill"]
    assert not r["mk_m15_L_fill"]
    bars2 = flat_bars()
    bars2["low"][32] = 998.0
    r2 = row_for(bars2)
    assert r2["mk_m15_L_fill"]
    assert r2["mk_m15_L_entry_px"] == 999.0


def test_short_symmetry():
    bars = flat_bars()
    bars["high"][32] = 1001.0  # 売り指値 1000 + 1 tick 突き抜け
    r = row_for(bars)
    assert r["mk_join5_S_fill"]
    assert r["mk_join5_S_entry_px"] == 1000.0
    assert not r["mk_join5_L_fill"]


# ---- 発注 bar 不算入 (レイテンシ保守化) --------------------------------------------

def test_decision_bar_penetration_not_counted():
    bars = flat_bars()
    bars["low"][30] = 999.0    # 判断時刻直後 bar (発注 bar) のみ突き抜け
    r = row_for(bars)
    assert not r["mk_join5_L_fill"]
    assert not r["mk_join30_L_fill"]


# ---- resting window 取消 -----------------------------------------------------------

def test_window_cancel_boundary():
    bars = flat_bars()
    # 09:35 開始 bar (index 35) は tod+300 を跨ぐため win5 では不算入、win30 では fill
    bars["low"][35] = 999.0
    r = row_for(bars)
    assert not r["mk_join5_L_fill"]
    assert r["mk_join30_L_fill"]


def test_fill_within_window():
    bars = flat_bars()
    bars["low"][33] = 999.0    # 09:33 bar は丸ごと win5 (取消 09:35) 内
    r = row_for(bars)
    assert r["mk_join5_L_fill"]


# ---- exit (b): 同一 bar 禁止・発注 bar 不算入・taker fallback ----------------------

def test_exit_maker_not_in_fill_bar_or_next():
    bars = flat_bars()
    bars["low"][32] = 999.0            # entry fill at bar 32
    bars["high"][32] = 1001.0          # 同一 bar の反発 — 数えない (修正 2)
    bars["high"][33] = 1001.0          # exit 発注 bar (fill+1) — 数えない
    r = row_for(bars)
    assert r["mk_join5_L_fill"]
    assert not r["mk_join5_L_exit_maker"]  # taker fallback へ
    # fallback = horizon bar (10:00, index 60) open
    assert r["taker_exit_px"] == 1000.0
    assert r["mk_join5_L_gross_b_bps"] == pytest.approx(0.0)


def test_exit_maker_from_fill_plus_two():
    bars = flat_bars()
    bars["low"][32] = 999.0
    bars["close"][32] = 1000.0         # exit 参照 = fill bar close
    bars["high"][34] = 1001.0          # fill+2 で 1 tick 突き抜け → exit 成立
    r = row_for(bars)
    assert r["mk_join5_L_exit_maker"]
    assert r["mk_join5_L_gross_b_bps"] == pytest.approx(0.0)  # 1000 entry → 1000 exit


def test_exit_maker_price_uses_fill_bar_close_ref():
    bars = flat_bars()
    bars["low"][32] = 999.0
    bars["close"][32] = 1005.0         # exit 指値 (join) = 1005
    bars["high"][40] = 1006.0          # 1005 + 1 tick
    r = row_for(bars)
    assert r["mk_join5_L_exit_maker"]
    assert r["mk_join5_L_gross_b_bps"] == pytest.approx((1005.0 / 1000.0 - 1) * 1e4)


def test_late_fill_falls_back_to_taker():
    bars = flat_bars()
    bars["low"][58] = 999.0            # win30 終盤で fill → exit 発注+1 が horizon 到達
    bars["high"][59] = 1001.0          # 数えられない (exit live_from = 60 = horizon bar)
    r = row_for(bars)
    assert r["mk_join30_L_fill"]
    assert not r["mk_join30_L_exit_maker"]
    assert r["mk_join30_L_gross_b_bps"] == pytest.approx(0.0)  # fallback open 1000


def test_gross_a_uses_horizon_open_taker():
    bars = flat_bars()
    bars["low"][32] = 999.0
    bars["open"][60] = 1010.0          # 10:00 bar open
    r = row_for(bars)
    assert r["mk_join5_L_gross_a_bps"] == pytest.approx((1010.0 / 1000.0 - 1) * 1e4)
    # short は符号反転
    bars["high"][32] = 1001.0
    r2 = row_for(bars)
    assert r2["mk_join5_S_gross_a_bps"] == pytest.approx(-(1010.0 / 1000.0 - 1) * 1e4)


def test_unfilled_row_is_nan():
    bars = flat_bars()
    r = row_for(bars)
    assert not r["mk_join5_L_fill"]
    assert np.isnan(r["mk_join5_L_gross_a_bps"])
    assert np.isnan(r["mk_join5_L_gross_b_bps"])


# ---- 行規約が gen4 features と揃う (join キー) -------------------------------------

def test_rows_align_with_symbol_day_rows():
    bars = flat_bars(n=400)
    f_rows = symbol_day_rows(bars)
    m_rows = mk.maker_day_rows(bars)
    assert f_rows is not None and m_rows is not None
    assert [r["tod"] for r in f_rows] == [r["tod"] for r in m_rows]


# ---- friction 分解 -----------------------------------------------------------------

def test_friction_decomposition():
    # price 1000: spread model = max(1tick/1000*1e4, floor 2.0) = 10bps
    # taker 片側 = 10 * 1.25 / 2 = 6.25、flat = 1.0
    assert mk.taker_side_bps(1000.0) == pytest.approx(6.25)
    assert mk.friction_config_a(1000.0) == pytest.approx(7.25)
    fb = mk.friction_config_b(np.array([1000.0, 1000.0]), np.array([True, False]))
    assert fb[0] == pytest.approx(1.0)      # 両側 maker 成立 = flat のみ
    assert fb[1] == pytest.approx(7.25)     # taker fallback
    # stress 片側 = (4 tick + 10bps) / 2 = (40 + 10) / 2 = 25 (price 1000, tick 1 → 4tick=40bps)
    assert mk.taker_side_stress_bps(1000.0) == pytest.approx(25.0)


def test_config_hash_stable():
    h1 = mk.config_hash()
    assert isinstance(h1, str) and len(h1) == 64
