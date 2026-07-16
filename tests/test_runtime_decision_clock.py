"""ライブ 1Hz 境界評価 (SymbolPaperEngine.on_boundary) と sessions.decision_grid の等価性。

- 境界 t は timestamp <= t の最後の PUSH・新着なしは棄却 (dedupe)
- dedupe は窓フィルタより先 (前場末尾 PUSH を 12:30 境界で stale 評価しない)
- エントリ窓 [09:00,11:30) ∪ [12:30,14:55) 内の境界だけ eligible
"""
import numpy as np

from conftest import DAY_BASE, iter_rows, synth_exec_day
from scalp_agent.runtime.paper_engine import SymbolPaperEngine
from scalp_agent.sessions import decision_grid


def _eligible_boundaries(snap) -> tuple[list[float], list[float]]:
    """ライブエンジンに PUSH + 全整数秒境界を流し、eligible (境界, 決定 PUSH ts) を返す。"""
    eng = SymbolPaperEngine("TEST", "2026-07-16", horizon_s=5.0, mult=3.0)
    ts = snap["ts"]
    events = [(float(t), 0, row) for t, row in zip(ts, iter_rows(snap))]
    for t in np.arange(np.ceil(ts[0]), np.floor(ts[-1]) + 2.0):
        events.append((float(t), 1, None))
    events.sort(key=lambda e: (e[0], e[1]))
    out_b, out_push = [], []
    for t, kind, row in events:
        if kind == 0:
            eng.on_push(row, t % 86400.0)
        else:
            rec = eng.on_boundary(t)
            if rec is not None:
                out_b.append(t)
                out_push.append(rec.push_ts)
    return out_b, out_push


def test_eligible_boundaries_match_decision_grid():
    # 午前 + 14:55 跨ぎ午後 (エントリ窓カットと EOD 側の挙動を両方含む)
    snap = synth_exec_day(seed=13)
    didx, b_ts = decision_grid(snap["ts"])
    live_b, live_push = _eligible_boundaries(snap)
    np.testing.assert_array_equal(np.asarray(live_b), b_ts)
    np.testing.assert_array_equal(np.asarray(live_push), snap["ts"][didx])


def test_morning_close_boundary_does_not_leak_to_afternoon():
    # 11:30 直前の PUSH は 11:30 境界 (窓外) で dedupe され、12:30 に再評価されない
    ts = np.asarray([DAY_BASE + 11 * 3600 + 29 * 60 + 59.5])
    snap = {
        "ts": ts,
        "bid_px_1": np.asarray([100.0]), "ask_px_1": np.asarray([100.5]),
        "last_px": np.asarray([100.0]), "volume": np.asarray([1.0]),
    }
    for i in range(2, 6):
        snap[f"bid_px_{i}"] = snap["bid_px_1"] - (i - 1) * 0.5
        snap[f"ask_px_{i}"] = snap["ask_px_1"] + (i - 1) * 0.5
    for i in range(1, 6):
        snap[f"bid_qty_{i}"] = np.asarray([100.0])
        snap[f"ask_qty_{i}"] = np.asarray([100.0])
    eng = SymbolPaperEngine("TEST", "2026-07-16", horizon_s=5.0, mult=3.0)
    row = {k: float(v[0]) for k, v in snap.items()}
    eng.on_push(row, row["ts"] % 86400.0)
    assert eng.on_boundary(DAY_BASE + 11 * 3600 + 30 * 60) is None  # 窓外だが dedupe 消費
    assert eng.on_boundary(DAY_BASE + 12 * 3600 + 30 * 60) is None  # 再評価されない
    didx, b_ts = decision_grid(snap["ts"])
    assert len(b_ts) == 0  # バッチも同じ結論
