"""録画 duckdb からの offline replay ドライバ。

ライブと同じ PaperTrader/SymbolPaperEngine を録画 PUSH 列で駆動する。
用途:
- ランタイム実装のオフライン検証 (バッチ正本パイプラインとの突合)
- 特徴量・約定規則の live/offline 等価性の回帰テスト

イベント順序はライブと同じ因果: 境界 t の処理は timestamp <= t の全 PUSH の後。
"""
from __future__ import annotations

import math

import numpy as np

from scalp_agent import loader
from scalp_agent.runtime.trader import PaperTrader

_ROW_KEYS = (
    ["ts", "last_px", "volume"]
    + [f"{s}_{k}_{i}" for s in ("bid", "ask") for k in ("px", "qty") for i in range(1, 6)]
)


def iter_symbol_rows(snap: dict[str, np.ndarray]):
    """loader 配列 → 行 dict の列 (ライブ boards.board_to_push_row と同形)。"""
    n = len(snap["ts"])
    cols = {k: snap[k] for k in _ROW_KEYS if k in snap}
    for i in range(n):
        yield {k: float(v[i]) for k, v in cols.items()}


def replay_day(trader: PaperTrader, day: str, codes: list[str]) -> None:
    """録画日 day の codes を PaperTrader に流し込む (finalize まで実施)。"""
    events: list[tuple[float, int, str, dict]] = []
    t_min, t_max = math.inf, -math.inf
    for code in codes:
        snap = loader.load_symbol_day(day, code)
        if len(snap["ts"]) == 0:
            continue
        t_min = min(t_min, float(snap["ts"][0]))
        t_max = max(t_max, float(snap["ts"][-1]))
        for row in iter_symbol_rows(snap):
            events.append((row["ts"], 0, code, row))
    if not events:
        return
    # 境界は連続整数秒の全域 (sessions.decision_grid と同じ範囲規約)
    for t in np.arange(np.ceil(t_min), np.floor(t_max) + 2.0):
        events.append((float(t), 1, "", {}))
    # PUSH (kind=0) を同時刻の境界 (kind=1) より先に処理 = timestamp <= t が境界に見える
    events.sort(key=lambda e: (e[0], e[1]))
    for ts, kind, code, row in events:
        if kind == 0:
            trader.on_push(code, row)
        else:
            trader.on_boundary(ts)
    trader.drain_decisions()
    trader.finalize()
