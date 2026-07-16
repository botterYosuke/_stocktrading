"""セッション窓・執行可能マスク・1Hz 決定グリッド。すべて pure。

因果規則 (2026-07-16 確定):
- 新規エントリ判定は整数秒境界 t で、timestamp <= t の最後の PUSH を使う
- 前回境界以降に新着 PUSH がなければ推論しない (同一 PUSH の再利用禁止)
- 「秒内の最終 PUSH をその秒の開始時点のデータとして扱う」は禁止 (lookahead)

執行可能 (executable) の定義:
- ask > bid (板寄せ・特別気配等のクロス/同値板を除外)
- 時刻が前場 [09:00, 11:30) または後場 [12:30, 15:20] 内
バリア判定・約定はこの executable 部分列の上でのみ行う。
"""
from __future__ import annotations

import numpy as np

from scalp_agent.config import (
    ENTRY_END_TOD,
    SESSION_AFTERNOON,
    SESSION_MORNING,
)


def time_of_day(ts: np.ndarray) -> np.ndarray:
    """naive JST epoch 秒 → 当日 0 時からの秒。"""
    return np.mod(ts, 86400.0)


def executable_mask(ts: np.ndarray, bid_px_1: np.ndarray, ask_px_1: np.ndarray) -> np.ndarray:
    tod = time_of_day(ts)
    in_morning = (tod >= SESSION_MORNING[0]) & (tod < SESSION_MORNING[1])
    in_afternoon = (tod >= SESSION_AFTERNOON[0]) & (tod <= SESSION_AFTERNOON[1])
    return (ask_px_1 > bid_px_1) & (in_morning | in_afternoon)


def exec_subset(snap: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """loader 出力を executable 部分列に絞る。以降の判定は全てこの列上。"""
    mask = executable_mask(snap["ts"], snap["bid_px_1"], snap["ask_px_1"])
    return {k: v[mask] for k, v in snap.items()}


def decision_grid(ts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """1Hz 決定グリッド。(decision_row_idx, decision_boundary_ts) を返す。

    - 境界 t は連続整数秒の全域で走らせ、同一 PUSH の重複採用を dedupe した後、
      エントリ窓 ([09:00,11:30) ∪ [12:30,14:55)) 内の境界だけ残す。
      こうすることで「PUSH の初回評価境界は必ず到着後 1 秒以内」が保たれ、
      前場末尾の PUSH が 12:30 の境界で stale 評価される事故を防ぐ。
    - 戻り値 idx は executable 部分列へのインデックス。
    """
    n = len(ts)
    if n == 0:
        return np.array([], dtype=np.int64), np.array([], dtype=np.float64)
    # 最終 PUSH の直後の境界まで含める (到着後 1 秒以内の初回評価を保証)
    boundaries = np.arange(np.ceil(ts[0]), np.floor(ts[-1]) + 2.0)
    if len(boundaries) == 0:
        return np.array([], dtype=np.int64), np.array([], dtype=np.float64)
    idx = np.searchsorted(ts, boundaries, side="right") - 1
    valid = idx >= 0
    # dedupe: 同じ PUSH を 2 度目以降の境界で使わない (新着なしなら推論しない)
    first_seen = np.ones(len(boundaries), dtype=bool)
    first_seen[1:] = idx[1:] != idx[:-1]
    keep = valid & first_seen
    # エントリ窓内の境界のみ (dedupe 後に窓フィルタ、の順序が重要)
    tod = np.mod(boundaries, 86400.0)
    in_window = (
        ((tod >= SESSION_MORNING[0]) & (tod < SESSION_MORNING[1]))
        | ((tod >= SESSION_AFTERNOON[0]) & (tod < ENTRY_END_TOD))
    )
    keep &= in_window
    return idx[keep].astype(np.int64), boundaries[keep]
