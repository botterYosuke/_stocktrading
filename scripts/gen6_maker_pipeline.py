"""足読み gen6 (maker 執行ルール再検証) パイプライン。

usage:
  uv run python scripts/gen6_maker_pipeline.py build   # 分足再走査 → maker 出来事 parquet
  uv run python scripts/gen6_maker_pipeline.py sweep   # gen5 凍結シグナル × 8 執行セル

規約:
- シグナルは gen5 val 最良セル (hl5, K10) に凍結。探索対象は執行ルールのみ
  (深さ {join, m1} × resting {5, 30 分} × 構成 {a, b} = 8 セル、maker.config_hash で凍結)。
- OOS (2025-04-01..30) は sealed を継承 — 本スクリプトは触れない。
- G6 代替: signal-free maker 対照 (同日・同時刻・同 K・同深さ・ランダム銘柄) との
  gap_z >= 2 を候補条件に課す (構成 a は従来 ratio >= 3 も併用)。
- 注文単位の会計 (owner 修正 4): fill 率 (全体・tod 別・side 別) と未約定注文の
  カウンターファクチュアル taker gross を必ず出力する。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scalp_agent.execution import Trade  # noqa: E402
from scalp_agent.gates import trade_metrics  # noqa: E402
from scalp_agent_bars.xsec import dataset as ds  # noqa: E402
from scalp_agent_bars.xsec import evaluate as ev  # noqa: E402
from scalp_agent_bars.xsec import maker as mk  # noqa: E402
from scalp_agent_bars.xsec import tod_lag as tl  # noqa: E402
from scalp_agent_bars.xsec.config import TRAIN_RANGE, VAL_RANGE  # noqa: E402
from scalp_agent_bars.xsec.universe import month_of  # noqa: E402

ART = Path("artifacts/gen6_maker")
MAKER_PATH = ART / "maker_isval.parquet"
SWEEP_RESULTS_PATH = ART / "sweep_val_results.json"
FROZEN_PATH = ART / "frozen_config.json"

K = mk.SIGNAL_TOP_K
H = mk.HORIZON

COMBO_FIELDS = ("fill", "fill_tod", "entry_px", "gross_a_bps", "gross_b_bps",
                "exit_maker", "path_min_bps", "path_max_bps")


def _combo_cols() -> list[str]:
    cols = []
    for side_name, _ in mk.SIDES:
        for depth in mk.DEPTHS:
            for win in mk.WINDOWS_MIN:
                key = mk.combo_key(depth, win, side_name)
                cols.extend(f"{key}_{f}" for f in COMBO_FIELDS)
    return cols


# ---- build ------------------------------------------------------------------------

def cmd_build() -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    t0 = time.time()
    day_min, day_max = TRAIN_RANGE[0], VAL_RANGE[1]
    universe = ds.load_universe()
    member_months: dict[str, set[str]] = {}
    for m, codes in universe.items():
        for c in codes:
            member_months.setdefault(c, set()).add(m)
    codes_sorted = sorted(member_months)
    print(f"gen6 config_hash = {mk.config_hash()}", flush=True)
    print(f"build: {len(codes_sorted)} codes, {day_min}..{day_max}", flush=True)

    combo_cols = _combo_cols()
    rows: dict[str, list] = {k: [] for k in (
        "code", "day", "tod", "last_close", "taker_exit_px", "taker_exit_reason",
        *combo_cols)}
    for ci, code in enumerate(codes_sorted):
        try:
            by_day = ds._load_symbol_bars(code, day_min, day_max)
        except Exception:
            continue
        months = member_months[code]
        for day, bars in by_day.items():
            if month_of(day) not in months:
                continue
            day_rows = mk.maker_day_rows(bars)
            if not day_rows:
                continue
            for r in day_rows:
                rows["code"].append(code)
                rows["day"].append(day)
                rows["tod"].append(r["tod"])
                rows["last_close"].append(r["last_close"])
                rows["taker_exit_px"].append(r["taker_exit_px"])
                rows["taker_exit_reason"].append(r["taker_exit_reason"])
                for c in combo_cols:
                    rows[c].append(r[c])
        if (ci + 1) % 25 == 0:
            print(f"  {ci + 1}/{len(codes_sorted)} rows={len(rows['code'])} "
                  f"({time.time() - t0:.0f}s)", flush=True)

    out: dict[str, np.ndarray] = {
        "code": np.asarray(rows["code"], dtype=str),
        "day": np.asarray(rows["day"], dtype=str),
        "tod": np.asarray(rows["tod"], dtype=np.float64),
        "last_close": np.asarray(rows["last_close"], dtype=np.float64),
        "taker_exit_px": np.asarray(rows["taker_exit_px"], dtype=np.float64),
        "taker_exit_reason": np.asarray(rows["taker_exit_reason"], dtype=np.int8),
    }
    for c in combo_cols:
        if c.endswith("_fill") or c.endswith("_exit_maker"):
            out[c] = np.asarray(rows[c], dtype=bool)
        else:
            out[c] = np.asarray(rows[c], dtype=np.float64)
    ART.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({k: pa.array(v) for k, v in out.items()}), MAKER_PATH)
    (ART / "maker_isval.meta.json").write_text(json.dumps({
        "day_range": [day_min, day_max], "rows": len(out["code"]),
        "config_hash": mk.config_hash(),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"build done: rows={len(out['code'])} → {MAKER_PATH} "
          f"({time.time() - t0:.0f}s)", flush=True)


# ---- sweep ------------------------------------------------------------------------

def _fmt(v, nd=2):
    return "None" if v is None else round(v, nd)


def _load_maker_aligned(data: dict[str, np.ndarray]) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """maker parquet を gen4 dataset の行順に整列。戻り: (aligned cols, has_maker)。"""
    import pyarrow.parquet as pq

    t = pq.read_table(MAKER_PATH)
    mkd = {c: t[c].to_numpy(zero_copy_only=False) for c in t.column_names}
    key_of = {}
    m_tod = mkd["tod"].astype(np.int64)
    for i in range(len(mkd["code"])):
        key_of[(mkd["code"][i], mkd["day"][i], int(m_tod[i]))] = i
    n = len(data["code"])
    idx = np.full(n, -1, dtype=np.int64)
    d_code = data["code"].astype(str)
    d_day = data["day"].astype(str)
    d_tod = data["tod"].astype(np.float64).astype(np.int64)
    for i in range(n):
        idx[i] = key_of.get((d_code[i], d_day[i], int(d_tod[i])), -1)
    has = idx >= 0
    safe = np.where(has, idx, 0)
    aligned: dict[str, np.ndarray] = {}
    for c, v in mkd.items():
        if c in ("code", "day", "tod"):
            continue
        a = v[safe]
        if v.dtype == bool:
            a = np.where(has, a, False)
        elif np.issubdtype(v.dtype, np.floating):
            a = np.where(has, a, np.nan)
        aligned[c] = a
    return aligned, has


def _cell_frictions(cfg: str, last_close: np.ndarray, exit_maker: np.ndarray,
                    stress: bool) -> np.ndarray:
    if cfg == "a":
        return (mk.friction_stress_config_a(last_close) if stress
                else mk.friction_config_a(last_close))
    return (mk.friction_stress_config_b(last_close, exit_maker) if stress
            else mk.friction_config_b(last_close, exit_maker))


def cmd_sweep() -> None:
    t0 = time.time()
    print(f"gen6 config_hash = {mk.config_hash()}", flush=True)
    data = ds.load_dataset("isval")
    aligned, has_maker = _load_maker_aligned(data)
    print(f"maker join coverage = {has_maker.mean():.4f} "
          f"({int(has_maker.sum())}/{len(has_maker)})", flush=True)
    if has_maker.mean() < 0.99:
        raise SystemExit("maker join coverage < 99% — build を確認")

    day = data["day"].astype(str)
    val_mask = (day >= VAL_RANGE[0]) & (day <= VAL_RANGE[1])

    # gen5 凍結シグナル (pivot は warmup のため全行、評価は val のみ)
    mat, code_idx, tod_idx, day_idx, n_tod = tl.pivot_series(
        data["code"], data["tod"], data["day"], data[f"h{H}_adj_bps"])
    sig = tl.ewma_lag_signal(mat, mk.SIGNAL_HALF_LIFE)
    scores_all = tl.perm_scores(
        sig, code_idx, tod_idx, day_idx, n_tod, tl.identity_perm(n_tod))

    vd = {k: v[val_mask] for k, v in data.items()}
    va = {k: v[val_mask] for k, v in aligned.items()}
    v_has = has_maker[val_mask]
    scores = scores_all[val_mask]
    print(f"rows: val={int(val_mask.sum())} ({time.time() - t0:.0f}s)", flush=True)

    elig = (~vd["near_limit"] & np.isfinite(vd[f"h{H}_gross_bps"])
            & np.isfinite(vd["friction_bps"]) & v_has)
    bounds = ev._group_bounds(vd["day"], vd["tod"])

    # booking: score 上位 K long / 下位 K short (有効数 < 4K のグループはスキップ)
    booked_long: list[np.ndarray] = []
    booked_short: list[np.ndarray] = []
    groups: list[np.ndarray] = []
    for lo, hi in bounds:
        idx = np.arange(lo, hi)
        el = elig[lo:hi]
        cand_all = idx[el]
        if len(cand_all) >= 4 * K:
            groups.append(cand_all)
        cand = idx[el & np.isfinite(scores[lo:hi])]
        if len(cand) < 4 * K:
            continue
        order = np.argsort(scores[cand], kind="stable")
        booked_long.append(cand[order[-K:]])
        booked_short.append(cand[order[:K]])
    bl = np.concatenate(booked_long) if booked_long else np.array([], dtype=int)
    bs = np.concatenate(booked_short) if booked_short else np.array([], dtype=int)
    print(f"orders: long={len(bl)} short={len(bs)} groups={len(groups)}", flush=True)

    v_day = vd["day"].astype(str)
    v_code = vd["code"].astype(str)
    v_tod = vd["tod"].astype(np.float64)
    lc = va["last_close"]
    epoch = np.array([ev._day_epoch(d) for d in v_day])

    rng = np.random.default_rng(20260716)
    results: dict[str, dict] = {}
    candidates: list[tuple[float, str]] = []
    for depth in mk.DEPTHS:
        for win in mk.WINDOWS_MIN:
            side_cols = {}
            for side_name, side in mk.SIDES:
                key = mk.combo_key(depth, win, side_name)
                side_cols[side] = {f: va[f"{key}_{f}"] for f in COMBO_FIELDS}
            for cfg in mk.CONFIGS:
                cell = f"{depth}|w{win}|{cfg}"
                gross_col = f"gross_{cfg}_bps"
                # per-side net 配列 (val 全行。未 fill は nan)
                net_by_side, fric_by_side = {}, {}
                for _, side in mk.SIDES:
                    sc_ = side_cols[side]
                    fr = _cell_frictions(cfg, lc, sc_["exit_maker"], stress=False)
                    fric_by_side[side] = fr
                    net_by_side[side] = np.where(
                        sc_["fill"], sc_[gross_col] - fr, np.nan)

                trades: list[Trade] = []
                stress_nets: list[float] = []
                n_orders = 0
                n_filled = 0
                cf_unfilled: list[float] = []
                cf_filled_taker: list[float] = []
                fill_by_tod: dict[int, list[int]] = {}
                n_exit_maker = 0
                fill_by_side = {1: [0, 0], -1: [0, 0]}
                for side, booked in ((1, bl), (-1, bs)):
                    sc_ = side_cols[side]
                    fr_stress = _cell_frictions(cfg, lc, sc_["exit_maker"], stress=True)
                    for i in booked:
                        n_orders += 1
                        tkey = int(v_tod[i])
                        fill_by_tod.setdefault(tkey, [0, 0])
                        fill_by_side[side][1] += 1
                        taker_gross = side * float(vd[f"h{H}_gross_bps"][i])
                        if not sc_["fill"][i]:
                            fill_by_tod[tkey][1] += 1
                            cf_unfilled.append(taker_gross)
                            continue
                        n_filled += 1
                        fill_by_tod[tkey][0] += 1
                        fill_by_tod[tkey][1] += 1
                        fill_by_side[side][0] += 1
                        cf_filled_taker.append(taker_gross)
                        if cfg == "b" and sc_["exit_maker"][i]:
                            n_exit_maker += 1
                        gross = float(sc_[gross_col][i])
                        fric = float(fric_by_side[side][i])
                        entry_px = float(sc_["entry_px"][i])
                        exit_px = entry_px * (1.0 + side * gross / 1e4)
                        if side == 1:
                            mae = max(0.0, -float(sc_["path_min_bps"][i]))
                        else:
                            mae = max(0.0, float(sc_["path_max_bps"][i]))
                        t_dec = epoch[i] + v_tod[i]
                        reason = (2 if (cfg == "b" and sc_["exit_maker"][i])
                                  else int(va["taker_exit_reason"][i]))
                        trades.append(Trade(
                            code=v_code[i], day=v_day[i], side=side,
                            decision_ts=t_dec,
                            entry_ts=epoch[i] + float(sc_["fill_tod"][i]),
                            exit_ts=t_dec + H * 60.0,
                            entry_px=entry_px, exit_px=exit_px,
                            mid_entry=entry_px, mid_exit=exit_px,
                            exit_reason=reason, exit_trigger_ts=t_dec + H * 60.0,
                            mae_bps=mae, gross_bps=gross, friction_bps=fric,
                            net_bps=gross - fric,
                        ))
                        stress_nets.append(gross - float(fr_stress[i]))

                m = trade_metrics(trades)
                m["stress_net_per_entry"] = (
                    float(np.mean(stress_nets)) if stress_nets else None)

                # signal-free maker 対照 (G2 拡張 = 両側 maker の G6 代替)
                means = []
                fill_rates = []
                for _ in range(mk.CONTROL_SHUFFLES):
                    nets = []
                    n_ord = 0
                    for cand in groups:
                        pick = rng.choice(cand, size=2 * K, replace=False)
                        nets.append(net_by_side[1][pick[:K]])
                        nets.append(net_by_side[-1][pick[K:]])
                        n_ord += 2 * K
                    allv = np.concatenate(nets)
                    fin = np.isfinite(allv)
                    if fin.sum():
                        means.append(float(allv[fin].mean()))
                        fill_rates.append(fin.sum() / n_ord)
                ctl_mean = float(np.mean(means)) if means else None
                ctl_std = (float(np.std(means, ddof=1))
                           if len(means) > 1 else None)
                gap = (m["net_per_entry"] - ctl_mean
                       if m["net_per_entry"] is not None and ctl_mean is not None
                       else None)
                control = {
                    "control_net_mean": ctl_mean, "control_net_std": ctl_std,
                    "control_fill_rate": (float(np.mean(fill_rates))
                                          if fill_rates else None),
                    "gap": gap,
                    "gap_z": (gap / ctl_std if gap is not None and ctl_std
                              else None),
                    "control_positive": (ctl_mean is not None and ctl_mean > 0),
                }
                m["control"] = control

                # 注文単位の会計 (owner 修正 4)
                m["orders"] = {
                    "n_orders": n_orders, "n_filled": n_filled,
                    "fill_rate": n_filled / n_orders if n_orders else None,
                    "fill_rate_by_tod": {
                        str(k): (v[0] / v[1] if v[1] else None)
                        for k, v in sorted(fill_by_tod.items())},
                    "fill_rate_by_side": {
                        ("long" if s == 1 else "short"):
                            (v[0] / v[1] if v[1] else None)
                        for s, v in fill_by_side.items()},
                    "unfilled_cf_taker_gross_bps": (
                        float(np.mean(cf_unfilled)) if cf_unfilled else None),
                    "filled_cf_taker_gross_bps": (
                        float(np.mean(cf_filled_taker)) if cf_filled_taker else None),
                    "exit_maker_rate": (
                        n_exit_maker / n_filled
                        if cfg == "b" and n_filled else None),
                }
                results[cell] = m
                cand_ok = mk.is_candidate(m, control, cfg)
                if cand_ok and m["net_per_entry"] is not None:
                    candidates.append((m["net_per_entry"], cell))
                o = m["orders"]
                print(
                    f"  {cell}: n={m['n']} D={m['D']} fill={_fmt(o['fill_rate'], 3)} "
                    f"net={_fmt(m['net_per_entry'])} gross={_fmt(m['gross_per_entry'])} "
                    f"fric={_fmt(m['friction_per_entry'])} t={_fmt(m['t_net'])} "
                    f"ctl={_fmt(control['control_net_mean'])} "
                    f"gap_z={_fmt(control['gap_z'])} "
                    f"cf_unfilled={_fmt(o['unfilled_cf_taker_gross_bps'])} "
                    f"exit_mk={_fmt(o['exit_maker_rate'], 3)} cand={cand_ok}",
                    flush=True,
                )

    slim = {k: {kk: vv for kk, vv in m.items() if kk not in ("by_code", "by_day")}
            for k, m in results.items()}
    SWEEP_RESULTS_PATH.write_text(json.dumps({
        "config_hash": mk.config_hash(), "signal": "gen5 hl5 k10 frozen",
        "results": slim,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"results → {SWEEP_RESULTS_PATH} ({time.time() - t0:.0f}s)", flush=True)

    if not candidates:
        FROZEN_PATH.write_text(json.dumps({
            "verdict": "IS-KILL",
            "reason": ("no execution cell met candidate conditions "
                       "(n/D/net>0/shares + control gap_z>=2, ratio>=3 for cfg a)"),
            "config_hash": mk.config_hash(),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print("IS-KILL: no candidate. OOS stays sealed.", flush=True)
        return
    candidates.sort(reverse=True)
    best = candidates[0][1]
    FROZEN_PATH.write_text(json.dumps({
        "verdict": "FROZEN", "cell": best,
        "val_metrics": {k: v for k, v in results[best].items()
                        if k not in ("by_code", "by_day")},
        "config_hash": mk.config_hash(),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"FROZEN: {best} net/entry={results[best]['net_per_entry']:.2f}bps",
          flush=True)


def main() -> None:
    cmds = {"build": cmd_build, "sweep": cmd_sweep}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        raise SystemExit(f"usage: gen6_maker_pipeline.py {{{'|'.join(cmds)}}}")
    cmds[sys.argv[1]]()


if __name__ == "__main__":
    main()
