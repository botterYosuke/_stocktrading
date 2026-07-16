"""場中観測 gap ポリシー (DESIGN 2026-07-16) の固定。

- 切断区間が取引時間と重なった in-flight は unresolved_gap 化し、
  復帰後の板で fill/exit を捏造しない
- 場外・昼休みのみの切断は gap 汚染に数えない
- クラッシュ後の次回起動で前セッションの未終端 entry を unresolved 化する
"""
import json

from conftest import DAY_BASE
from scalp_agent.runtime.outputs import PaperOutputs
from scalp_agent.runtime.stall import market_overlap_seconds
from scalp_agent.runtime.virtual_execution import (
    EntryCancelled,
    EntryFilled,
    Unresolved,
    VirtualSymbolExecution,
)


def _holding_engine():
    eng = VirtualSymbolExecution("8306", "2026-07-16", horizon_s=60.0, mult=3.0)
    b = DAY_BASE + 9 * 3600 + 1800
    eng.on_decision(b, 1, decision_bid=100.0, decision_ask=100.5)
    ev = eng.on_push(b + 0.5, 9 * 3600 + 1800.5, 100.0, 100.5)
    assert isinstance(ev, EntryFilled)
    return eng


def test_abort_inflight_holding_becomes_unresolved_gap():
    eng = _holding_engine()
    ev = eng.abort_inflight("gap")
    assert isinstance(ev, Unresolved) and ev.cause == "gap" and ev.phase == "holding"
    assert not eng.busy
    # 復帰後の板で fill を捏造しない: 以降の PUSH は何も返さない
    assert eng.on_push(DAY_BASE + 9 * 3600 + 4000, 9 * 3600 + 4000, 90.0, 90.5) is None
    # 冪等
    assert eng.abort_inflight("gap") is None


def test_abort_inflight_pending_entry_cancelled():
    eng = VirtualSymbolExecution("8306", "2026-07-16", horizon_s=60.0, mult=3.0)
    b = DAY_BASE + 9 * 3600 + 1800
    eng.on_decision(b, -1, decision_bid=100.0, decision_ask=100.5)
    ev = eng.abort_inflight("gap")
    assert isinstance(ev, EntryCancelled) and ev.reason == "gap"


def test_market_overlap_seconds():
    # 昼休みのみ (11:45→12:10) → 0
    assert market_overlap_seconds(DAY_BASE + 11.75 * 3600, DAY_BASE + 12 * 3600 + 600) == 0.0
    # 12:25→12:35 → 後場 5 分と重複
    assert market_overlap_seconds(DAY_BASE + 12 * 3600 + 1500,
                                  DAY_BASE + 12 * 3600 + 2100) == 300.0
    # 場外 (16:00→17:00) → 0
    assert market_overlap_seconds(DAY_BASE + 16 * 3600, DAY_BASE + 17 * 3600) == 0.0
    # 場中まるごと (10:00→10:01) → 60
    assert market_overlap_seconds(DAY_BASE + 10 * 3600, DAY_BASE + 10 * 3600 + 60) == 60.0


def test_reconcile_previous_session_marks_crashed_entries(tmp_path):
    out = PaperOutputs(tmp_path, "2026-07-16", {"test": True})
    lines = [
        {"event": "entry", "code": "8306", "side": 1, "decision_ts": 1.0,
         "entry_ts": 2.0, "entry_px": 100.5, "entry_quote_px": 100.5},
        {"event": "exit", "code": "8306", "entry_ts": 2.0},
        {"event": "entry", "code": "9984", "side": -1, "decision_ts": 3.0,
         "entry_ts": 4.0, "entry_px": 200.0, "entry_quote_px": 200.0},
    ]
    with open(out.trades_jsonl, "w", encoding="utf-8") as fh:
        for r in lines:
            fh.write(json.dumps(r) + "\n")
    n = out.reconcile_previous_session()
    assert n == 1
    assert len(out.unresolved) == 1
    u = out.unresolved[0]
    assert u.code == "9984" and u.cause == "crash_recovered"
    # 再実行しても二重計上しない (unresolved 行が entry を打ち消す)
    out2 = PaperOutputs(tmp_path, "2026-07-16", {"test": True})
    assert out2.reconcile_previous_session() == 0
