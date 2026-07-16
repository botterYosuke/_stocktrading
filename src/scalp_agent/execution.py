"""保守的 taker 執行シミュレータ。pure・状態は引数と戻り値のみ。

- バリア解決 (labels.barrier_outcomes_grid) をラベル生成と共有し、シミュレータは
  「どの決定行でエントリしたか」の選択だけを行う → ラベルと執行規則の乖離が
  構造的に起きない。
- 執行規則: argmax(score) が UP/DOWN かつ score ≥ τ (long/short 共通単一 τ)。
  softmax 出力は較正済み確率ではなく未較正 score として扱う。
- 銘柄単位 1 ポジション (保有中は新規エントリ禁止)・銘柄間は無制限・仮想 1 単位。
- PnL は実約定価格で計算し、取引単位で gross − friction = net が厳密に成立する:
    gross_yen    = side × (mid_exit − mid_entry)
    friction_yen = side × (entry_px − mid_entry) + side × (mid_exit − exit_px)
    net_yen      = side × (exit_px − entry_px) = gross_yen − friction_yen
  bps 換算の分母は entry 行の mid で統一する。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from scalp_agent.labels import EXIT_NONE

CLS_DOWN, CLS_FLAT, CLS_UP = 0, 1, 2  # ラベル {-1, 0, +1} → クラス index {0, 1, 2}


@dataclass(frozen=True)
class Trade:
    code: str
    day: str
    side: int              # +1 long / -1 short
    decision_ts: float
    entry_ts: float
    exit_ts: float
    entry_px: float
    exit_px: float
    mid_entry: float
    mid_exit: float
    exit_reason: int
    exit_trigger_ts: float  # TP/SL はトリガー PUSH、timeout/EOD は時計時刻
    mae_bps: float
    gross_bps: float
    friction_bps: float
    net_bps: float

    @property
    def decision_to_entry_ms(self) -> float:
        return (self.entry_ts - self.decision_ts) * 1e3

    @property
    def trigger_to_exit_ms(self) -> float:
        return (self.exit_ts - self.exit_trigger_ts) * 1e3


def trade_pnl_bps(
    side: int,
    entry_px: float,
    exit_px: float,
    mid_entry: float,
    mid_exit: float,
) -> tuple[float, float, float]:
    """(gross_bps, friction_bps, net_bps)。恒等式 gross − friction = net。"""
    gross_yen = side * (mid_exit - mid_entry)
    friction_yen = side * (entry_px - mid_entry) + side * (mid_exit - exit_px)
    net_yen = side * (exit_px - entry_px)
    k = 1e4 / mid_entry
    return gross_yen * k, friction_yen * k, net_yen * k


# キャッシュに実体化された per-side フィールド (dataset.py が生成)
SIDE_FIELDS = (
    "reason", "entry_ts", "exit_ts", "entry_px", "exit_px",
    "mid_entry", "mid_exit", "tp_trigger_ts", "exit_trigger_ts", "mae_bps",
)


def make_trade(
    code: str,
    day: str,
    side: int,
    decision_ts: float,
    fields: dict[str, np.ndarray],
    d: int,
) -> Trade:
    """決定行 d の per-side 実体化フィールドから Trade を生成する。"""
    entry_px = float(fields["entry_px"][d])
    exit_px = float(fields["exit_px"][d])
    mid_e = float(fields["mid_entry"][d])
    mid_x = float(fields["mid_exit"][d])
    gross, friction, net = trade_pnl_bps(side, entry_px, exit_px, mid_e, mid_x)
    return Trade(
        code=code, day=day, side=side,
        decision_ts=float(decision_ts),
        entry_ts=float(fields["entry_ts"][d]), exit_ts=float(fields["exit_ts"][d]),
        entry_px=entry_px, exit_px=exit_px, mid_entry=mid_e, mid_exit=mid_x,
        exit_reason=int(fields["reason"][d]),
        exit_trigger_ts=float(fields["exit_trigger_ts"][d]),
        mae_bps=float(fields["mae_bps"][d]),
        gross_bps=gross, friction_bps=friction, net_bps=net,
    )


def simulate_symbol_day(
    code: str,
    day: str,
    decision_ts: np.ndarray,
    scores: np.ndarray,
    long_fields: dict[str, np.ndarray],
    short_fields: dict[str, np.ndarray],
    tau: float,
) -> list[Trade]:
    """1 銘柄 1 日を決定行順に走査して取引列を返す。

    scores: (n_decisions, 3) の未較正 softmax score (列 = [down, flat, up])。
    long_fields / short_fields: SIDE_FIELDS の配列辞書 (キャッシュ由来)。
    """
    cls = np.argmax(scores, axis=1)
    trades: list[Trade] = []
    busy_until = -np.inf
    for d in range(len(decision_ts)):
        if decision_ts[d] < busy_until:
            continue  # ポジション保有中は新規エントリ禁止
        c = cls[d]
        if c == CLS_UP:
            side, fields = 1, long_fields
        elif c == CLS_DOWN:
            side, fields = -1, short_fields
        else:
            continue
        if scores[d, c] < tau:
            continue
        if fields["reason"][d] == EXIT_NONE:
            continue  # エントリ不能または未解決
        trade = make_trade(code, day, side, float(decision_ts[d]), fields, d)
        trades.append(trade)
        busy_until = trade.exit_ts
    return trades


def max_concurrency(trades: list[Trade]) -> int:
    """全銘柄合算の同時保有数の最大値 (診断出力・資金制約検討の材料)。"""
    events: list[tuple[float, int]] = []
    for t in trades:
        events.append((t.entry_ts, 1))
        events.append((t.exit_ts, -1))
    events.sort()
    cur = peak = 0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)
    return peak
