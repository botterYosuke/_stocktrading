"""ペーパートレード出力: decisions/trades duckdb + trades jsonl + EOD サマリ。

すべての出力に calibration_only タグが付く (runtime/calibration.py 参照)。
本出力は fill 較正・選択バイアス診断・live/offline 突合のためのデータであり、
戦略判定 (ADR-0001 ゲート・台帳記録) に使ってはならない。
"""
from __future__ import annotations

import json
import statistics
import time
from datetime import datetime
from pathlib import Path

import duckdb
import pyarrow as pa

from scalp_agent.runtime.calibration import CALIBRATION_TAGS
from scalp_agent.runtime.paper_engine import DecisionRecord
from scalp_agent.runtime.virtual_execution import (
    EXIT_REASON_NAMES,
    EntryCancelled,
    EntryFilled,
    PaperTrade,
    Unresolved,
)

DECISIONS_SCHEMA = pa.schema([
    ("code", pa.string()),
    ("boundary_ts", pa.float64()),
    ("push_ts", pa.float64()),
    ("bid", pa.float64()),
    ("ask", pa.float64()),
    ("s_down", pa.float64()),
    ("s_flat", pa.float64()),
    ("s_up", pa.float64()),
    ("cls", pa.int32()),
    ("fired", pa.bool_()),
    ("side", pa.int32()),
    ("skip_reason", pa.string()),
    ("next_push_ts", pa.float64()),
    ("next_bid", pa.float64()),
    ("next_ask", pa.float64()),
])

_DECISIONS_SQL_COLS = {
    pa.string(): "VARCHAR", pa.float64(): "DOUBLE",
    pa.int32(): "INTEGER", pa.bool_(): "BOOLEAN",
}

TRADE_FIELDS = (
    "code", "day", "side", "decision_ts", "entry_ts", "exit_ts",
    "entry_px", "exit_px", "mid_entry", "mid_exit", "exit_reason",
    "exit_trigger_ts", "mae_bps", "gross_bps", "friction_bps", "net_bps",
    "entry_quote_px", "exit_quote_px", "slippage_entry_bps", "slippage_exit_bps",
)


def _decision_to_row(r: DecisionRecord) -> dict:
    s = r.scores or (None, None, None)
    return {
        "code": r.code, "boundary_ts": r.boundary_ts, "push_ts": r.push_ts,
        "bid": r.bid, "ask": r.ask,
        "s_down": s[0], "s_flat": s[1], "s_up": s[2],
        "cls": r.cls, "fired": r.fired, "side": r.side,
        "skip_reason": r.skip_reason,
        "next_push_ts": r.next_push_ts, "next_bid": r.next_bid, "next_ask": r.next_ask,
    }


def trade_to_dict(t: PaperTrade) -> dict:
    d = {f: getattr(t, f) for f in TRADE_FIELDS}
    d["exit_reason_name"] = EXIT_REASON_NAMES.get(t.exit_reason, str(t.exit_reason))
    d["decision_to_entry_ms"] = t.decision_to_entry_ms
    d["trigger_to_exit_ms"] = t.trigger_to_exit_ms
    return d


