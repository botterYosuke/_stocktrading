"""gen8: 実現 net_bps 教師と分位棄却付き売買規則。pure・numpy のみ。

教師はキャッシュ済み per-side フィールド (dataset.build_day_rows が実体化) から
execution.trade_pnl_bps と同一の恒等式で作る。ラベルと執行規則の乖離は構造的に
起きない (gen2 と同じ設計原則)。
"""
from __future__ import annotations

import numpy as np

from scalp_agent.execution import SIDE_FIELDS, Trade, make_trade
from scalp_agent.labels import EXIT_NONE


def side_net_labels(
    table: dict[str, np.ndarray], ck: str, side_prefix: str
) -> tuple[np.ndarray, np.ndarray]:
    """(net_bps, valid) — 決定行ごとの実現完全往復 net (半スプレッド往復込み)。

    side_prefix: "L" | "S"。valid = バリア解決済み (reason != EXIT_NONE)。
    trade_pnl_bps の net_yen = side × (exit_px − entry_px) / mid_entry と同一。
    """
    sgn = 1.0 if side_prefix == "L" else -1.0
    reason = np.asarray(table[f"{ck}_{side_prefix}_reason"])
    entry_px = np.asarray(table[f"{ck}_{side_prefix}_entry_px"], dtype=np.float64)
    exit_px = np.asarray(table[f"{ck}_{side_prefix}_exit_px"], dtype=np.float64)
    mid_entry = np.asarray(table[f"{ck}_{side_prefix}_mid_entry"], dtype=np.float64)
    valid = (reason != EXIT_NONE) & np.isfinite(entry_px) & np.isfinite(exit_px) \
        & np.isfinite(mid_entry) & (mid_entry > 0)
    net = np.full(len(reason), np.nan)
    net[valid] = sgn * (exit_px[valid] - entry_px[valid]) / mid_entry[valid] * 1e4
    return net, valid


def select_entries(
    q_long: np.ndarray,
    m_long: np.ndarray,
    q_short: np.ndarray,
    m_short: np.ndarray,
) -> np.ndarray:
    """決定行ごとの発注サイド (+1 / -1 / 0)。

    発火 ⟺ 下側分位予測 q̂ > 0。両サイド発火なら期待値予測の大きい側、
    同値なら見送り (凍結規則 — 事前登録 §2)。
    """
    fire_l = np.asarray(q_long) > 0
    fire_s = np.asarray(q_short) > 0
    both = fire_l & fire_s
    side = np.where(fire_l, 1, 0) + np.where(fire_s, -1, 0)  # both は 0 に相殺
    ml = np.asarray(m_long)
    ms = np.asarray(m_short)
    side = np.where(both & (ml > ms), 1, side)
    side = np.where(both & (ms > ml), -1, side)
    return side.astype(np.int64)


def simulate_symbol_day_selector(
    code: str,
    day: str,
    decision_ts: np.ndarray,
    sides: np.ndarray,
    long_fields: dict[str, np.ndarray],
    short_fields: dict[str, np.ndarray],
) -> list[Trade]:
    """1 銘柄 1 日を決定行順に走査 (銘柄単位 1 ポジション・busy_until は gen2 と同一)。

    sides: select_entries の出力 (+1/-1/0)。
    """
    trades: list[Trade] = []
    busy_until = -np.inf
    for d in range(len(decision_ts)):
        s = int(sides[d])
        if s == 0 or decision_ts[d] < busy_until:
            continue
        fields = long_fields if s == 1 else short_fields
        if fields["reason"][d] == EXIT_NONE:
            continue  # エントリ不能または未解決
        trade = make_trade(code, day, s, float(decision_ts[d]), fields, d)
        trades.append(trade)
        busy_until = trade.exit_ts
    return trades


def side_fields_of(table: dict[str, np.ndarray], ck: str, side_prefix: str,
                   mask: np.ndarray) -> dict[str, np.ndarray]:
    """キャッシュテーブル → SIDE_FIELDS 配列辞書 (mask 行のみ)。"""
    return {f: np.asarray(table[f"{ck}_{side_prefix}_{f}"])[mask] for f in SIDE_FIELDS}
