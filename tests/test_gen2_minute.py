"""足読み gen2 (stocks_minute) の回帰テスト。

- ATR / eligible_decisions (連続性・昼休み・窓)
- 保守的バー執行: TP/SL 解決・同一バー両接触=SL・ギャップ不利約定・timeout/EOD・
  データ終端 EXIT_NONE・friction 恒等式
- クロス特徴 (leave-self-out・as-of)・peer map
- P3 クロスセクション top-1・busy 規則
- 凍結格子と config hash
"""
import numpy as np

from scalp_agent.execution import trade_pnl_bps
from scalp_agent.labels import (
    EXIT_EOD,
    EXIT_NONE,
    EXIT_SL,
    EXIT_TIMEOUT,
    EXIT_TP,
    labels_from_outcomes,
)
from scalp_agent_bars.minute import config as mcfg
from scalp_agent_bars.minute.exec_bars import (
    atr,
    barrier_outcomes_bars,
    eligible_decisions,
)
from scalp_agent_bars.minute.features import (
    ALL_FEATURE_NAMES,
    compute_peer_map,
    day_cross_features,
    feature_schema_hash,
)
from scalp_agent_bars.minute.portfolio import simulate_cross_section

DAY = 1_800_000_000.0  # 任意の日基準 epoch (テスト用)


def _mk_bars(tods, o, h, l):
    tods = np.asarray(tods, dtype=np.float64)
    return {
        "ts": DAY + tods,
        "start_tod": tods,
        "open": np.asarray(o, dtype=np.float64),
        "high": np.asarray(h, dtype=np.float64),
        "low": np.asarray(l, dtype=np.float64),
    }


def _outcome(bars, didx, spread_bps=10.0, horizon_bars=2, atr_mult=2.0,
             atr_value=1.0, force_close=mcfg.FORCE_CLOSE_TOD):
    n = len(bars["ts"])
    return barrier_outcomes_bars(
        bars["ts"], bars["start_tod"], bars["open"], bars["high"], bars["low"],
        np.asarray(didx, dtype=np.int64), np.full(n, atr_value),
        spread_bps, horizon_bars, atr_mult, force_close,
    )


# ---- ATR ----------------------------------------------------------------------

def test_atr_true_range_and_warmup():
    high = np.array([101.0, 102.0, 103.0])
    low = np.array([99.0, 100.0, 101.0])
    close = np.array([100.0, 101.0, 102.0])
    a = atr(high, low, close, n=2)
    assert np.isnan(a[0])
    # TR = [2, 2, 2] (high-low が支配) → 2 本平均 = 2
    assert np.allclose(a[1:], 2.0)


# ---- eligible_decisions ---------------------------------------------------------

def test_eligible_decisions_windows_and_contiguity():
    tods = np.array([
        11 * 3600 + 28 * 60, 11 * 3600 + 29 * 60, 11 * 3600 + 30 * 60,  # 前場末
        12 * 3600 + 30 * 60, 12 * 3600 + 31 * 60, 12 * 3600 + 32 * 60,  # 後場頭
        14 * 3600 + 53 * 60, 14 * 3600 + 54 * 60, 14 * 3600 + 55 * 60,  # 引け際
    ], dtype=np.float64)
    n = len(tods)
    open_ = np.full(n, 100.0)
    atr_arr = np.full(n, 1.0)
    didx = eligible_decisions(
        tods, open_, atr_arr, mcfg.SESSION_MORNING, mcfg.SESSION_AFTERNOON,
        mcfg.ENTRY_END_TOD,
    )
    got = tods[didx + 1]  # エントリバー開始
    # 11:29 ○ / 11:30 × (前場外) / 12:30 × (単発寄りバー) / 12:31 ○ / 12:32 ○
    # 14:54 ○ / 14:55 × (エントリ窓外)
    want = [11 * 3600 + 29 * 60, 12 * 3600 + 31 * 60, 12 * 3600 + 32 * 60,
            14 * 3600 + 54 * 60]
    assert np.allclose(sorted(got), want)
    # ATR NaN の決定バーは除外
    atr_nan = atr_arr.copy()
    atr_nan[0] = np.nan
    didx2 = eligible_decisions(tods, open_, atr_nan, mcfg.SESSION_MORNING,
                               mcfg.SESSION_AFTERNOON, mcfg.ENTRY_END_TOD)
    assert (11 * 3600 + 28 * 60) not in tods[didx2]


# ---- 保守的バー執行 --------------------------------------------------------------

def _tods(n, start=9 * 3600 + 30 * 60):
    return start + 60.0 * np.arange(n)


