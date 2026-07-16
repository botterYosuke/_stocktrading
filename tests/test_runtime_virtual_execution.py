"""仮想約定エンジン (ライブ逐次) とバッチ正本 (barrier_outcomes_grid +
simulate_symbol_day) の等価性固定。

同一の合成 executable 列・同一 scores で両者を走らせ、取引列が一致することを
確認する。既知の構造的な差は 1 点のみ: バッチは日末 EXIT_NONE (未解決) の
決定を「取引なし・スロット解放」と遡及的に扱えるが、ライブは因果的に
ポジションを保有し続ける (unresolved)。よって比較は最初の unresolved の
decision_ts より前の区間で行う。
"""
import numpy as np
import pytest

from conftest import iter_rows, synth_exec_day
from scalp_agent.config import ENTRY_MAX_LATENCY_S, FORCE_CLOSE_TOD
from scalp_agent.dataset import _materialize_side
from scalp_agent.execution import simulate_symbol_day
from scalp_agent.labels import barrier_outcomes_grid
from scalp_agent.runtime.paper_engine import SymbolPaperEngine
from scalp_agent.runtime.virtual_execution import EntryCancelled, PaperTrade, Unresolved
from scalp_agent.sessions import decision_grid

DAY = "2026-07-16"


def _batch_trades(snap, scores, h, m, tau):
    ts = snap["ts"]
    bid, ask = snap["bid_px_1"], snap["ask_px_1"]
    didx, b_ts = decision_grid(ts)
    out = barrier_outcomes_grid(
        ts, np.mod(ts, 86400.0), bid, ask, didx, b_ts,
        mults=(m,), horizons_s=(h,),
        entry_max_latency_s=ENTRY_MAX_LATENCY_S, force_close_tod=FORCE_CLOSE_TOD,
    )
    cell = out[(h, m)]
    mid = (bid + ask) / 2.0
    lf = _materialize_side(cell["long"], ts, bid, ask, mid, 1)
    sf = _materialize_side(cell["short"], ts, bid, ask, mid, -1)
    return simulate_symbol_day("TEST", DAY, b_ts, scores, lf, sf, tau), b_ts


def _live_run(snap, scores, b_ts, h, m, tau):
    eng = SymbolPaperEngine("TEST", DAY, horizon_s=h, mult=m)
    b_index = {float(t): i for i, t in enumerate(b_ts)}
    ts = snap["ts"]
    events = [(float(t), 0, row) for t, row in zip(ts, iter_rows(snap))]
    for t in np.arange(np.ceil(ts[0]), np.floor(ts[-1]) + 2.0):
        events.append((float(t), 1, None))
    events.sort(key=lambda e: (e[0], e[1]))
    trades, cancels, unresolved = [], [], []
    for t, kind, row in events:
        if kind == 0:
            ev = eng.on_push(row, t % 86400.0)
            if isinstance(ev, PaperTrade):
                trades.append(ev)
            elif isinstance(ev, EntryCancelled):
                cancels.append(ev)
        else:
            rec = eng.on_boundary(t)
            if rec is not None:
                assert t in b_index, f"live eligible boundary {t} not in decision_grid"
                eng.apply_decision(rec, scores[b_index[t]], tau)
    ev = eng.finalize()
    if isinstance(ev, Unresolved):
        unresolved.append(ev)
    return trades, cancels, unresolved


FIELDS_EXACT = ("side", "decision_ts", "entry_ts", "exit_ts", "entry_px", "exit_px",
                "mid_entry", "mid_exit", "exit_reason", "exit_trigger_ts")
FIELDS_CLOSE = ("mae_bps", "gross_bps", "friction_bps", "net_bps")


@pytest.mark.parametrize("seed,h,m,tau", [
    (17, 5.0, 3.0, 0.70),
    (17, 5.0, 3.0, 0.40),
    (29, 60.0, 2.0, 0.40),
    (41, 15.0, 1.5, 0.50),
])
def test_live_execution_matches_batch(seed, h, m, tau):
    snap = synth_exec_day(seed=seed)
    _, b_ts = decision_grid(snap["ts"])
    assert len(b_ts) > 50
    rng = np.random.default_rng(seed + 1)
    scores = rng.dirichlet([0.5, 0.5, 0.5], size=len(b_ts))
    batch, b_ts = _batch_trades(snap, scores, h, m, tau)
    live, cancels, unresolved = _live_run(snap, scores, b_ts, h, m, tau)

    cutoff = min((u.decision_ts for u in unresolved), default=np.inf)
    batch_cmp = [t for t in batch if t.decision_ts < cutoff]
    live_cmp = [t for t in live if t.decision_ts < cutoff]
    assert len(live_cmp) == len(batch_cmp)
    assert len(batch_cmp) > 0, "テストデータで取引が発生していない (カバレッジ不足)"
    for lt, bt in zip(live_cmp, batch_cmp):
        for f in FIELDS_EXACT:
            assert getattr(lt, f) == getattr(bt, f), (
                f"{f}: live={getattr(lt, f)} batch={getattr(bt, f)} "
                f"(decision_ts={bt.decision_ts})")
        for f in FIELDS_CLOSE:
            np.testing.assert_allclose(
                getattr(lt, f), getattr(bt, f), rtol=1e-12, atol=1e-12,
                err_msg=f"{f} (decision_ts={bt.decision_ts})")
    # 恒等式 gross - friction = net (execution.py と同じ契約)
    for t in live:
        np.testing.assert_allclose(t.gross_bps - t.friction_bps, t.net_bps,
                                   rtol=0, atol=1e-9)


def test_exit_reasons_covered():
    """合成日で TP/SL/timeout/EOD がすべて発生していること (テスト自体の健全性)。"""
    snap = synth_exec_day(seed=17)
    _, b_ts = decision_grid(snap["ts"])
    rng = np.random.default_rng(18)
    scores = rng.dirichlet([0.5, 0.5, 0.5], size=len(b_ts))
    all_reasons = set()
    for h, m, tau in [(5.0, 3.0, 0.40), (60.0, 2.0, 0.40), (300.0, 1.5, 0.40),
                      (300.0, 3.0, 0.40), (600.0, 4.0, 0.40)]:
        batch, bts = _batch_trades(snap, scores, h, m, tau)
        live, cancels, _ = _live_run(snap, scores, bts, h, m, tau)
        all_reasons |= {t.exit_reason for t in live}
    assert {1, 2, 3, 4} <= all_reasons, f"exit reasons covered: {all_reasons}"


def test_latency_cancel_and_slippage_fields():
    snap = synth_exec_day(seed=17)
    _, b_ts = decision_grid(snap["ts"])
    rng = np.random.default_rng(19)
    scores = rng.dirichlet([0.4, 0.2, 0.4], size=len(b_ts))  # 高頻度発火
    live, cancels, _ = _live_run(snap, scores, b_ts, 5.0, 3.0, 0.40)
    assert any(c.reason == "latency" for c in cancels), "大ギャップで latency cancel が出るはず"
    for t in live[:50]:
        # quoted-spread fill との乖離が記録されている
        assert np.isfinite(t.slippage_entry_bps)
        assert np.isfinite(t.slippage_exit_bps)