class PaperOutputs:
    """artifacts/runtime/<day>/ に paper.duckdb (decisions/trades) + jsonl + summary。"""

    def __init__(self, out_dir: str | Path, day: str, meta: dict):
        self.dir = Path(out_dir)
        self.day = day
        self.meta = {**CALIBRATION_TAGS, **meta}
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "paper.duckdb"
        self.trades_jsonl = self.dir / "trades.jsonl"
        self.summary_path = self.dir / "summary.json"
        self.con: duckdb.DuckDBPyConnection | None = None
        self._decision_buf: list[dict] = []
        self.n_decisions = 0
        self.trades: list[PaperTrade] = []
        self.cancels: list[EntryCancelled] = []
        self.unresolved: list[Unresolved] = []
        self.last_flush = time.monotonic()

    def open(self) -> None:
        self.con = duckdb.connect(str(self.db_path))
        self.con.execute("SET enable_progress_bar=false")
        cols = ", ".join(
            f"{f.name} {_DECISIONS_SQL_COLS[f.type]}" for f in DECISIONS_SCHEMA)
        self.con.execute(f"CREATE TABLE IF NOT EXISTS decisions ({cols})")
        (self.dir / "meta.json").write_text(
            json.dumps(self.meta, ensure_ascii=False, indent=1), encoding="utf-8")

    def add_decisions(self, recs: list[DecisionRecord]) -> None:
        self._decision_buf.extend(_decision_to_row(r) for r in recs)
        self.n_decisions += len(recs)

    def _append_jsonl(self, rec: dict) -> None:
        with open(self.trades_jsonl, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def add_trade(self, t: PaperTrade) -> None:
        self.trades.append(t)
        self._append_jsonl({"event": "exit", **trade_to_dict(t), **CALIBRATION_TAGS})

    def add_entry(self, ev: EntryFilled) -> None:
        """entry 約定の監査イベント (クラッシュ後の未終端 entry 検出に使う)。"""
        self._append_jsonl({
            "event": "entry", "code": ev.code, "side": ev.side,
            "decision_ts": ev.decision_ts, "entry_ts": ev.entry_ts,
            "entry_px": ev.entry_px, "entry_quote_px": ev.entry_quote_px,
            **CALIBRATION_TAGS})

    def add_audit(self, event: str, **fields) -> None:
        """disconnect/reconnect/register/gap などの接続監査ログ (audit.jsonl)。"""
        rec = {"event": event, "at": datetime.now().isoformat(), **fields}
        with open(self.dir / "audit.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def add_cancel(self, ev: EntryCancelled) -> None:
        self.cancels.append(ev)
        self._append_jsonl({"event": "entry_cancelled", "code": ev.code, "side": ev.side,
                            "decision_ts": ev.decision_ts, "reason": ev.reason,
                            **CALIBRATION_TAGS})

    def add_unresolved(self, ev: Unresolved) -> None:
        self.unresolved.append(ev)
        self._append_jsonl({"event": "unresolved", "code": ev.code, "side": ev.side,
                            "decision_ts": ev.decision_ts, "entry_ts": ev.entry_ts,
                            "entry_px": ev.entry_px, "phase": ev.phase,
                            "cause": ev.cause, **CALIBRATION_TAGS})

    def reconcile_previous_session(self) -> int:
        """同日 trades.jsonl の未終端 entry (exit/unresolved なし) を unresolved 化する。

        クラッシュ・強制終了で finalize が走らなかった前セッションの entry を
        次回起動で検出する (DESIGN 2026-07-16: 再起動を暗黙の正常決済として扱わない)。
        """
        if not self.trades_jsonl.exists():
            return 0
        open_entries: dict[tuple, dict] = {}
        with open(self.trades_jsonl, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = (rec.get("code"), rec.get("entry_ts"))
                if rec.get("event") == "entry":
                    open_entries[key] = rec
                elif rec.get("event") in ("exit", "unresolved"):
                    open_entries.pop(key, None)
        for rec in open_entries.values():
            self.add_unresolved(Unresolved(
                code=rec["code"], side=rec["side"], decision_ts=rec["decision_ts"],
                entry_ts=rec["entry_ts"], entry_px=rec["entry_px"],
                phase="holding", cause="crash_recovered"))
        return len(open_entries)

    def should_flush(self, now_mono: float) -> bool:
        return (len(self._decision_buf) >= 2000
                or (self._decision_buf and now_mono - self.last_flush >= 30.0))

    def flush(self) -> None:
        if not self._decision_buf or self.con is None:
            return
        tbl = pa.Table.from_pylist(self._decision_buf, schema=DECISIONS_SCHEMA)
        self.con.register("dec_buf", tbl)
        names = ", ".join(f.name for f in DECISIONS_SCHEMA)
        self.con.execute(f"INSERT INTO decisions SELECT {names} FROM dec_buf")
        self.con.unregister("dec_buf")
        self._decision_buf.clear()
        self.last_flush = time.monotonic()

    def write_summary(self, extra: dict) -> dict:
        def agg(vals):
            if not vals:
                return {"n": 0, "mean": None, "median": None}
            return {"n": len(vals),
                    "mean": round(statistics.mean(vals), 4),
                    "median": round(statistics.median(vals), 4)}

        by_reason: dict[str, int] = {}
        by_code: dict[str, int] = {}
        for t in self.trades:
            name = EXIT_REASON_NAMES.get(t.exit_reason, str(t.exit_reason))
            by_reason[name] = by_reason.get(name, 0) + 1
            by_code[t.code] = by_code.get(t.code, 0) + 1
        summary = {
            **self.meta,
            "note": "fill 較正の生データ集計。戦略成績としての解釈・判定利用は禁止",
            "day": self.day,
            "n_trades": len(self.trades),
            "n_entry_cancelled": len(self.cancels),
            "n_unresolved": len(self.unresolved),
            "n_unresolved_by_cause": {
                c: sum(1 for u in self.unresolved if u.cause == c)
                for c in sorted({u.cause for u in self.unresolved})},
            "n_decisions": self.n_decisions,
            "slippage_entry_bps": agg([t.slippage_entry_bps for t in self.trades]),
            "slippage_exit_bps": agg([t.slippage_exit_bps for t in self.trades]),
            "friction_bps": agg([t.friction_bps for t in self.trades]),
            "decision_to_entry_ms": agg([t.decision_to_entry_ms for t in self.trades]),
            "trigger_to_exit_ms": agg([t.trigger_to_exit_ms for t in self.trades]),
            "per_exit_reason": by_reason,
            "trades_per_code": by_code,
            **extra,
            "generated_at": datetime.now().isoformat(),
        }
        self.summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        return summary

    def close(self) -> None:
        self.flush()
        if self.con is not None:
            self.con.close()
            self.con = None