def test_tp_clean_hit_and_friction_identity():
    # base=100, Δ=2 → TP 102 / SL 98。バー2 で TP のみ接触。
    bars = _mk_bars(_tods(4),
                    o=[100, 100, 100.5, 101],
                    h=[100.5, 101.0, 102.5, 101],
                    l=[99.8, 99.5, 100.0, 100.5])
    out = _outcome(bars, [0])
    lo = out["long"]
    assert lo["reason"][0] == EXIT_TP
    assert np.isclose(lo["mid_exit"][0], 102.0)          # TP はバリア価格ちょうど
    assert np.isclose(lo["entry_px"][0], 100.0 * (1 + 5e-4))
    assert np.isclose(lo["exit_px"][0], 102.0 * (1 - 5e-4))
    gross, fric, net = trade_pnl_bps(1, lo["entry_px"][0], lo["exit_px"][0],
                                     lo["mid_entry"][0], lo["mid_exit"][0])
    assert np.isclose(gross - fric, net)
    assert np.isclose(fric, 10.1, atol=0.2)              # ≒ spread 10bps
    assert np.isclose(lo["exit_ts"][0], bars["ts"][2] + 60.0)  # exit バー確定時刻


def test_both_touch_same_bar_is_sl():
    bars = _mk_bars(_tods(3),
                    o=[100, 100, 100],
                    h=[100.5, 100.5, 102.5],
                    l=[99.8, 99.8, 97.5])
    out = _outcome(bars, [0])
    assert out["long"]["reason"][0] == EXIT_SL
    assert np.isclose(out["long"]["mid_exit"][0], 98.0)  # SL バリア価格


def test_gap_through_sl_fills_at_open():
    bars = _mk_bars(_tods(3),
                    o=[100, 100, 97.0],                   # バー2 が SL(98) を下抜けて寄る
                    h=[100.5, 100.5, 97.5],
                    l=[99.8, 99.9, 96.5])
    out = _outcome(bars, [0])
    lo = out["long"]
    assert lo["reason"][0] == EXIT_SL
    assert np.isclose(lo["mid_exit"][0], 97.0)            # 不利側 = 始値


def test_timeout_exits_at_next_bar_open():
    bars = _mk_bars(_tods(5),
                    o=[100, 100, 100.2, 100.1, 100.3],
                    h=[100.4] * 5,
                    l=[99.7] * 5)
    out = _outcome(bars, [0], horizon_bars=2)
    lo = out["long"]
    assert lo["reason"][0] == EXIT_TIMEOUT
    # エントリバー J=1 を含め H=2 本保有 → 返済はバー J+H=3 の始値
    assert np.isclose(lo["mid_exit"][0], 100.1)
    # short 側も同じ time exit
    assert out["short"]["reason"][0] == EXIT_TIMEOUT


def test_eod_beats_timeout():
    start = mcfg.FORCE_CLOSE_TOD - 2 * 60.0               # 14:53
    bars = _mk_bars(_tods(4, start),
                    o=[100, 100, 100.1, 100.2],
                    h=[100.4] * 4,
                    l=[99.7] * 4)
    out = _outcome(bars, [0], horizon_bars=3)
    lo = out["long"]
    assert lo["reason"][0] == EXIT_EOD
    assert np.isclose(lo["mid_exit"][0], 100.1)           # 14:55 バーの始値


def test_data_end_is_exit_none():
    bars = _mk_bars(_tods(3),
                    o=[100, 100, 100.1],
                    h=[100.4] * 3,
                    l=[99.7] * 3)
    out = _outcome(bars, [0], horizon_bars=5)
    assert out["long"]["reason"][0] == EXIT_NONE
    y, yv = labels_from_outcomes(out["long"], out["short"])
    assert not yv[0]


def test_short_mirror_tp():
    bars = _mk_bars(_tods(4),
                    o=[100, 100, 99.5, 99],
                    h=[100.3, 100.5, 100.0, 99.5],
                    l=[99.7, 99.0, 97.5, 98.5])
    out = _outcome(bars, [0])
    sh = out["short"]
    assert sh["reason"][0] == EXIT_TP                     # TP = 98 に low が接触
    assert np.isclose(sh["mid_exit"][0], 98.0)
    assert np.isclose(sh["entry_px"][0], 100.0 * (1 - 5e-4))
    assert np.isclose(sh["exit_px"][0], 98.0 * (1 + 5e-4))
    y, yv = labels_from_outcomes(out["long"], out["short"])
    assert yv[0] and y[0] == -1


# ---- クロス特徴 ------------------------------------------------------------------

