"""執行シミュレータ: PnL 恒等式 (gross − friction = net)・τ ゲート・重複禁止。"""
import numpy as np

from scalp_agent.execution import (
    SIDE_FIELDS,
    Trade,
    make_trade,
    max_concurrency,
    simulate_symbol_day,
    trade_pnl_bps,
)
from scalp_agent.labels import EXIT_NONE, EXIT_TP


def test_gross_minus_friction_equals_net_per_trade():
    rng = np.random.default_rng(7)
    for _ in range(200):
        mid_e = float(rng.uniform(100, 30000))
        s_e = float(rng.uniform(0.01, 0.02)) * mid_e
        mid_x = mid_e * float(rng.uniform(0.98, 1.02))
        s_x = float(rng.uniform(0.01, 0.02)) * mid_x
        for side in (1, -1):
            entry_px = mid_e + side * s_e / 2       # 対向 best を叩く
            exit_px = mid_x - side * s_x / 2
            gross, friction, net = trade_pnl_bps(side, entry_px, exit_px, mid_e, mid_x)
            assert abs((gross - friction) - net) < 1e-9
            assert friction > 0                     # taker は必ず摩擦を払う
            # net は実約定の直接計算とも一致
            assert abs(net - side * (exit_px - entry_px) / mid_e * 1e4) < 1e-9


def _fields(n, reason=EXIT_TP):
    f = {k: np.zeros(n) for k in SIDE_FIELDS}
    f["reason"] = np.full(n, reason, dtype=np.int8)
    return f


def test_simulator_tau_gate_argmax_gate_and_no_overlap():
    n = 4
    decision_ts = np.array([100.0, 101.0, 102.0, 110.0])
    scores = np.array([
        [0.1, 0.2, 0.7],   # UP, τ 以上 → エントリ
        [0.1, 0.1, 0.8],   # UP だが保有中 → スキップ
        [0.8, 0.1, 0.1],   # DOWN だが保有中 → スキップ
        [0.2, 0.5, 0.3],   # argmax=FLAT → スキップ (score は 0.5 でも)
    ])
    long_f = _fields(n)
    short_f = _fields(n)
    long_f["entry_ts"][:] = decision_ts + 0.5
    long_f["exit_ts"][:] = decision_ts + 5.0   # 最初の取引は ts=105 まで保有
    long_f["entry_px"][:] = 100.5
    long_f["exit_px"][:] = 101.5
    long_f["mid_entry"][:] = 100.25
    long_f["mid_exit"][:] = 101.75
    trades = simulate_symbol_day("TEST", "2026-07-13", decision_ts, scores,
                                 long_f, short_f, tau=0.6)
    assert len(trades) == 1
    assert trades[0].side == 1 and trades[0].decision_ts == 100.0
    # 恒等式は Trade 生成経路でも成立
    t = trades[0]
    assert abs((t.gross_bps - t.friction_bps) - t.net_bps) < 1e-9


def test_simulator_skips_unresolved_and_below_tau():
    decision_ts = np.array([100.0, 200.0])
    scores = np.array([[0.1, 0.2, 0.7], [0.05, 0.4, 0.55]])
    long_f = _fields(2)
    long_f["reason"][0] = EXIT_NONE          # 未解決 → スキップ
    trades = simulate_symbol_day("TEST", "d", decision_ts, scores,
                                 _fields(2, EXIT_NONE), _fields(2), tau=0.6)
    assert trades == []                       # 1本目 NONE、2本目 τ 未満


def test_reentry_allowed_after_exit():
    decision_ts = np.array([100.0, 106.0])
    scores = np.array([[0.1, 0.2, 0.7], [0.1, 0.2, 0.7]])
    long_f = _fields(2)
    long_f["entry_ts"][:] = decision_ts + 0.5
    long_f["exit_ts"][:] = decision_ts + 5.0  # 1本目は 105 で返済 → 106 は新規可
    long_f["mid_entry"][:] = 100.0
    long_f["mid_exit"][:] = 100.0
    long_f["entry_px"][:] = 100.1
    long_f["exit_px"][:] = 99.9
    trades = simulate_symbol_day("TEST", "d", decision_ts, scores,
                                 long_f, _fields(2), tau=0.6)
    assert len(trades) == 2


def test_max_concurrency_counts_overlapping_positions():
    def tr(e, x):
        return Trade("C", "d", 1, e, e, x, 1, 1, 1, 1, EXIT_TP, x, 0.0, 0.0, 0.0, 0.0)
    assert max_concurrency([tr(0, 10), tr(5, 15), tr(20, 30)]) == 2
    assert max_concurrency([]) == 0
