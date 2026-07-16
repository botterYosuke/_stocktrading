"""gen4 (横断ランキング) の pure 関数テスト。"""
from __future__ import annotations

import numpy as np
import pytest

from scalp_agent_bars.xsec import features as F
from scalp_agent_bars.xsec import friction as fr
from scalp_agent_bars.xsec import universe as un


# ---- friction ------------------------------------------------------------------

def test_tick_ladder():
    assert fr.tick_size(2500.0) == 1
    assert fr.tick_size(3000.0) == 1
    assert fr.tick_size(3001.0) == 5
    assert fr.tick_size(25000.0) == 10
    assert fr.tick_size(65000.0) == 100


def test_friction_conservative_vs_floor():
    # 2500 円: 1 tick = 4bps > floor
    f = float(fr.friction_bps(2500.0))
    assert f == pytest.approx(4.0 * 1.25 + 1.0)
    # 高流動メガキャップ帯でも floor 2bps × safety + flat を下回らない
    f2 = float(fr.friction_bps(9000.0))
    assert f2 >= 2.0 * 1.25 + 1.0


def test_friction_stress_is_larger():
    p = np.array([500.0, 2500.0, 40000.0])
    assert (fr.friction_bps_stress(p) > fr.friction_bps(p)).all()


# ---- symbol_day_rows -------------------------------------------------------------

def _synth_bars(n=391, start=9 * 3600.0, drift_bps_per_min=1.0):
    """09:00 から n 本の連続分足。close は毎分 drift で上がる。"""
    st = start + 60.0 * np.arange(n)
    close = 1000.0 * (1 + drift_bps_per_min / 1e4) ** np.arange(n)
    op = np.roll(close, 1)
    op[0] = 1000.0
    return {
        "start_tod": st, "open": op, "high": close * 1.001,
        "low": op * 0.999, "close": close,
        "vol": np.full(n, 100.0), "value": np.full(n, 1e6),
    }


def test_symbol_day_rows_causal_entry_exit():
    bars = _synth_bars()
    rows = F.symbol_day_rows(bars)
    assert rows is not None
    tods = [r["tod"] for r in rows]
    assert tods == [t for t in (34200.0, 36000.0, 37800.0, 46800.0, 50400.0)]
    r930 = rows[0]
    # entry = 09:30 バーの open = 09:29 バーの close (合成データの規約)
    i = int((34200.0 - 32400.0) / 60)
    assert r930["entry_px"] == pytest.approx(bars["open"][i])
    # 特徴は 09:29 バーまで (確定済み) の close
    assert r930["last_close"] == pytest.approx(bars["close"][i - 1])
    # h15 exit = 09:45 バーの open
    j = i + 15
    assert r930["h15_exit_px"] == pytest.approx(bars["open"][j])
    assert r930["h15_exit_reason"] == F.EXIT_HORIZON


def test_symbol_day_rows_day_end_fallback():
    # 14:00 決定 + 60 分 horizon だが 14:30 でバーが尽きる日 → 最終バー close
    bars = _synth_bars(n=331)  # 09:00..14:30
    rows = F.symbol_day_rows(bars)
    r1400 = [r for r in rows if r["tod"] == 50400.0][0]
    assert r1400["h60_exit_reason"] == F.EXIT_DAY_END
    assert r1400["h60_exit_px"] == pytest.approx(bars["close"][-1])


def test_symbol_day_rows_skips_when_no_entry_bar():
    bars = _synth_bars(n=25)  # 09:24 まで → 09:30 のエントリーバーが無い
    rows = F.symbol_day_rows(bars)
    assert rows is None


# ---- z-score / labels -------------------------------------------------------------

def test_zscore_by_group_basic():
    g = np.array(["a"] * 12 + ["b"] * 12)
    v = np.concatenate([np.arange(12.0), np.arange(12.0) * 10])
    z = F.zscore_by_group(v, g)
    assert np.nanmax(np.abs(z[:12] - z[12:])) < 1e-9  # 線形変換不変
    assert abs(np.nanmean(z[:12])) < 1e-9


def test_zscore_small_group_nan():
    g = np.array(["a"] * 5)
    z = F.zscore_by_group(np.arange(5.0), g)
    assert np.isnan(z).all()


def test_adjust_and_rank_labels_sector_demean():
    n = 40
    g = np.array(["d"] * n)
    sec = np.array(["A"] * 20 + ["B"] * 20)
    raw = np.concatenate([np.full(20, 100.0), np.full(20, -100.0)])
    raw[0] = 110.0   # A 内で +10
    raw[20] = -90.0  # B 内で +10
    adj, pct = F.adjust_and_rank_labels(raw, g, sec)
    # 業種控除後は「業種内での超過」だけが残る
    assert adj[0] == pytest.approx(10.0 * 19 / 20)
    assert adj[20] == pytest.approx(10.0 * 19 / 20)
    assert min(pct[0], pct[20]) >= 38.0 / 39.0 - 1e-9  # 上位 2 位はこの 2 行
    assert np.nanmax(pct) == pytest.approx(1.0)
    assert np.nanmin(pct) == pytest.approx(0.0)


def test_adjust_labels_small_sector_falls_back_to_market():
    n = 30
    g = np.array(["d"] * n)
    sec = np.array(["A"] * 27 + ["B"] * 3)  # B は SECTOR_MIN_MEMBERS 未満
    raw = np.arange(n, dtype=np.float64)
    adj, _ = F.adjust_and_rank_labels(raw, g, sec)
    mkt = raw.mean()
    np.testing.assert_allclose(adj[27:], raw[27:] - mkt)


# ---- universe ---------------------------------------------------------------------

def test_months_between():
    assert un.months_between("2024-11-04", "2025-02-18") == [
        "2024-11", "2024-12", "2025-01", "2025-02"]


def test_build_monthly_universe_pit():
    """月 m の選定に m 月のデータが使われない (lookahead なし)。"""
    rng = np.random.default_rng(0)
    # 実在日である必要はない (pure 関数は文字列順のみ使う) — 窓 60 営業日を満たす数
    days = [f"2024-01-{d:02d}" for d in range(1, 36)] + \
           [f"2024-02-{d:02d}" for d in range(1, 36)] + \
           [f"2024-03-{d:02d}" for d in range(1, 36)]
    codes, day_col, close, turn = [], [], [], []
    for c, base_turn in (("AAAA", 1e9), ("BBBB", 5e8), ("CCCC", 1e8)):
        for d in days:
            codes.append(c)
            day_col.append(d)
            close.append(1000.0)
            # CCCC は 3 月だけ突然高流動 — 3 月の選定には効かないはず
            t = base_turn
            if c == "CCCC" and d >= "2024-03":
                t = 1e10
            turn.append(t + rng.normal(0, 1e5))
    panel = {
        "code": np.array(codes), "day": np.array(day_col),
        "close": np.array(close), "turnover": np.array(turn),
    }
    uni = un.build_monthly_universe(
        panel, ["2024-03"], prime={"AAAA", "BBBB", "CCCC"},
        has_minute={"AAAA", "BBBB", "CCCC"}, size=2,
    )
    assert uni["2024-03"] == ["AAAA", "BBBB"]  # CCCC の 3 月の急増は反映されない
