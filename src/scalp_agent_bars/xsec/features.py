"""gen4 特徴量・ラベル。pure・numpy のみ。

行 = (day, decision_tod, code)。2 段階:
1. `symbol_day_rows`: 1 銘柄 1 日の分足 → 判断時刻ごとの因果特徴 + entry/exit 価格。
   バー t は start+60s に確定するため、判断時刻 T で使えるのは start <= T-60 のバー。
2. `assemble_cross_section`: 全行を (day, tod) グループで z-score 化し、
   市場・業種控除後 forward リターンとその横断百分位 (教師) を付ける。
"""
from __future__ import annotations

import numpy as np

from scalp_agent_bars.xsec.config import (
    DECISION_TODS,
    ENTRY_MAX_DELAY_S,
    HORIZON_MIN,
    SECTOR_MIN_MEMBERS,
)

# 1 銘柄段階の生特徴 (z 化前)
INTRA_FEATURE_NAMES: tuple[str, ...] = (
    "ret_open_bps", "ret_5m_bps", "ret_15m_bps", "ret_30m_bps",
    "vol1m_bps", "range_pos", "hl_range_bps", "cum_value",
)
DAILY_FEATURE_NAMES: tuple[str, ...] = (
    "gap_bps", "prev1d_ret_bps", "prev5d_ret_bps", "atr14_bps", "liq_log",
)
# rvol = cum_value / trailing 中央値 turnover は assemble 前に dataset 側で作る
MODEL_FEATURE_NAMES: tuple[str, ...] = (
    "gap_bps", "prev1d_ret_bps", "prev5d_ret_bps", "atr14_bps", "liq_log",
    "ret_open_bps", "ret_5m_bps", "ret_15m_bps", "ret_30m_bps",
    "vol1m_bps", "range_pos", "hl_range_bps", "rvol",
    "sec_rel_ret_open_bps",
)

EXIT_HORIZON = 0    # horizon のバー始値で exit
EXIT_DAY_END = 1    # horizon までにバーが無く日の最終バー close で強制 exit


def _asof_close(start_tod: np.ndarray, close: np.ndarray, t: float) -> float:
    """t 時点で確定済み (start <= t-60) の最新 close。無ければ nan。"""
    i = np.searchsorted(start_tod, t - 60.0, side="right") - 1
    return float(close[i]) if i >= 0 else np.nan


def symbol_day_rows(bars: dict[str, np.ndarray]) -> list[dict] | None:
    """1 銘柄 1 日 → 判断時刻ごとの dict のリスト。バーが無い時刻は行を作らない。

    bars: {start_tod, open, high, low, close, vol, value} (日内昇順)。
    戻り値の各行: tod, INTRA_FEATURE_NAMES, entry_px,
                  h{15,30,60}_exit_px / _exit_reason / _path_min_bps / _path_max_bps
    """
    st = bars["start_tod"]
    if len(st) < 10:
        return None
    op, hi, lo, cl = bars["open"], bars["high"], bars["low"], bars["close"]
    value = bars["value"]
    day_open = float(op[0])
    if day_open <= 0:
        return None

    rows: list[dict] = []
    for tod in DECISION_TODS:
        done = np.searchsorted(st, tod - 60.0, side="right")  # 確定済みバー数
        if done < 5:
            continue
        last_close = float(cl[done - 1])
        c_open = (last_close / day_open - 1.0) * 1e4
        refs = {}
        for k, name in ((5, "ret_5m_bps"), (15, "ret_15m_bps"), (30, "ret_30m_bps")):
            ref = _asof_close(st, cl, tod - k * 60.0)
            refs[name] = (last_close / ref - 1.0) * 1e4 if np.isfinite(ref) and ref > 0 else np.nan
        with np.errstate(invalid="ignore", divide="ignore"):
            rets = np.diff(np.log(cl[:done])) * 1e4
        vol1m = float(np.std(rets)) if len(rets) >= 5 else np.nan
        d_hi, d_lo = float(np.max(hi[:done])), float(np.min(lo[:done]))
        rng = d_hi - d_lo
        range_pos = (last_close - d_lo) / rng if rng > 0 else 0.5
        hl_range = rng / day_open * 1e4
        cum_val = float(np.sum(value[:done]))

        j = np.searchsorted(st, tod, side="left")
        if j >= len(st) or st[j] > tod + ENTRY_MAX_DELAY_S:
            continue
        entry_px = float(op[j])
        if entry_px <= 0:
            continue

        row = {
            "tod": float(tod), "last_close": last_close,
            "ret_open_bps": c_open, **refs,
            "vol1m_bps": vol1m, "range_pos": range_pos,
            "hl_range_bps": hl_range, "cum_value": cum_val,
            "entry_px": entry_px,
        }
        for h in HORIZON_MIN:
            e = np.searchsorted(st, tod + h * 60.0, side="left")
            if e < len(st):
                exit_px, reason, e_incl = float(op[e]), EXIT_HORIZON, e
            else:
                exit_px, reason, e_incl = float(cl[-1]), EXIT_DAY_END, len(st) - 1
            p_min = float(np.min(lo[j:e_incl + 1]))
            p_max = float(np.max(hi[j:e_incl + 1]))
            row[f"h{h}_exit_px"] = exit_px
            row[f"h{h}_exit_reason"] = reason
            row[f"h{h}_path_min_bps"] = (p_min / entry_px - 1.0) * 1e4
            row[f"h{h}_path_max_bps"] = (p_max / entry_px - 1.0) * 1e4
        rows.append(row)
    return rows or None


