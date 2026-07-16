"""PaperTrader: 全銘柄の SymbolPaperEngine を束ね、境界ごとに一括推論する。

live (runner) と offline replay の両方から同じ経路で使う — オフライン/ライブで
同じコードを共有する repo 規律の適用点。scorer を差し替えられるのはテスト・
replay 検証のため (本番は LightGBM booster)。
"""
from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np

from scalp_agent.runtime.outputs import PaperOutputs
from scalp_agent.runtime.paper_engine import DecisionRecord, SymbolPaperEngine
from scalp_agent.runtime.virtual_execution import EntryCancelled, EntryFilled, PaperTrade

# scorer(feature_matrix (n,19)) -> scores (n,3)  [down, flat, up]
Scorer = Callable[[np.ndarray], np.ndarray]


def booster_scorer(booster) -> Scorer:
    def _score(x: np.ndarray) -> np.ndarray:
        return booster.predict(x)
    return _score


class PaperTrader:
    def __init__(self, day: str, codes: list[str], horizon_s: float, mult: float,
                 tau: float, scorer: Scorer, outputs: PaperOutputs | None):
        self.day = day
        self.tau = tau
        self.scorer = scorer
        self.outputs = outputs
        self.engines = {c: SymbolPaperEngine(c, day, horizon_s, mult) for c in codes}
        self.n_trades = 0
        self.n_open_positions = 0
        self.suspended = False  # 再接続〜register 完了まで新規推論・entry を止める

    def _engine(self, code: str) -> SymbolPaperEngine:
        eng = self.engines.get(code)
        if eng is None:  # universe 外の PUSH (自動登録など) — エンジンを遅延生成
            eng = SymbolPaperEngine(code, self.day,
                                    next(iter(self.engines.values())).vexec.horizon_s
                                    if self.engines else 5.0,
                                    next(iter(self.engines.values())).vexec.mult
                                    if self.engines else 3.0)
            self.engines[code] = eng
        return eng

    def on_push(self, code: str, row: dict[str, float]) -> None:
        tod = math.fmod(row["ts"], 86400.0)
        ev = self._engine(code).on_push(row, tod)
        if ev is None or self.outputs is None:
            return
        if isinstance(ev, PaperTrade):
            self.n_trades += 1
            self.outputs.add_trade(ev)
        elif isinstance(ev, EntryCancelled):
            self.outputs.add_cancel(ev)
        elif isinstance(ev, EntryFilled):
            self.outputs.add_entry(ev)

    def on_gap(self, cause: str = "gap") -> int:
        """場中の観測 gap: 全銘柄の in-flight を unresolved_gap 化する (冪等)。

        復帰後の板で fill/exit を捏造しない (DESIGN 2026-07-16)。
        戻り値は破棄した in-flight 数。
        """
        n = 0
        for eng in self.engines.values():
            ev = eng.vexec.abort_inflight(cause)
            if ev is None:
                continue
            n += 1
            if self.outputs is not None:
                if isinstance(ev, EntryCancelled):
                    self.outputs.add_cancel(ev)
                else:
                    self.outputs.add_unresolved(ev)
        self.n_open_positions = 0
        return n

    def on_boundary(self, t: float) -> None:
        """整数秒境界 t: eligible 銘柄をまとめて 1 回の predict で評価する。"""
        if self.suspended:
            return
        recs: list[DecisionRecord] = []
        engines: list[SymbolPaperEngine] = []
        for eng in self.engines.values():
            rec = eng.on_boundary(t)
            if rec is not None:
                recs.append(rec)
                engines.append(eng)
        if not recs:
            return
        x = np.asarray([r.features for r in recs], dtype=np.float64)
        scores = self.scorer(x)
        for i, (rec, eng) in enumerate(zip(recs, engines)):
            eng.apply_decision(rec, scores[i], self.tau)
            rec.features = []  # 書き出さないので解放 (録画から再計算可能)
        self.n_open_positions = sum(1 for e in self.engines.values() if e.vexec.busy)

    def drain_decisions(self) -> None:
        if self.outputs is None:
            return
        for eng in self.engines.values():
            done = eng.drain_completed()
            if done:
                self.outputs.add_decisions(done)

    def finalize(self) -> None:
        for eng in self.engines.values():
            ev = eng.finalize()
            if self.outputs is not None:
                done = eng.drain_completed()
                if done:
                    self.outputs.add_decisions(done)
                if isinstance(ev, EntryCancelled):
                    self.outputs.add_cancel(ev)
                elif ev is not None:
                    self.outputs.add_unresolved(ev)
