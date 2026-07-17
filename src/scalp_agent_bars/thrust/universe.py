"""gen7 「イナゴが寄ってくる人気株」ユニバース。pure・numpy のみ。

owner 定義 (2026-07-17 grill-me): **最高値更新した株などイナゴが寄ってくる人気株**。
3 定義を並べてイベント数を先に見る (owner 指定)。選択は「n>=30 / D>=20 を満たすか」
で行い、gross の良し悪しでは選ばない (ADR-0001 §4 best-cell マイニング防止)。

    U1 = new_high AND turnover_spike   (owner 文言の直訳: 最高値更新 + イナゴ流入)
    U2 = turnover_spike                (注目度のみ)
    U3 = new_high                      (最高値更新のみ)

gen4/gen5 のユニバースとの決定的な違い (2026-07-17 調査):
  - gen4/gen5 = 前月末までの trailing 60 日 **median** TurnoverValue 上位 400・**月次**。
    median はスパイクを無視する推定量であり、月次リバランスは月中の発火を翌月まで
    取りこぼす。**イナゴ株を構造的に排除する装置**になっていた。
  - 本モジュールは逆を行く: **スパイク**基準・**日次**リバランス。

因果性 (ADR-0001 G1): 日 d のユニバース判定は d-1 以前の日足のみを使う。
"""
from __future__ import annotations

import numpy as np

# ---- 事前登録パラメータ (掃引禁止) ---------------------------------------------

NEW_HIGH_WINDOW = 60        # 「最高値」= 直近 60 営業日の adj_close 高値
SPIKE_WINDOW = 20           # 売買代金の平常値を測る窓
SPIKE_MULT = 3.0            # イナゴ流入 = 前日代金 >= 3x 平常。検出器の vol_mult と同値
MIN_MEDIAN_CLOSE = 200.0    # 低位株除外 (tick 比スプレッドが支配的。gen4 から継承)
LIQ_WINDOW = 60             # 流動性・株価水準の評価窓

# G7 執行可能性の proxy: 貸借銘柄の履歴区分が listed_info に無いため、
# 「プライム かつ 平常売買代金 >= この額」を代替とする。9107 (24.6B) は通る。
# proxy であることは分析ノートに明記し、PASS した場合のみ kabu API で実在庫を確認する。
MIN_MEDIAN_TURNOVER = 1_000_000_000.0   # 10 億円/日


def pit_flags(
    days: np.ndarray,
    adj_close: np.ndarray,
    close: np.ndarray,
    turnover: np.ndarray,
) -> dict[str, np.ndarray]:
    """1 銘柄の日足系列 (day 昇順) → 各日のユニバース判定フラグ。

    返り値の flag[k] は **days[k] を取引日とする判定**であり、days[<k] のみから
    計算される (G1: 当日の情報を使わない)。

    new_high      : 前日 adj_close が直近 NEW_HIGH_WINDOW 日の高値を更新
    spike         : 前日 turnover >= SPIKE_MULT * median(直近 SPIKE_WINDOW 日)
    shortable_px  : 直近 LIQ_WINDOW 日の median close >= MIN_MEDIAN_CLOSE
    liquid        : 直近 LIQ_WINDOW 日の median turnover >= MIN_MEDIAN_TURNOVER
    """
    n = days.size
    adj_close = np.asarray(adj_close, dtype=np.float64)
    close = np.asarray(close, dtype=np.float64)
    turnover = np.asarray(turnover, dtype=np.float64)

    new_high = np.zeros(n, dtype=bool)
    spike = np.zeros(n, dtype=bool)
    shortable_px = np.zeros(n, dtype=bool)
    liquid = np.zeros(n, dtype=bool)

    for k in range(n):
        # 判定に使えるのは index < k (= 前日以前) のみ
        if k < 1:
            continue
        prev = k - 1

        # new_high: 前日終値が「その前の NEW_HIGH_WINDOW 日の高値」を**上抜いた**。
        # 狭義 (>) にする。>= だと横ばい系列で毎日が「更新」になり、イナゴ株どころか
        # 無風の銘柄を全部拾ってしまう (test_pit_flags_are_causal_no_lookahead が検出)。
        lo = max(0, prev - NEW_HIGH_WINDOW)
        w = adj_close[lo:prev]
        if w.size >= NEW_HIGH_WINDOW and np.isfinite(adj_close[prev]):
            new_high[k] = bool(adj_close[prev] > np.max(w))

        # spike: 前日代金 >= 3x 直近 SPIKE_WINDOW 日 median
        lo_s = max(0, prev - SPIKE_WINDOW + 1)
        ws = turnover[lo_s:prev + 1]
        if ws.size >= SPIKE_WINDOW:
            med = float(np.median(ws))
            if med > 0 and np.isfinite(turnover[prev]):
                spike[k] = turnover[prev] >= SPIKE_MULT * med

        # 流動性・株価水準
        lo_l = max(0, prev - LIQ_WINDOW + 1)
        wc = close[lo_l:prev + 1]
        wt = turnover[lo_l:prev + 1]
        if wc.size >= LIQ_WINDOW:
            shortable_px[k] = float(np.median(wc)) >= MIN_MEDIAN_CLOSE
            liquid[k] = float(np.median(wt)) >= MIN_MEDIAN_TURNOVER

    return {
        "new_high": new_high,
        "spike": spike,
        "shortable_px": shortable_px,
        "liquid": liquid,
    }


def universe_masks(flags: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """pit_flags の出力 → 3 定義それぞれの membership mask。

    hard constraint (全定義共通): shortable_px かつ liquid。
    """
    hard = flags["shortable_px"] & flags["liquid"]
    return {
        "U1_newhigh_and_spike": hard & flags["new_high"] & flags["spike"],
        "U2_spike_only": hard & flags["spike"],
        "U3_newhigh_only": hard & flags["new_high"],
    }


UNIVERSE_NAMES = ("U1_newhigh_and_spike", "U2_spike_only", "U3_newhigh_only")
