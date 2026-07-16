#!/usr/bin/env python
"""スキャル特化ユニバース分析 (DESIGN.md 決定 #6)。

既存録画の全銘柄について「N 秒以内に mid がその時点の spread × mult 以上動く」
イベントの発生率 (= taker が原理的に取れる機会の密度) を計測し、
50 銘柄枠の再設計候補ランキングを出す。

使い方:
  uv run python scripts/analyze_scalpability_universe.py            # 全日全銘柄
  uv run python scripts/analyze_scalpability_universe.py --horizon 30 --mult 1.5

出力: 標準出力にランキング表 + artifacts/scalpability_<params>.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scalp_agent import loader
from scalp_agent.features import mid, spread
from scalp_agent.labels import LabelSpec, make_labels


def analyze_symbol_day(day: str, code: str, spec: LabelSpec) -> dict | None:
    snap = loader.load_symbol_day(day, code)
    ts = snap["ts"]
    if len(ts) < 1000:  # 板が薄すぎる/登録が短時間だけの銘柄は評価不能
        return None
    m = mid(snap["bid_px_1"], snap["ask_px_1"])
    sp = spread(snap["bid_px_1"], snap["ask_px_1"])
    y, valid = make_labels(ts, m, sp, spec)
    n_valid = int(valid.sum())
    if n_valid == 0:
        return None
    hours = (ts[-1] - ts[0]) / 3600.0
    n_events = int((y != 0).sum())
    return {
        "day": day,
        "code": code,
        "snapshots": len(ts),
        "hours": round(hours, 2),
        "event_rate": n_events / n_valid,          # 有効スナップショット当たり発火率
        "events_per_hour": n_events / hours if hours > 0 else 0.0,
        "median_spread_bps": float(np.median(sp / m * 1e4)),
        "up_share": float((y == 1).sum() / max(n_events, 1)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=float, default=30.0, help="ホライズン 秒")
    ap.add_argument("--mult", type=float, default=1.5, help="閾値 = spread × mult")
    ap.add_argument("--days", nargs="*", default=None)
    args = ap.parse_args()

    spec = LabelSpec(horizon_s=args.horizon, threshold_spread_mult=args.mult)
    days = args.days or loader.available_days()
    results: list[dict] = []
    for day in days:
        try:
            codes = loader.list_codes(day)
        except Exception as e:  # 壊れ duckdb (wal 残骸等) はスキップ
            print(f"skip {day}: {e}")
            continue
        for code in codes:
            r = analyze_symbol_day(day, code, spec)
            if r:
                results.append(r)
        print(f"{day}: {len(codes)} 銘柄処理済み")

    # 銘柄集計 (日平均)
    by_code: dict[str, list[dict]] = {}
    for r in results:
        by_code.setdefault(r["code"], []).append(r)
    ranking = sorted(
        (
            {
                "code": c,
                "days": len(rs),
                "events_per_hour": round(float(np.mean([r["events_per_hour"] for r in rs])), 1),
                "event_rate": round(float(np.mean([r["event_rate"] for r in rs])), 4),
                "median_spread_bps": round(float(np.mean([r["median_spread_bps"] for r in rs])), 2),
            }
            for c, rs in by_code.items()
        ),
        key=lambda x: -x["events_per_hour"],
    )

    print(f"\n== scalpability ranking (horizon={args.horizon}s, mult={args.mult}) ==")
    print(f"{'code':>6} {'days':>4} {'ev/h':>8} {'rate':>8} {'spread_bps':>10}")
    for r in ranking:
        print(f"{r['code']:>6} {r['days']:>4} {r['events_per_hour']:>8} {r['event_rate']:>8} {r['median_spread_bps']:>10}")

    out_dir = Path(__file__).resolve().parents[1] / "artifacts"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"scalpability_h{args.horizon:g}_m{args.mult:g}.json"
    out.write_text(json.dumps({"spec": vars(args), "ranking": ranking, "per_day": results},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
