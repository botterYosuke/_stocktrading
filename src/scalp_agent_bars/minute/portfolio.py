"""P3: プール学習モデルのスコアで、各分境界に score 最大の銘柄だけエントリーする
クロスセクション執行。pure — 状態は関数内のみ。

- 銘柄単位 1 ポジション (保有中の銘柄は候補から外す)
- 各境界で argmax(score) が side クラスかつ score >= τ の銘柄を score 降順に top_k
- 約定・exit はキャッシュ済みバリア解決 (barrier_outcomes_bars) をそのまま使う
"""
from __future__ import annotations

from itertools import groupby

import numpy as np

from scalp_agent.execution import CLS_DOWN, CLS_UP, Trade, make_trade
from scalp_agent.labels import EXIT_NONE


def simulate_cross_section(
    day: str,
    rows_by_code: dict[str, dict],
    tau: float,
    top_k: int,
) -> list[Trade]:
    """1 日分のクロスセクション執行。

    rows_by_code[code] = {"b_ts": (n,), "scores": (n,3), "lf": fields, "sf": fields}
    """
    events: list[tuple[float, str, int]] = []
    for c, r in rows_by_code.items():
        for i, t in enumerate(np.asarray(r["b_ts"], dtype=np.float64)):
            events.append((float(t), c, i))
    events.sort()
    busy: dict[str, float] = {c: -np.inf for c in rows_by_code}
    trades: list[Trade] = []
    for t, group in groupby(events, key=lambda e: e[0]):
        cands = []
        for _, c, i in group:
            r = rows_by_code[c]
            if t < busy[c]:
                continue
            s = r["scores"][i]
            cls = int(np.argmax(s))
            if cls == CLS_UP:
                side, fields = 1, r["lf"]
            elif cls == CLS_DOWN:
                side, fields = -1, r["sf"]
            else:
                continue
            if s[cls] < tau:
                continue
            if fields["reason"][i] == EXIT_NONE:
                continue
            cands.append((float(s[cls]), c, i, side, fields))
        cands.sort(key=lambda x: (-x[0], x[1]))
        for _, c, i, side, fields in cands[:top_k]:
            tr = make_trade(c, day, side, t, fields, i)
            trades.append(tr)
            busy[c] = tr.exit_ts
    return trades
