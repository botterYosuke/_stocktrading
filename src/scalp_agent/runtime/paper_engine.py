"""ペーパートレードエンジン: 1Hz 決定グリッド + 特徴量 + 仮想約定の銘柄別結線。

決定グリッドの因果規則は sessions.decision_grid と同値:
- 整数秒境界 t で timestamp <= t の最後の executable PUSH を使う
- 前回境界以降に新着 PUSH がなければ推論しない (dedupe は窓フィルタより先 —
  前場末尾の PUSH を 12:30 境界で stale 評価しない)
- エントリ窓 [09:00,11:30) ∪ [12:30,14:55) 内の境界のみ発火対象

全 eligible 決定行 (busy・閾値未満・flat を含む) を DecisionRecord として記録し、
次 PUSH 遷移 (next-PUSH best) を到着時に補完する — 選択バイアス診断用
(owner 要求 2026-07-16)。
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from scalp_agent.config import (
    ENTRY_END_TOD,
    SESSION_AFTERNOON,
    SESSION_MORNING,
)
from scalp_agent.execution import CLS_DOWN, CLS_UP
from scalp_agent.runtime.live_features import LiveFeatureEngine
from scalp_agent.runtime.virtual_execution import VirtualSymbolExecution

RING_MAXLEN = 64


def executable_row(tod: float, bid: float, ask: float) -> bool:
    """sessions.executable_mask のスカラー版。"""
    in_morning = SESSION_MORNING[0] <= tod < SESSION_MORNING[1]
    in_afternoon = SESSION_AFTERNOON[0] <= tod <= SESSION_AFTERNOON[1]
    return (ask > bid) and (in_morning or in_afternoon)


def in_entry_window(tod: float) -> bool:
    """sessions.decision_grid の窓フィルタと同値。"""
    return (SESSION_MORNING[0] <= tod < SESSION_MORNING[1]) or (
        SESSION_AFTERNOON[0] <= tod < ENTRY_END_TOD)


@dataclass
class DecisionRecord:
    """eligible 1Hz 決定行 1 件。発火有無に関わらず全行記録する。"""
    code: str
    boundary_ts: float
    push_ts: float
    bid: float
    ask: float
    scores: tuple[float, float, float] | None = None  # (down, flat, up)
    cls: int | None = None
    fired: bool = False
    side: int = 0
    skip_reason: str | None = None  # busy / flat / below_tau / no_model
    next_push_ts: float | None = None
    next_bid: float | None = None
    next_ask: float | None = None
    features: list[float] = field(default_factory=list, repr=False)


class SymbolPaperEngine:
    """1 銘柄の PUSH 取り込み・境界評価・仮想約定を束ねる。"""

    def __init__(self, code: str, day: str, horizon_s: float, mult: float):
        self.code = code
        self.features = LiveFeatureEngine()
        self.vexec = VirtualSymbolExecution(code, day, horizon_s, mult)
        self.ring: deque[tuple[int, float, float, float, list[float]]] = deque(maxlen=RING_MAXLEN)
        # (push_idx, ts, bid, ask, feature_vec)
        self._push_count = 0
        self._last_used_push_idx = -1
        self._awaiting_next: list[DecisionRecord] = []
        self.completed: list[DecisionRecord] = []  # next-PUSH 遷移確定済み・書き出し待ち

    def on_push(self, row: dict[str, float], tod: float):
        """executable 判定込みで 1 PUSH を処理。仮想約定イベント (or None) を返す。

        row は boards.board_to_push_row / loader 形式 (best 欠損は呼び出し側で除外済み)。
        """
        bid, ask = row["bid_px_1"], row["ask_px_1"]
        if not executable_row(tod, bid, ask):
            return None
        vec = self.features.update(row)
        self._push_count += 1
        ts = row["ts"]
        self.ring.append((self._push_count, ts, bid, ask, vec))
        # 選択バイアス診断: 直前境界レコードの next-PUSH 遷移を補完
        if self._awaiting_next:
            still = []
            for rec in self._awaiting_next:
                if ts > rec.boundary_ts:
                    rec.next_push_ts = ts
                    rec.next_bid = bid
                    rec.next_ask = ask
                    self.completed.append(rec)
                else:
                    still.append(rec)
            self._awaiting_next = still
        return self.vexec.on_push(ts, tod, bid, ask)

    def on_boundary(self, t: float) -> DecisionRecord | None:
        """整数秒境界 t の評価。eligible なら DecisionRecord (scores 未設定) を返す。"""
        chosen = None
        for item in reversed(self.ring):
            if item[1] <= t:
                chosen = item
                break
        if chosen is None:
            return None
        push_idx, ts, bid, ask, vec = chosen
        if push_idx == self._last_used_push_idx:
            return None  # 新着なし — 同一 PUSH の再評価禁止
        self._last_used_push_idx = push_idx  # dedupe は窓フィルタより先
        if not in_entry_window(math.fmod(t, 86400.0)):
            return None
        rec = DecisionRecord(code=self.code, boundary_ts=t, push_ts=ts,
                             bid=bid, ask=ask, features=vec)
        self._awaiting_next.append(rec)
        return rec

    def apply_decision(self, rec: DecisionRecord, scores, tau: float) -> None:
        """予測 scores (down, flat, up) を決定規則に適用し、rec を確定させる。

        規則は execution.simulate_symbol_day と同値: busy → argmax flat →
        score < τ の順で棄却。発火時は仮想約定へ PENDING_ENTRY を積む。
        """
        s0, s1, s2 = float(scores[0]), float(scores[1]), float(scores[2])
        rec.scores = (s0, s1, s2)
        cls = 0
        best = s0
        if s1 > best:
            cls, best = 1, s1
        if s2 > best:
            cls, best = 2, s2
        rec.cls = cls
        if self.vexec.busy:
            rec.skip_reason = "busy"
            return
        if cls not in (CLS_UP, CLS_DOWN):
            rec.skip_reason = "flat"
            return
        if best < tau:
            rec.skip_reason = "below_tau"
            return
        side = 1 if cls == CLS_UP else -1
        rec.fired = True
        rec.side = side
        self.vexec.on_decision(rec.boundary_ts, side, rec.bid, rec.ask)

    def drain_completed(self) -> list[DecisionRecord]:
        out = self.completed
        self.completed = []
        return out

    def finalize(self):
        """日末処理: next-PUSH 未確定の決定行を確定 (None のまま) し、
        仮想約定の未解決イベント (or None) を返す。"""
        self.completed.extend(self._awaiting_next)
        self._awaiting_next = []
        return self.vexec.finalize()
