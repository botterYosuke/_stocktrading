"""gen4 point-in-time ユニバース選定。pure (入力は daily パネル配列)。

月 m のユニバース = 前月末までの trailing LIQUIDITY_WINDOW_DAYS 営業日における
median TurnoverValue 上位 UNIVERSE_SIZE。
条件: median close >= MIN_MEDIAN_CLOSE / プライム / 分足ファイル存在 /
窓内の観測数 >= 窓の 8 割 (新規上場・長期停止を除外)。
lookahead なし: 月 m の判定に使うのは m-1 月末以前のデータのみ。
"""
from __future__ import annotations

import numpy as np

from scalp_agent_bars.xsec.config import (
    LIQUIDITY_WINDOW_DAYS,
    MIN_MEDIAN_CLOSE,
    UNIVERSE_SIZE,
)

MIN_WINDOW_COVERAGE = 0.8


def month_of(day: str) -> str:
    return day[:7]


def build_monthly_universe(
    panel: dict[str, np.ndarray],
    months: list[str],
    prime: set[str],
    has_minute: set[str],
    size: int = UNIVERSE_SIZE,
) -> dict[str, list[str]]:
    """month → 選定 code リスト (流動性降順)。

    panel: daily.load_daily_panel() の配列辞書 (day 昇順である必要はない)。
    """
    codes = panel["code"]
    days = panel["day"]
    closes = panel["close"]
    turnovers = panel["turnover"]

    all_days = np.unique(days)  # 昇順
    out: dict[str, list[str]] = {}
    for m in months:
        # 窓 = 月 m の初日より前の直近 LIQUIDITY_WINDOW_DAYS 営業日
        before = all_days[all_days < m]  # "YYYY-MM" < "YYYY-MM-DD" 比較で月初日前
        if len(before) < LIQUIDITY_WINDOW_DAYS:
            raise ValueError(f"{m}: trailing 窓に営業日が足りない ({len(before)})")
        window = set(before[-LIQUIDITY_WINDOW_DAYS:].tolist())
        in_win = np.isin(days, list(window))
        w_codes = codes[in_win]
        w_close = closes[in_win]
        w_turn = turnovers[in_win]

        order = np.argsort(w_codes, kind="stable")
        w_codes, w_close, w_turn = w_codes[order], w_close[order], w_turn[order]
        uniq, first = np.unique(w_codes, return_index=True)
        bounds = np.concatenate([first, [len(w_codes)]])
        scored: list[tuple[float, str]] = []
        min_obs = int(np.ceil(LIQUIDITY_WINDOW_DAYS * MIN_WINDOW_COVERAGE))
        for k in range(len(uniq)):
            c = str(uniq[k])
            lo, hi = int(bounds[k]), int(bounds[k + 1])
            if hi - lo < min_obs:
                continue
            if (c not in prime and c.upper() not in prime) or c not in has_minute:
                continue
            med_close = float(np.median(w_close[lo:hi]))
            if med_close < MIN_MEDIAN_CLOSE:
                continue
            med_turn = float(np.median(w_turn[lo:hi]))
            if med_turn <= 0:
                continue
            scored.append((med_turn, c))
        scored.sort(reverse=True)
        out[m] = [c for _, c in scored[:size]]
    return out


def months_between(day_min: str, day_max: str) -> list[str]:
    y0, m0 = int(day_min[:4]), int(day_min[5:7])
    y1, m1 = int(day_max[:4]), int(day_max[5:7])
    out = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out
