"""足読み gen5 (同時刻 lag 横断ランキング) パイプライン。

usage:
  uv run python scripts/gen5_tod_lag_pipeline.py sweep

入力は gen4 の凍結キャッシュ artifacts/gen4_xsec/dataset_isval.parquet のみ
(新しい分足走査なし)。signal の warmup (lag 40) のため pivot はデータセット全行
(train+val) で行い、取引・評価は val 行のみ。

規約:
- OOS (2025-04-01..30) は gen4 から sealed を継承 — 本スクリプトは触れない。
  val 候補が出た場合のみ、凍結構成に対する oos ステップを別途実装する。
- 格子は {hl5, hl20} × {K5, K10}・horizon 30 分のみ (tod_lag.config_hash で凍結)。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scalp_agent.gates import trade_metrics  # noqa: E402
from scalp_agent_bars.xsec import dataset as ds  # noqa: E402
from scalp_agent_bars.xsec import evaluate as ev  # noqa: E402
from scalp_agent_bars.xsec import tod_lag as tl  # noqa: E402
from scalp_agent_bars.xsec.config import (  # noqa: E402
    NULL_SHUFFLES,
    TRAIN_RANGE,
    VAL_RANGE,
)

SWEEP_RESULTS_PATH = tl.ART / "sweep_val_results.json"
FROZEN_PATH = tl.ART / "frozen_config.json"

H = tl.HORIZON


def _fmt(v, nd=2):
    return "None" if v is None else round(v, nd)


def _grid_metrics(data: dict[str, np.ndarray], scores: np.ndarray, k: int,
                  mask: np.ndarray) -> dict:
    """gen4 sweep と同じ採点一式 (horizon は 30 分固定)。"""
    trades = ev.book_trades(data, scores, H, k, mask)
    m = trade_metrics(trades)
    long_m = trade_metrics([t for t in trades if t.side == 1])
    short_m = trade_metrics([t for t in trades if t.side == -1])
    stress = trade_metrics(ev.book_trades(data, scores, H, k, mask, stress=True))
    by_month: dict[str, float] = {}
    for t in trades:
        by_month[t.day[:7]] = by_month.get(t.day[:7], 0.0) + t.net_bps
    total = sum(by_month.values())
    m["max_month_share"] = (
        max(abs(v) for v in by_month.values()) / abs(total) if total else None)
    m["long_net_per_entry"] = long_m["net_per_entry"]
    m["short_net_per_entry"] = short_m["net_per_entry"]
    m["stress_net_per_entry"] = stress["net_per_entry"]
    if m["net_per_entry"] is not None:
        m["null"] = ev.null_gap(data, H, k, mask, m["net_per_entry"], NULL_SHUFFLES)
    else:
        m["null"] = {"null_mean": None, "gap": None, "gap_z": None}
    return m


def cmd_sweep() -> None:
    tl.ART.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    print(f"gen5 config_hash = {tl.config_hash()}", flush=True)

    data = ds.load_dataset("isval")
    day = data["day"].astype(str)
    train_mask = (day >= TRAIN_RANGE[0]) & (day <= TRAIN_RANGE[1])
    val_mask = (day >= VAL_RANGE[0]) & (day <= VAL_RANGE[1])
    assert not (train_mask & val_mask).any()

    # pivot は全行 (warmup)、評価は val 行のみのスライスで行う
    mat, code_idx, tod_idx, day_idx, n_tod = tl.pivot_series(
        data["code"], data["tod"], data["day"], data[f"h{H}_adj_bps"])
    val_data = {k: v[val_mask] for k, v in data.items()}
    ones = np.ones(int(val_mask.sum()), dtype=bool)
    print(f"rows: total={len(day)} train={train_mask.sum()} val={val_mask.sum()} "
          f"series={mat.shape[0]} days={mat.shape[1]} ({time.time() - t0:.0f}s)",
          flush=True)

    perms = tl.all_tod_permutations(n_tod)
    results: dict[str, dict] = {}
    candidates: list[tuple[float, str]] = []
    for hl in tl.HALF_LIVES:
        sig = tl.ewma_lag_signal(mat, hl)
        scores = tl.perm_scores(
            sig, code_idx, tod_idx, day_idx, n_tod, tl.identity_perm(n_tod))[val_mask]
        deciles = ev.decile_diagnostics(val_data, scores, H, ones)

        # 対照の trade 列は (perm, K) ごと。scores gather は perm ごとに 1 回。
        rot_metrics: dict[int, list[dict]] = {k: [] for k in tl.TOP_KS}
        for shift in range(1, n_tod):
            sc = tl.perm_scores(sig, code_idx, tod_idx, day_idx, n_tod,
                                tl.rotation_perm(n_tod, shift))[val_mask]
            for k in tl.TOP_KS:
                mm = trade_metrics(ev.book_trades(val_data, sc, H, k, ones))
                rot_metrics[k].append({
                    "shift": shift, "n": mm["n"],
                    "net_per_entry": mm["net_per_entry"],
                    "gross_per_entry": mm["gross_per_entry"],
                })
        perm_nets: dict[int, list[float]] = {k: [] for k in tl.TOP_KS}
        perm_grosses: dict[int, list[float]] = {k: [] for k in tl.TOP_KS}
        for pi, perm in enumerate(perms):
            sc = tl.perm_scores(sig, code_idx, tod_idx, day_idx, n_tod,
                                np.asarray(perm))[val_mask]
            for k in tl.TOP_KS:
                mm = trade_metrics(ev.book_trades(val_data, sc, H, k, ones))
                if mm["net_per_entry"] is not None:
                    perm_nets[k].append(mm["net_per_entry"])
                    perm_grosses[k].append(mm["gross_per_entry"])
            if (pi + 1) % 20 == 0:
                print(f"  hl{hl:g}: perm {pi + 1}/{len(perms)} "
                      f"({time.time() - t0:.0f}s)", flush=True)

        for k in tl.TOP_KS:
            key = f"hl{hl:g}|k{k}"
            m = _grid_metrics(val_data, scores, k, ones)
            m["deciles"] = deciles
            rg = [r["gross_per_entry"] for r in rot_metrics[k]
                  if r["gross_per_entry"] is not None]
            pn = perm_nets[k]
            controls = {
                "rotation": rot_metrics[k],
                "rotation_gross_mean": float(np.mean(rg)) if rg else None,
                "rotation_net_mean": float(np.mean(
                    [r["net_per_entry"] for r in rot_metrics[k]
                     if r["net_per_entry"] is not None])) if rg else None,
                "perm_n": len(pn),
                "perm_net_mean": float(np.mean(pn)) if pn else None,
                "perm_net_std": float(np.std(pn, ddof=1)) if len(pn) > 1 else None,
                "perm_gross_mean": (float(np.mean(perm_grosses[k]))
                                    if perm_grosses[k] else None),
                "perm_p_net": (
                    (1 + sum(1 for v in pn if v >= m["net_per_entry"]))
                    / (1 + len(pn))
                    if pn and m["net_per_entry"] is not None else None),
            }
            m["controls"] = controls
            results[key] = m
            cand = tl.is_candidate(m, deciles, controls)
            if cand and m["net_per_entry"] is not None:
                candidates.append((m["net_per_entry"], key))
            print(
                f"  {key}: n={m['n']} D={m['D']} net={_fmt(m['net_per_entry'])} "
                f"gross={_fmt(m['gross_per_entry'])} ratio={_fmt(m['ratio'])} "
                f"t={_fmt(m['t_net'])} spearman={_fmt(deciles.get('spearman'))} "
                f"null_gap_z={_fmt(m['null'].get('gap_z'))} "
                f"rot_gross={_fmt(controls['rotation_gross_mean'])} "
                f"perm_p={_fmt(controls['perm_p_net'], 4)} cand={cand}",
                flush=True,
            )

    slim = {k: {kk: vv for kk, vv in m.items() if kk not in ("by_code", "by_day")}
            for k, m in results.items()}
    SWEEP_RESULTS_PATH.write_text(json.dumps({
        "config_hash": tl.config_hash(), "results": slim,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"results → {SWEEP_RESULTS_PATH} ({time.time() - t0:.0f}s)", flush=True)

    if not candidates:
        FROZEN_PATH.write_text(json.dumps({
            "verdict": "IS-KILL",
            "reason": (
                "no (half_life, K) met gen4 candidate conditions + gen5 controls "
                "(gross > rotation mean, perm_p_net < 0.05) on val"
            ),
            "config_hash": tl.config_hash(),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print("IS-KILL: no candidate. OOS stays sealed.", flush=True)
        return
    candidates.sort(reverse=True)
    best = candidates[0][1]
    FROZEN_PATH.write_text(json.dumps({
        "verdict": "FROZEN", "key": best,
        "val_metrics": {k: v for k, v in results[best].items()
                        if k not in ("by_code", "by_day")},
        "config_hash": tl.config_hash(),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"FROZEN: {best} net/entry={results[best]['net_per_entry']:.2f}bps",
          flush=True)


def main() -> None:
    cmds = {"sweep": cmd_sweep}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        raise SystemExit(f"usage: gen5_tod_lag_pipeline.py {{{'|'.join(cmds)}}}")
    cmds[sys.argv[1]]()


if __name__ == "__main__":
    main()