def _group_bounds(keys: np.ndarray) -> list[tuple[int, int]]:
    """ソート済み key 配列 → [lo, hi) 区間リスト。"""
    uniq, first = np.unique(keys, return_index=True)
    bounds = np.concatenate([first, [len(keys)]])
    return [(int(bounds[k]), int(bounds[k + 1])) for k in range(len(uniq))]


def zscore_by_group(
    values: np.ndarray, group_keys: np.ndarray, clip: float = 3.0
) -> np.ndarray:
    """(day,tod) グループ内 z-score。group_keys でソート済み前提。nan は nan のまま。"""
    out = np.full(len(values), np.nan)
    for lo, hi in _group_bounds(group_keys):
        v = values[lo:hi]
        fin = np.isfinite(v)
        if fin.sum() < 10:
            continue
        mu = float(np.mean(v[fin]))
        sd = float(np.std(v[fin]))
        if sd <= 0:
            out[lo:hi] = 0.0
            continue
        z = (v - mu) / sd
        out[lo:hi] = np.clip(z, -clip, clip)
    return out


def adjust_and_rank_labels(
    raw_fwd_bps: np.ndarray,
    group_keys: np.ndarray,
    sectors: np.ndarray,
    min_members: int = SECTOR_MIN_MEMBERS,
) -> tuple[np.ndarray, np.ndarray]:
    """市場・業種控除 + 横断百分位。group_keys でソート済み前提。

    adj = raw − (同 (day,tod) の業種平均、メンバー < min_members なら市場平均)
    pct = adj の (day,tod) 内百分位 [0,1]。有効観測 < 20 のグループは nan。
    """
    adj = np.full(len(raw_fwd_bps), np.nan)
    pct = np.full(len(raw_fwd_bps), np.nan)
    for lo, hi in _group_bounds(group_keys):
        v = raw_fwd_bps[lo:hi]
        sec = sectors[lo:hi]
        fin = np.isfinite(v)
        if fin.sum() < 20:
            continue
        mkt_mean = float(np.mean(v[fin]))
        a = v - mkt_mean
        for s in np.unique(sec):
            m = (sec == s) & fin
            if m.sum() >= min_members:
                a[m] = v[m] - float(np.mean(v[m]))
        a[~fin] = np.nan
        adj[lo:hi] = a
        order = np.argsort(np.argsort(a[fin], kind="stable"), kind="stable")
        p = np.full(len(v), np.nan)
        p[fin] = order / max(fin.sum() - 1, 1)
        pct[lo:hi] = p
    return adj, pct