def test_day_cross_features_leave_self_out():
    minute = np.arange(3, dtype=np.int64)
    per_code = {
        "A": {"minute": minute, "close": np.array([100.0, 101.0, 102.01])},
        "B": {"minute": minute, "close": np.array([200.0, 201.0, 200.0])},
        "C": {"minute": minute, "close": np.array([50.0, 50.0, 50.5])},
    }
    out = day_cross_features(per_code, {"A": "B"}, min_names=2)
    # bar0 は全銘柄 ret NaN
    assert np.isnan(out["A"]["mkt_ret_1b_bps"][0])
    # bar1: A=100bps, B=50bps, C=0bps → A の市場 (B,C) = 25bps
    assert np.isclose(out["A"]["mkt_ret_1b_bps"][1], 25.0)
    assert np.isclose(out["A"]["rel_ret_1b_bps"][1], 75.0)
    # breadth (A 視点・B,C): B 正・C 非正 → 0.5
    assert np.isclose(out["A"]["mkt_breadth_1b"][1], 0.5)
    # peer = B
    assert np.isclose(out["A"]["peer_ret_1b_bps"][1], 50.0)
    # C 視点 bar2: A ≈ 100bps, B ≈ -49.75bps
    assert np.isclose(out["C"]["mkt_ret_1b_bps"][2],
                      ((102.01 / 101.0 - 1) * 1e4 + (200.0 / 201.0 - 1) * 1e4) / 2)


def test_compute_peer_map_prefers_max_abs_corr():
    rng = np.random.default_rng(3)
    days = [f"2024-{m:02d}-{d:02d}" for m in range(1, 4) for d in range(1, 29)]
    base = rng.normal(0, 0.01, len(days))
    noise = rng.normal(0, 0.01, len(days))
    def closes(rets):
        px, out = 100.0, {}
        for d, r in zip(days, rets):
            px *= 1 + r
            out[d] = px
        return out
    daily = {"A": closes(base), "B": closes(base * 0.9 + noise * 0.05),
             "C": closes(rng.normal(0, 0.01, len(days)))}
    peer = compute_peer_map(daily)
    assert peer["A"] == "B" and peer["B"] == "A"


# ---- P3 クロスセクション ----------------------------------------------------------

def test_cross_section_top1_and_busy():
    t0 = DAY + 10 * 3600
    def rows(score_up, reason=EXIT_TP):
        n = 2
        fields = {
            "reason": np.array([reason, reason], dtype=np.int8),
            "entry_ts": np.array([t0, t0 + 60]),
            "exit_ts": np.array([t0 + 300, t0 + 360]),
            "entry_px": np.array([100.0, 100.0]),
            "exit_px": np.array([101.0, 101.0]),
            "mid_entry": np.array([100.0, 100.0]),
            "mid_exit": np.array([101.0, 101.0]),
            "tp_trigger_ts": np.array([t0 + 240, t0 + 300]),
            "exit_trigger_ts": np.array([t0 + 240, t0 + 300]),
            "mae_bps": np.array([1.0, 1.0]),
        }
        scores = np.zeros((n, 3))
        scores[:, 2] = score_up
        scores[:, 1] = 1 - score_up
        return {"b_ts": np.array([t0, t0 + 60.0]), "scores": scores,
                "lf": fields, "sf": fields}
    by_code = {"AAA": rows(0.9), "BBB": rows(0.8)}
    trades = simulate_cross_section("2025-07-01", by_code, tau=0.5, top_k=1)
    # 境界1: AAA (0.9 > 0.8) のみ。境界2: AAA は busy → BBB。
    assert [(t.code, t.decision_ts) for t in trades] == [
        ("AAA", t0), ("BBB", t0 + 60.0)]
    # τ を上げると発火しない
    assert simulate_cross_section("2025-07-01", by_code, tau=0.95, top_k=1) == []


# ---- 凍結設定 ---------------------------------------------------------------------

def test_gen2_frozen_grid_and_hash():
    assert mcfg.HORIZON_BARS == (5, 15, 30)
    assert mcfg.ATR_MULTS == (1.0, 2.0)
    assert mcfg.TAUS == (0.45, 0.55, 0.65)
    assert len(mcfg.grid_cells_full()) == 18
    assert len(mcfg.UNIVERSE) == 17
    assert mcfg.TRAIN_RANGE[1] < mcfg.VAL_RANGE[0] < mcfg.VAL_RANGE[1] < mcfg.OOS_RANGE[0]
    assert len(ALL_FEATURE_NAMES) == 26
    assert mcfg.config_hash() == GEN2_CONFIG_HASH
    assert feature_schema_hash() == GEN2_FEATURE_HASH


GEN2_CONFIG_HASH = "7f05c68021ec5ad2cdcec4a1ac0f62623a3ebf21f6626710aa7b3beab44d291e"
GEN2_FEATURE_HASH = "f2fef1441627a9382cbf8f04f13f6a6c5da934f3c7974fcac63abfb97cfae5ec"
