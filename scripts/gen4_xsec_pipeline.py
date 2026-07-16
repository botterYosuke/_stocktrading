"""足読み gen4 (横断ランキング) パイプライン。

usage:
  uv run python scripts/gen4_xsec_pipeline.py universe     # 日次パネル + 月次ユニバース
  uv run python scripts/gen4_xsec_pipeline.py build-cache  # train+val データセット
  uv run python scripts/gen4_xsec_pipeline.py sweep        # cheap gate (model × h × K)
  uv run python scripts/gen4_xsec_pipeline.py oos          # 凍結構成のみ 1 回 (sealed)

規約:
- OOS (2025-10-01〜2026-02-18) は sweep 完了・凍結まで一切読み込まない。
  oos は artifacts/gen4_xsec/oos_lock.json で再実行を拒否する。
- friction は呼値ラダー保守モデル。universe 時に gen2 の 17 銘柄板実測と照合し、
  モデルが実測を下回る銘柄があれば警告する。
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scalp_agent.execution import Trade  # noqa: E402
from scalp_agent.gates import trade_metrics  # noqa: E402
from scalp_agent_bars.xsec import daily as daily_mod  # noqa: E402
from scalp_agent_bars.xsec import dataset as ds  # noqa: E402
from scalp_agent_bars.xsec import evaluate as ev  # noqa: E402
from scalp_agent_bars.xsec import models as md  # noqa: E402
from scalp_agent_bars.xsec import universe as un  # noqa: E402
from scalp_agent_bars.xsec.config import (  # noqa: E402
    ART,
    CANDIDATE_MAX_CODE_SHARE,
    CANDIDATE_MAX_DAY_SHARE,
    CANDIDATE_MIN_D,
    CANDIDATE_MIN_DECILE_SPEARMAN,
    CANDIDATE_MIN_N,
    CANDIDATE_MIN_RATIO,
    HORIZON_MIN,
    MINUTE_COVERAGE_REQ,
    MODELS,
    NULL_SHUFFLES,
    OOS_RANGE,
    TOP_KS,
    TRAIN_RANGE,
    VAL_RANGE,
    config_hash,
)
from scalp_agent_bars.xsec.features import MODEL_FEATURE_NAMES  # noqa: E402
from scalp_agent_bars.xsec.friction import friction_bps  # noqa: E402

SWEEP_RESULTS_PATH = ART / "sweep_val_results.json"
FROZEN_PATH = ART / "frozen_config.json"
OOS_LOCK_PATH = ART / "oos_lock.json"
OOS_RESULT_PATH = ART / "oos_result.json"

# 日次パネルはユニバース窓 (train 初月の trailing 60 営業日) の分だけ手前から
PANEL_MIN = "2023-12-01"

COVERAGE_PATH = ART / "minute_coverage.json"


def _has_minute() -> set[str]:
    """分足カバレッジ走査 (2026-07-16) の凍結入力から、要求窓を満たす銘柄集合。"""
    if not COVERAGE_PATH.exists():
        raise SystemExit(
            f"{COVERAGE_PATH} がない。scan_minute_coverage (全銘柄 metadata 走査) を先に。")
    cov = json.loads(COVERAGE_PATH.read_text(encoding="utf-8"))
    return {
        c for c, v in cov.items()
        if "error" not in v
        and v["from"] <= MINUTE_COVERAGE_REQ[0]
        and v["to"] >= MINUTE_COVERAGE_REQ[1]
    }


def _progress(t0: float):
    def p(msg: str) -> None:
        print(f"  {msg} ({time.time() - t0:.0f}s)", flush=True)
    return p


def cmd_universe() -> None:
    ART.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    daily_mod.build_daily_panel(PANEL_MIN, VAL_RANGE[1], progress=_progress(t0))
    panel = daily_mod.load_daily_panel()
    sector_map, prime = daily_mod.load_sector_map()
    prime_norm = {c.upper() for c in prime} | set(prime)
    has_minute = _has_minute()
    print(f"minute coverage {MINUTE_COVERAGE_REQ}: {len(has_minute)} codes", flush=True)
    months = un.months_between(TRAIN_RANGE[0], VAL_RANGE[1])
    universe = un.build_monthly_universe(panel, months, prime_norm, has_minute)
    sizes = {m: len(cs) for m, cs in universe.items()}
    ds.UNIVERSE_PATH.write_text(json.dumps({
        "config_hash": config_hash(), "sizes": sizes, "universe": universe,
    }, ensure_ascii=False), encoding="utf-8")
    print(f"universe sizes: {sizes}", flush=True)

    # friction モデル照合 (gen2 の 17 銘柄板実測)
    gen2_fric = Path("artifacts/gen2_minute/friction_spread_bps.json")
    if gen2_fric.exists():
        measured = json.loads(gen2_fric.read_text(encoding="utf-8"))["median_spread_bps"]
        last_close = {}
        codes_ = panel["code"]
        for c in measured:
            m = codes_ == c
            if m.any():
                last_close[c] = float(panel["close"][m][-1])
        bad = []
        for c, meas in measured.items():
            if c not in last_close:
                continue
            model = float(friction_bps(last_close[c]))
            if model < meas:
                bad.append((c, meas, model))
            print(f"  friction check {c}: model={model:.2f} measured_spread={meas:.2f}",
                  flush=True)
        if bad:
            print(f"WARNING: モデル < 実測スプレッドの銘柄あり: {bad}", flush=True)


def cmd_build_cache() -> None:
    t0 = time.time()
    panel = daily_mod.load_daily_panel()
    sector_map, _ = daily_mod.load_sector_map()
    sector_map = {k.upper(): v for k, v in sector_map.items()}
    ds.build_dataset("isval", TRAIN_RANGE[0], VAL_RANGE[1], panel, sector_map,
                     progress=_progress(t0))


def _masks(data: dict[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    day = data["day"].astype(str)
    train = (day >= TRAIN_RANGE[0]) & (day <= TRAIN_RANGE[1])
    val = (day >= VAL_RANGE[0]) & (day <= VAL_RANGE[1])
    assert not (train & val).any()
    return train, val


def _features_matrix(data: dict[str, np.ndarray]) -> np.ndarray:
    return np.column_stack([data[f"z_{n}"] for n in MODEL_FEATURE_NAMES])


def _train_and_score(
    data: dict[str, np.ndarray], x: np.ndarray, h: int,
    train_mask: np.ndarray, model: str,
) -> tuple[object, np.ndarray]:
    """train 行で学習し、全行の score を返す。ラベル nan の train 行は捨てる。"""
    pct = data[f"h{h}_pct"]
    tm = train_mask & np.isfinite(pct)
    if model == "linear":
        coef = md.train_linear(x[tm], pct[tm])
        return coef, md.predict_linear(coef, x)
    # lambdarank はグループ連続性が要る — train 行は (day,tod) ソート済みの部分列
    sizes = ds.group_sizes(data["day"][tm], data["tod"][tm])
    booster = md.train_lgbm_rank(x[tm], pct[tm], sizes)
    return booster, md.predict_lgbm(booster, x)


def _grid_metrics(
    data: dict[str, np.ndarray], scores: np.ndarray, h: int, k: int,
    eval_mask: np.ndarray,
) -> dict:
    trades = ev.book_trades(data, scores, h, k, eval_mask)
    m = trade_metrics(trades)
    long_m = trade_metrics([t for t in trades if t.side == 1])
    short_m = trade_metrics([t for t in trades if t.side == -1])
    stress = trade_metrics(ev.book_trades(data, scores, h, k, eval_mask, stress=True))
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
        m["null"] = ev.null_gap(data, h, k, eval_mask, m["net_per_entry"], NULL_SHUFFLES)
    else:
        m["null"] = {"null_mean": None, "gap": None, "gap_z": None}
    return m


def is_candidate(m: dict, deciles: dict) -> bool:
    sp = deciles.get("spearman")
    nul = m.get("null", {})
    return (
        m["n"] >= CANDIDATE_MIN_N
        and m["D"] >= CANDIDATE_MIN_D
        and m["net_per_entry"] is not None and m["net_per_entry"] > 0
        and m["ratio"] is not None and m["ratio"] >= CANDIDATE_MIN_RATIO
        and (m["max_day_share"] is None or m["max_day_share"] <= CANDIDATE_MAX_DAY_SHARE)
        and (m["max_code_share"] is None or m["max_code_share"] <= CANDIDATE_MAX_CODE_SHARE)
        and sp is not None and sp >= CANDIDATE_MIN_DECILE_SPEARMAN
        and nul.get("gap_z") is not None and nul["gap_z"] >= 2.0
    )


def _fmt(v, nd=2):
    return "None" if v is None else round(v, nd)


def cmd_sweep() -> None:
    ART.mkdir(parents=True, exist_ok=True)
    print(f"gen4 config_hash = {config_hash()}", flush=True)
    data = ds.load_dataset("isval")
    train_mask, val_mask = _masks(data)
    x = _features_matrix(data)
    print(f"rows: total={len(x)} train={train_mask.sum()} val={val_mask.sum()}",
          flush=True)

    results: dict[str, dict] = {}
    candidates: list[tuple[float, str]] = []
    for model in MODELS:
        for h in HORIZON_MIN:
            _, scores = _train_and_score(data, x, h, train_mask, model)
            deciles = ev.decile_diagnostics(data, scores, h, val_mask)
            for k in TOP_KS:
                key = f"{model}|h{h}|k{k}"
                m = _grid_metrics(data, scores, h, k, val_mask)
                m["deciles"] = deciles
                results[key] = m
                cand = is_candidate(m, deciles)
                if cand and m["net_per_entry"] is not None:
                    candidates.append((m["net_per_entry"], key))
                print(
                    f"  {key}: n={m['n']} D={m['D']} net={_fmt(m['net_per_entry'])} "
                    f"gross={_fmt(m['gross_per_entry'])} ratio={_fmt(m['ratio'])} "
                    f"t={_fmt(m['t_net'])} spearman={_fmt(deciles.get('spearman'))} "
                    f"null_gap_z={_fmt(m['null'].get('gap_z'))} cand={cand}",
                    flush=True,
                )

    slim = {k: {kk: vv for kk, vv in m.items() if kk not in ("by_code", "by_day")}
            for k, m in results.items()}
    SWEEP_RESULTS_PATH.write_text(json.dumps({
        "config_hash": config_hash(), "results": slim,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    if not candidates:
        FROZEN_PATH.write_text(json.dumps({
            "verdict": "IS-KILL",
            "reason": (
                f"no (model, horizon, K) met n>={CANDIDATE_MIN_N}, D>={CANDIDATE_MIN_D}, "
                f"net>0, ratio>={CANDIDATE_MIN_RATIO}, shares<=0.3, "
                f"decile_spearman>={CANDIDATE_MIN_DECILE_SPEARMAN}, null_gap_z>=2 on val"
            ),
            "config_hash": config_hash(),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print("IS-KILL: no candidate. OOS stays sealed.", flush=True)
        return
    candidates.sort(reverse=True)
    best = candidates[0][1]
    FROZEN_PATH.write_text(json.dumps({
        "verdict": "FROZEN", "key": best,
        "val_metrics": {k: v for k, v in results[best].items()
                        if k not in ("by_code", "by_day")},
        "config_hash": config_hash(),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"FROZEN: {best} net/entry={results[best]['net_per_entry']:.2f}bps", flush=True)


def cmd_oos() -> None:
    if not FROZEN_PATH.exists():
        raise SystemExit("frozen_config.json がない。先に sweep。")
    frozen = json.loads(FROZEN_PATH.read_text(encoding="utf-8"))
    if frozen.get("verdict") != "FROZEN":
        raise SystemExit(f"凍結構成が無い (verdict={frozen.get('verdict')})。OOS は開けない。")
    if OOS_LOCK_PATH.exists():
        raise SystemExit("oos_lock.json が存在する。OOS は 1 回だけ。")
    if frozen["config_hash"] != config_hash():
        raise SystemExit("config_hash 不一致。凍結後に設定が変わっている。")
    model, hs, ks = frozen["key"].split("|")
    h, k = int(hs[1:]), int(ks[1:])

    # ここで初めて OOS に触れる。直後にロック。
    OOS_LOCK_PATH.write_text(json.dumps(
        {"opened_for": frozen["key"], "config_hash": config_hash()},
        ensure_ascii=False, indent=1), encoding="utf-8")

    t0 = time.time()
    daily_mod.build_daily_panel(PANEL_MIN, OOS_RANGE[1], progress=_progress(t0))
    panel = daily_mod.load_daily_panel()
    sector_map, prime = daily_mod.load_sector_map()
    sector_norm = {kk.upper(): vv for kk, vv in sector_map.items()}
    prime_norm = {c.upper() for c in prime} | set(prime)
    has_minute = _has_minute()
    months = un.months_between(OOS_RANGE[0], OOS_RANGE[1])
    oos_universe = un.build_monthly_universe(panel, months, prime_norm, has_minute)
    uni = json.loads(ds.UNIVERSE_PATH.read_text(encoding="utf-8"))
    uni["universe"] |= oos_universe
    ds.UNIVERSE_PATH.write_text(json.dumps(uni, ensure_ascii=False), encoding="utf-8")
    ds.build_dataset("oos", *OOS_RANGE, panel, sector_norm, progress=_progress(t0))

    isval = ds.load_dataset("isval")
    x_isval = _features_matrix(isval)
    all_mask = np.ones(len(x_isval), dtype=bool)
    fitted, _ = _train_and_score(isval, x_isval, h, all_mask, model)

    oos = ds.load_dataset("oos")
    x_oos = _features_matrix(oos)
    scores = (md.predict_linear(fitted, x_oos) if model == "linear"
              else md.predict_lgbm(fitted, x_oos))
    oos_mask = np.ones(len(x_oos), dtype=bool)
    m = _grid_metrics(oos, scores, h, k, oos_mask)
    m["deciles"] = ev.decile_diagnostics(oos, scores, h, oos_mask)
    trades = ev.book_trades(oos, scores, h, k, oos_mask)
    OOS_RESULT_PATH.write_text(json.dumps({
        "verdict_note": "正式判定は ADR-0001 G1-G8 の採点で行う。",
        "frozen_key": frozen["key"], "oos_range": list(OOS_RANGE),
        "metrics": {k_: v for k_, v in m.items() if k_ not in ("by_code", "by_day")},
        "config_hash": config_hash(),
        "trades": [asdict(t) for t in trades],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k_: v for k_, v in m.items()
                      if k_ not in ("by_code", "by_day", "deciles", "null")},
                     ensure_ascii=False, indent=1), flush=True)
    print(f"OOS done → {OOS_RESULT_PATH}", flush=True)


def main() -> None:
    cmds = {"universe": cmd_universe, "build-cache": cmd_build_cache,
            "sweep": cmd_sweep, "oos": cmd_oos}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        raise SystemExit(f"usage: gen4_xsec_pipeline.py {{{'|'.join(cmds)}}}")
    cmds[sys.argv[1]]()


if __name__ == "__main__":
    main()
