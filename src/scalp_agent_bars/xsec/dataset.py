"""gen4 データセット構築 (I/O)。分足読み出し → 特徴/ラベル → parquet キャッシュ。

キャッシュ: artifacts/gen4_xsec/dataset_{scope}.parquet (scope = isval | oos)。
行 = (day, tod, code)、(day, tod) ソート済み。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from scalp_agent_bars.xsec import features as F
from scalp_agent_bars.xsec.config import ART, HORIZON_MIN, MINUTE_DIR, config_hash
from scalp_agent_bars.xsec.friction import friction_bps, friction_bps_stress
from scalp_agent_bars.xsec.universe import month_of

UNIVERSE_PATH = ART / "universe_monthly.json"

SESSION_MIN_TOD = 9 * 3600.0
SESSION_MAX_TOD = 15 * 3600.0 + 30 * 60.0

ATR_DAYS = 14
LIQ_DAYS = 20


def dataset_path(scope: str) -> Path:
    return ART / f"dataset_{scope}.parquet"


def load_universe() -> dict[str, list[str]]:
    return json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))["universe"]


def _load_symbol_bars(code: str, day_min: str, day_max: str) -> dict[str, dict[str, np.ndarray]]:
    """source.load_symbol_days と同じ規約 + Value 列。I/O はここに閉じる。"""
    import duckdb

    with duckdb.connect(str(MINUTE_DIR / f"{code}.duckdb"), read_only=True) as con:
        con.execute("SET enable_progress_bar=false")
        res = con.execute(
            """
            select cast(Date as varchar) as day, Time,
                   Open, High, Low, Close, coalesce(Value, 0) as Value
            from stocks_minute
            where Date >= ? and Date <= ?
              and Open > 0 and High > 0 and Low > 0 and Close > 0
            order by Date, Time
            """,
            [day_min, day_max],
        ).fetchnumpy()
    times = res["Time"]
    tod = np.array(
        [float(t[:2]) * 3600.0 + float(t[3:5]) * 60.0 for t in times], dtype=np.float64
    )
    keep = (tod >= SESSION_MIN_TOD) & (tod <= SESSION_MAX_TOD)
    day_arr = np.asarray(res["day"]).astype(str)[keep]
    tod = tod[keep]
    cols = {
        "open": np.asarray(res["Open"], dtype=np.float64)[keep],
        "high": np.asarray(res["High"], dtype=np.float64)[keep],
        "low": np.asarray(res["Low"], dtype=np.float64)[keep],
        "close": np.asarray(res["Close"], dtype=np.float64)[keep],
        "value": np.asarray(res["Value"], dtype=np.float64)[keep],
    }
    out: dict[str, dict[str, np.ndarray]] = {}
    uniq, first = np.unique(day_arr, return_index=True)
    bounds = np.concatenate([first, [len(day_arr)]])
    for k in range(len(uniq)):
        lo, hi = int(bounds[k]), int(bounds[k + 1])
        out[str(uniq[k])] = {
            "start_tod": tod[lo:hi], **{c: v[lo:hi] for c, v in cols.items()},
        }
    return out


def daily_features_for_code(
    days: np.ndarray, adj_o: np.ndarray, adj_h: np.ndarray, adj_l: np.ndarray,
    adj_c: np.ndarray, turnover: np.ndarray, upper: np.ndarray, lower: np.ndarray,
) -> dict[str, dict[str, float]]:
    """1 銘柄の日次系列 (day 昇順) → day → 因果日次特徴。pure。

    すべて「その日の寄付き時点で既知」の値: gap は当日 adj_open / 前日 adj_close、
    それ以外は前日以前のみ。
    """
    n = len(days)
    out: dict[str, dict[str, float]] = {}
    tr = np.full(n, np.nan)
    for i in range(1, n):
        pc = adj_c[i - 1]
        tr[i] = max(adj_h[i] - adj_l[i], abs(adj_h[i] - pc), abs(adj_l[i] - pc))
    for i in range(1, n):
        pc = adj_c[i - 1]
        d = {
            "gap_bps": (adj_o[i] / pc - 1.0) * 1e4,
            "prev1d_ret_bps": (adj_c[i - 1] / adj_c[i - 2] - 1.0) * 1e4 if i >= 2 else np.nan,
            "prev5d_ret_bps": (adj_c[i - 1] / adj_c[i - 6] - 1.0) * 1e4 if i >= 6 else np.nan,
            "upper": float(upper[i]), "lower": float(lower[i]),
        }
        if i >= ATR_DAYS + 1:
            d["atr14_bps"] = float(np.nanmean(tr[i - ATR_DAYS:i]) / pc * 1e4)
        else:
            d["atr14_bps"] = np.nan
        if i >= LIQ_DAYS:
            med = float(np.median(turnover[i - LIQ_DAYS:i]))
            d["liq_med"] = med
            d["liq_log"] = float(np.log10(med)) if med > 0 else np.nan
        else:
            d["liq_med"] = np.nan
            d["liq_log"] = np.nan
        out[str(days[i])] = d
    return out


def build_dataset(
    scope: str, day_min: str, day_max: str,
    panel: dict[str, np.ndarray], sector_map: dict[str, str],
    progress=None,
) -> None:
    universe = load_universe()
    # code → 参加月集合
    member_months: dict[str, set[str]] = {}
    for m, codes in universe.items():
        for c in codes:
            member_months.setdefault(c, set()).add(m)

    # 日次特徴を code ごとに前計算
    p_codes = panel["code"]
    order = np.lexsort((panel["day"], p_codes))
    p_codes = p_codes[order]
    p_day = panel["day"][order]
    p_cols = {k: panel[k][order] for k in
              ("adj_open", "adj_high", "adj_low", "adj_close", "turnover", "upper", "lower")}
    uniq, first = np.unique(p_codes, return_index=True)
    bounds = np.concatenate([first, [len(p_codes)]])
    code_slice = {str(uniq[k]): (int(bounds[k]), int(bounds[k + 1])) for k in range(len(uniq))}

    rows: dict[str, list] = {k: [] for k in (
        "code", "day", "month", "tod", "sector", "entry_px", "last_close",
        "near_limit", *F.INTRA_FEATURE_NAMES, *F.DAILY_FEATURE_NAMES, "rvol",
        *[f"h{h}_{f}" for h in HORIZON_MIN
          for f in ("exit_px", "exit_reason", "path_min_bps", "path_max_bps")],
    )}

    from scalp_agent_bars.xsec.config import LIMIT_PROXIMITY

    codes_sorted = sorted(member_months)
    for ci, code in enumerate(codes_sorted):
        if code not in code_slice:
            continue
        lo, hi = code_slice[code]
        dfeat = daily_features_for_code(
            p_day[lo:hi], *(p_cols[k][lo:hi] for k in
                            ("adj_open", "adj_high", "adj_low", "adj_close",
                             "turnover", "upper", "lower")),
        )
        try:
            by_day = _load_symbol_bars(code, day_min, day_max)
        except Exception:
            continue
        sector = sector_map.get(code.upper(), sector_map.get(code, "9999"))
        months = member_months[code]
        for day, bars in by_day.items():
            if month_of(day) not in months:
                continue
            df = dfeat.get(day)
            if df is None:
                continue
            day_rows = F.symbol_day_rows(bars)
            if not day_rows:
                continue
            for r in day_rows:
                liq_med = df["liq_med"]
                rvol = r["cum_value"] / liq_med if liq_med and liq_med > 0 else np.nan
                near = (
                    df["upper"] > 0 and r["last_close"] >= df["upper"] * (1 - LIMIT_PROXIMITY)
                ) or (
                    df["lower"] > 0 and r["last_close"] <= df["lower"] * (1 + LIMIT_PROXIMITY)
                )
                rows["code"].append(code)
                rows["day"].append(day)
                rows["month"].append(month_of(day))
                rows["tod"].append(r["tod"])
                rows["sector"].append(sector)
                rows["entry_px"].append(r["entry_px"])
                rows["last_close"].append(r["last_close"])
                rows["near_limit"].append(bool(near))
                for f in F.INTRA_FEATURE_NAMES:
                    rows[f].append(r[f])
                for f in F.DAILY_FEATURE_NAMES:
                    rows[f].append(df[f])
                rows["rvol"].append(rvol)
                for h in HORIZON_MIN:
                    for f in ("exit_px", "exit_reason", "path_min_bps", "path_max_bps"):
                        rows[f"h{h}_{f}"].append(r[f"h{h}_{f}"])
        if progress and (ci + 1) % 50 == 0:
            progress(f"minute {ci + 1}/{len(codes_sorted)} rows={len(rows['code'])}")

    # (day, tod, code) ソート → グループ演算
    day_a = np.asarray(rows["day"])
    tod_a = np.asarray(rows["tod"], dtype=np.float64)
    code_a = np.asarray(rows["code"])
    order = np.lexsort((code_a, tod_a, day_a))
    for k in rows:
        rows[k] = np.asarray(rows[k])[order]
    n = len(rows["code"])
    gkey = np.char.add(np.char.add(rows["day"].astype(str), "|"),
                       rows["tod"].astype(np.float64).astype(np.int64).astype(str))

    # 業種相対 (z 化前の生特徴): ret_open − 同業種平均
    sec_rel = np.array(rows["ret_open_bps"], dtype=np.float64).copy()
    sectors = rows["sector"].astype(str)
    ret_open = np.array(rows["ret_open_bps"], dtype=np.float64)
    for lo_, hi_ in F._group_bounds(gkey):
        v = ret_open[lo_:hi_]
        sec = sectors[lo_:hi_]
        fin = np.isfinite(v)
        mkt = float(np.mean(v[fin])) if fin.sum() else np.nan
        a = v - mkt
        for s in np.unique(sec):
            m = (sec == s) & fin
            if m.sum() >= 5:
                a[m] = v[m] - float(np.mean(v[m]))
        sec_rel[lo_:hi_] = a
    raw_feats = {name: np.array(rows[name], dtype=np.float64)
                 for name in F.MODEL_FEATURE_NAMES if name != "sec_rel_ret_open_bps"}
    raw_feats["sec_rel_ret_open_bps"] = sec_rel

    out_cols: dict[str, np.ndarray] = {
        "code": rows["code"].astype(str), "day": rows["day"].astype(str),
        "month": rows["month"].astype(str), "tod": tod_a[order],
        "sector": sectors, "entry_px": np.array(rows["entry_px"], dtype=np.float64),
        "near_limit": np.array(rows["near_limit"], dtype=bool),
    }
    for name, v in raw_feats.items():
        out_cols[f"z_{name}"] = F.zscore_by_group(v, gkey)
    entry = out_cols["entry_px"]
    out_cols["friction_bps"] = friction_bps(entry)
    out_cols["friction_stress_bps"] = friction_bps_stress(entry)
    for h in HORIZON_MIN:
        exit_px = np.array(rows[f"h{h}_exit_px"], dtype=np.float64)
        raw_fwd = (exit_px / entry - 1.0) * 1e4
        adj, pct = F.adjust_and_rank_labels(raw_fwd, gkey, sectors)
        out_cols[f"h{h}_gross_bps"] = raw_fwd
        out_cols[f"h{h}_adj_bps"] = adj
        out_cols[f"h{h}_pct"] = pct
        out_cols[f"h{h}_exit_reason"] = np.array(rows[f"h{h}_exit_reason"], dtype=np.int8)
        out_cols[f"h{h}_path_min_bps"] = np.array(rows[f"h{h}_path_min_bps"], dtype=np.float64)
        out_cols[f"h{h}_path_max_bps"] = np.array(rows[f"h{h}_path_max_bps"], dtype=np.float64)

    table = pa.table({k: pa.array(v) for k, v in out_cols.items()})
    ART.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, dataset_path(scope))
    meta = {
        "scope": scope, "day_range": [day_min, day_max], "rows": n,
        "config_hash": config_hash(),
    }
    (ART / f"dataset_{scope}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    if progress:
        progress(f"dataset {scope}: rows={n} → {dataset_path(scope)}")


def load_dataset(scope: str) -> dict[str, np.ndarray]:
    t = pq.read_table(dataset_path(scope))
    return {c: t[c].to_numpy(zero_copy_only=False) for c in t.column_names}


def group_sizes(day: np.ndarray, tod: np.ndarray) -> np.ndarray:
    """(day, tod) ソート済み配列 → lambdarank 用グループサイズ列。"""
    gkey = np.char.add(np.char.add(day.astype(str), "|"),
                       tod.astype(np.float64).astype(np.int64).astype(str))
    _, first = np.unique(gkey, return_index=True)
    bounds = np.sort(np.concatenate([first, [len(gkey)]]))
    return np.diff(bounds)
