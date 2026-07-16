"""ラベル生成。taker 前提: 「N 秒後の mid がスプレッド+バッファを超えて動くか」。

pure・numpy のみ。将来リーク防止のため、参照するのは t+horizon **以前で最も近い**
スナップショット (未来を跨がない側に丸めると horizon が縮むので、こちらは
「t+horizon 以降で最初の」点を使う。ホライズン終端をまたいだ直後の値 = 実際に
taker が返済できる時点の板に最も近い)。ホライズン内に次データが無い末尾は無効。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class LabelSpec:
    horizon_s: float          # 何秒先を当てるか
    threshold_spread_mult: float  # 発火閾値 = その時点の spread × この倍率


def forward_mid(ts: np.ndarray, mid_px: np.ndarray, horizon_s: float) -> tuple[np.ndarray, np.ndarray]:
    """各 t について t+horizon_s 以降で最初の mid と、その有効マスクを返す。"""
    idx = np.searchsorted(ts, ts + horizon_s, side="left")
    valid = idx < len(ts)
    fwd = mid_px[np.clip(idx, 0, len(ts) - 1)]
    return np.where(valid, fwd, np.nan), valid


def make_labels(
    ts: np.ndarray,
    mid_px: np.ndarray,
    spread_now: np.ndarray,
    spec: LabelSpec,
) -> tuple[np.ndarray, np.ndarray]:
    """3 値ラベル (+1/-1/0) と有効マスク。

    +1: mid が +spread×mult 以上動いた (ロング taker が往復 friction を超える)
    -1: 同、下方向
     0: どちらでもない
    """
    fwd, valid = forward_mid(ts, mid_px, spec.horizon_s)
    move = fwd - mid_px
    thr = spread_now * spec.threshold_spread_mult
    y = np.zeros(len(ts), dtype=np.int8)
    with np.errstate(invalid="ignore"):
        y[valid & (move >= thr)] = 1
        y[valid & (move <= -thr)] = -1
    return y, valid


# ---- トリプルバリア (gen1 正本。上の mid 基準 make_labels は診断併記用) -------
#
# 2026-07-16 グリル確定仕様:
#   s = entry 時 spread、Δ = s × (mult − 1)   (mult > 1 のみ有効)
#   Long : entry = ask₀、TP: bid ≥ ask₀+Δ、SL: bid ≤ bid₀−Δ
#   Short: entry = bid₀、TP: ask ≤ bid₀−Δ、SL: ask ≥ ask₀+Δ
#   (SL は entry 時の清算可能な対向 best 基準 — mult≤2 の即時 SL 退化を回避)
#   エントリ約定   : 決定 PUSH の厳密な次 PUSH の対向 best
#   first-touch    : entry 約定 PUSH の次の PUSH から開始
#   TP/SL 約定     : トリガー PUSH の厳密な次 PUSH
#   Timeout / 14:55: 時計時刻より後の最初の PUSH で約定 (もう 1 PUSH 待たない)
#   14:55 が先なら強制決済を優先。バリアは entry 時固定・保有中再計算しない
#   ラベル: TP が SL/timeout より先にトリガーされた方向。双方 TP なら先着、
#           同一 timestamp なら 0。実現 PnL のスリッページでラベルは変えない

EXIT_NONE = 0      # 未解決 (データ終端など) — ラベル/取引とも無効
EXIT_TP = 1
EXIT_SL = 2
EXIT_TIMEOUT = 3
EXIT_EOD = 4       # 14:55 強制決済


def _first_touch(arr: np.ndarray, lo: int, hi: int, thr: float, ge: bool) -> int:
    """arr[lo:hi] で条件 (>= / <=) を最初に満たす index。無ければ hi。"""
    if lo >= hi:
        return hi
    win = arr[lo:hi]
    hitmask = (win >= thr) if ge else (win <= thr)
    pos = int(np.argmax(hitmask))
    return lo + pos if hitmask[pos] else hi


def barrier_outcomes_grid(
    ts: np.ndarray,
    tod: np.ndarray,
    bid: np.ndarray,
    ask: np.ndarray,
    decision_idx: np.ndarray,
    decision_ts: np.ndarray,
    mults: tuple[float, ...],
    horizons_s: tuple[float, ...],
    entry_max_latency_s: float,
    force_close_tod: float,
) -> dict[tuple[float, float], dict[str, dict[str, np.ndarray]]]:
    """全 (mult, horizon) × 両サイドのバリア解決を一括計算する。

    入力は executable 部分列。戻り値:
      out[(h, m)]["long"|"short"] = {reason, entry_idx, exit_idx, tp_trigger_ts, mae_bps}
    exit_idx は約定行 (TP/SL はトリガーの次 PUSH、timeout/EOD はその行自身)。
    reason == EXIT_NONE の取引は無効 (エントリ不能・データ終端で未解決)。

    ラベル演算とシミュレータは本関数の出力を共有する (定義乖離の構造的排除)。
    """
    for m in mults:
        if m <= 1.0:
            raise ValueError(f"mult must be > 1 (got {m}); mult=1 はバリア幅ゼロで除外")
    n = len(ts)
    nd = len(decision_idx)
    horizons_sorted = sorted(horizons_s)
    h_max = horizons_sorted[-1]
    # 14:55 より後の最初の行 (tod は日内単調)
    eod_start = int(np.searchsorted(tod, force_close_tod, side="right"))

    out: dict[tuple[float, float], dict[str, dict[str, np.ndarray]]] = {}
    for h in horizons_s:
        for m in mults:
            out[(h, m)] = {
                side: {
                    "reason": np.zeros(nd, dtype=np.int8),
                    "entry_idx": np.full(nd, -1, dtype=np.int64),
                    "exit_idx": np.full(nd, -1, dtype=np.int64),
                    "tp_trigger_ts": np.full(nd, np.nan),
                    "exit_trigger_ts": np.full(nd, np.nan),  # trigger→exit 遅延の記録用
                    "mae_bps": np.full(nd, np.nan),
                }
                for side in ("long", "short")
            }

    for d in range(nd):
        e = int(decision_idx[d]) + 1  # エントリ = 決定 PUSH の厳密な次 PUSH
        if e >= n or (ts[e] - decision_ts[d]) > entry_max_latency_s:
            continue  # エントリ不能 (次 PUSH なし / stale fill ガード)
        if tod[e] >= force_close_tod:
            continue  # 約定が 14:55 以後に落ちる — 新規建て不可
        ask0, bid0 = ask[e], bid[e]
        s = ask0 - bid0
        if not (s > 0):
            continue
        j0 = e + 1  # first-touch は entry 約定 PUSH の次の PUSH から
        mid0 = (ask0 + bid0) / 2.0
        # 時間ベース exit: 各 horizon の「deadline より後の最初の行」
        deadlines = ts[e] + np.asarray(horizons_sorted)
        t_idx = np.searchsorted(ts, deadlines, side="right")
        eodi = max(eod_start, j0)  # entry は 14:55 前なので eod_start > e
        scan_hi = int(min(max(t_idx[-1], j0), eodi, n))
        # MAE 用: [j0, exit] を後で index できるよう exit 行 (最大 scan_hi) まで含める
        win_hi = min(scan_hi + 1, n)
        bid_cummin = np.minimum.accumulate(bid[j0:win_hi]) if win_hi > j0 else np.array([])
        ask_cummax = np.maximum.accumulate(ask[j0:win_hi]) if win_hi > j0 else np.array([])

        def _mae(side: str, exit_j: int) -> float:
            """entry から exit 行 (含む) までの逆行極値 (bps)。経路が空なら 0。"""
            k = exit_j - j0
            if k < 0 or len(bid_cummin) == 0:
                return 0.0
            k = min(k, len(bid_cummin) - 1)
            if side == "long":
                return (mid0 - bid_cummin[k]) / mid0 * 1e4
            return (ask_cummax[k] - mid0) / mid0 * 1e4
        for m in mults:
            delta = s * (m - 1.0)
            # 閾値は entry 時固定 (保有中再計算しない)
            tp_l = _first_touch(bid, j0, scan_hi, ask0 + delta, ge=True)
            sl_l = _first_touch(bid, j0, scan_hi, bid0 - delta, ge=False)
            tp_s = _first_touch(ask, j0, scan_hi, bid0 - delta, ge=False)
            sl_s = _first_touch(ask, j0, scan_hi, ask0 + delta, ge=True)
            for hi_i, h in enumerate(horizons_sorted):
                ti = max(int(t_idx[hi_i]), j0)
                hard_end = min(ti, eodi)  # 14:55 が先なら EOD 優先
                time_reason = EXIT_EOD if eodi <= ti else EXIT_TIMEOUT
                cell = out[(h, m)]
                for side, tp_j, sl_j in (("long", tp_l, sl_l), ("short", tp_s, sl_s)):
                    rec = cell[side]
                    trig = min(tp_j, sl_j)
                    if trig < hard_end:
                        fill = trig + 1  # TP/SL 約定 = トリガーの厳密な次 PUSH
                        if fill >= n:
                            continue  # データ終端で約定不能 → EXIT_NONE
                        rec["reason"][d] = EXIT_TP if tp_j < sl_j else EXIT_SL
                        rec["exit_idx"][d] = fill
                        rec["exit_trigger_ts"][d] = ts[trig]
                        if tp_j < sl_j:
                            rec["tp_trigger_ts"][d] = ts[trig]
                    else:
                        if hard_end >= n:
                            continue  # deadline より後の行が無い → EXIT_NONE
                        fill = hard_end
                        rec["reason"][d] = time_reason
                        rec["exit_idx"][d] = fill
                        # 時計イベントのトリガー時刻 (timeout=deadline / EOD=14:55)
                        day_base = ts[e] - tod[e]
                        rec["exit_trigger_ts"][d] = (
                            day_base + force_close_tod
                            if time_reason == EXIT_EOD
                            else ts[e] + h
                        )
                    rec["entry_idx"][d] = e
                    rec["mae_bps"][d] = _mae(side, fill)
    return out


def labels_from_outcomes(
    long_out: dict[str, np.ndarray],
    short_out: dict[str, np.ndarray],
) -> tuple[np.ndarray, np.ndarray]:
    """両サイドのバリア解決 → 3 値ラベル (+1/-1/0) と有効マスク。

    +1: long TP が先着 / -1: short TP が先着 / 0: どちらも TP でない・同時
    有効 = 両サイドとも解決済み (EXIT_NONE を含む決定行は無効)。
    """
    lr, sr = long_out["reason"], short_out["reason"]
    valid = (lr != EXIT_NONE) & (sr != EXIT_NONE)
    l_tp = valid & (lr == EXIT_TP)
    s_tp = valid & (sr == EXIT_TP)
    lts, sts = long_out["tp_trigger_ts"], short_out["tp_trigger_ts"]
    y = np.zeros(len(lr), dtype=np.int8)
    only_l = l_tp & ~s_tp
    only_s = s_tp & ~l_tp
    both = l_tp & s_tp
    y[only_l] = 1
    y[only_s] = -1
    with np.errstate(invalid="ignore"):
        y[both & (lts < sts)] = 1
        y[both & (sts < lts)] = -1
        # 同一 timestamp は 0 のまま
    return y, valid
