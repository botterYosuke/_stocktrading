"""実録画日 (S:) でのライブ⇔バッチ等価性検証。

合成データテスト (test_runtime_live_features / test_runtime_virtual_execution) と
同じ突合を、実際の板 PUSH 録画に対して行う。決定グリッド・特徴量・仮想約定の
3 層すべてで live 実装がオフライン正本と一致することを確認する。
scores は境界位置のみに依存する決定的乱数 (モデル非依存 — 特徴量の丸め差で
発火が変わらないようにするため)。
"""
import numpy as np
import pytest

from scalp_agent import loader
from scalp_agent.config import ENTRY_MAX_LATENCY_S, FORCE_CLOSE_TOD
from scalp_agent.dataset import _materialize_side
from scalp_agent.execution import simulate_symbol_day
from scalp_agent.features import FEATURE_NAMES, build_features_normalized
from scalp_agent.labels import barrier_outcomes_grid
from scalp_agent.runtime.paper_engine import SymbolPaperEngine
from scalp_agent.runtime.virtual_execution import PaperTrade, Unresolved
from scalp_agent.sessions import decision_grid, exec_subset

pytestmark = pytest.mark.recorded_data

_HAS_DATA = loader.SNAPSHOT_DIR.exists() and "2026-07-13" in loader.available_days()
skip_no_data = pytest.mark.skipif(not _HAS_DATA, reason="録画 duckdb 不在 (S: 未マウント)")

DAY = "2026-07-13"
H, M, TAU = 5.0, 3.0, 0.40  # 較正セルの (h, m)。τ は取引数を稼ぐため低め


def _codes():
    codes = loader.list_codes(DAY)
    return codes[:2]


@skip_no_data
@pytest.mark.parametrize("code", _codes() if _HAS_DATA else ["-"])
def test_live_pipeline_matches_batch_on_recorded_day(code):
    snap = loader.load_symbol_day(DAY, code)
    ex = exec_subset(snap)
    ts = ex["ts"]
    didx, b_ts = decision_grid(ts)
    if len(b_ts) == 0:
        pytest.skip(f"{code}: 決定行なし")

    # ── バッチ正本 ──
    feats = build_features_normalized(ex)
    batch_x = np.column_stack([feats[k] for k in FEATURE_NAMES])[didx]
    bid, ask = ex["bid_px_1"], ex["ask_px_1"]
    out = barrier_outcomes_grid(
        ts, np.mod(ts, 86400.0), bid, ask, didx, b_ts,
        mults=(M,), horizons_s=(H,),
        entry_max_latency_s=ENTRY_MAX_LATENCY_S, force_close_tod=FORCE_CLOSE_TOD,
    )
    mid = (bid + ask) / 2.0
    lf = _materialize_side(out[(H, M)]["long"], ts, bid, ask, mid, 1)
    sf = _materialize_side(out[(H, M)]["short"], ts, bid, ask, mid, -1)
    rng = np.random.default_rng(20260713)
    scores = rng.dirichlet([0.5, 0.5, 0.5], size=len(b_ts))
    batch_trades = simulate_symbol_day(code, DAY, b_ts, scores, lf, sf, TAU)

    # ── ライブ (raw 行を流し、executable 判定もエンジン側) ──
    eng = SymbolPaperEngine(code, DAY, horizon_s=H, mult=M)
    b_index = {float(t): i for i, t in enumerate(b_ts)}
    keys = list(snap.keys())
    raw_ts = snap["ts"]
    events = [(float(raw_ts[i]), 0, i) for i in range(len(raw_ts))]
    for t in np.arange(np.ceil(raw_ts[0]), np.floor(raw_ts[-1]) + 2.0):
        events.append((float(t), 1, -1))
    events.sort(key=lambda e: (e[0], e[1]))
    live_trades, unresolved, live_x, live_b = [], [], [], []
    for t, kind, i in events:
        if kind == 0:
            row = {k: float(snap[k][i]) for k in keys}
            ev = eng.on_push(row, t % 86400.0)
            if isinstance(ev, PaperTrade):
                live_trades.append(ev)
        else:
            rec = eng.on_boundary(t)
            if rec is not None:
                assert t in b_index, f"live eligible boundary {t} not in decision_grid"
                live_b.append(t)
                live_x.append(list(rec.features))
                eng.apply_decision(rec, scores[b_index[t]], TAU)
    ev = eng.finalize()
    if isinstance(ev, Unresolved):
        unresolved.append(ev)

    # 1) 決定グリッド一致
    np.testing.assert_array_equal(np.asarray(live_b), b_ts)
    # 2) 特徴量一致 (窓和の丸め順序差のみ許容)
    np.testing.assert_allclose(np.asarray(live_x), batch_x,
                               rtol=1e-6, atol=1e-9, equal_nan=True)
    # 3) 取引一致 (日末 unresolved 以降はライブが因果的に busy — 突合対象外)
    cutoff = min((u.decision_ts for u in unresolved), default=np.inf)
    batch_cmp = [t for t in batch_trades if t.decision_ts < cutoff]
    live_cmp = [t for t in live_trades if t.decision_ts < cutoff]
    assert len(live_cmp) == len(batch_cmp)
    for lt, bt in zip(live_cmp, batch_cmp):
        assert (lt.side, lt.entry_ts, lt.exit_ts, lt.exit_reason) == \
               (bt.side, bt.entry_ts, bt.exit_ts, bt.exit_reason)
        np.testing.assert_allclose(lt.net_bps, bt.net_bps, rtol=1e-12)
