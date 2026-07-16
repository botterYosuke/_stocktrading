"""gen1b (板非依存・分足/日足の兄弟 family) パイプライン。

規約は gen1 と同一プロトコル:
- sweep は 07-09 学習 → 07-13 で 120 セル評価 → 候補 (n≥100, net>0, ratio≥3)
  から net/entry 最大の 1 セルを凍結。候補ゼロなら 07-14 を開けず IS-KILL。
- oos は凍結セルが存在するときのみ 1 回だけ。artifacts/gen1b/oos_lock.json で拒否。
- gen1b は新 family — 判定は台帳へ honest-N +1 で記録する。

usage:
  uv run python scripts/gen1b_pipeline.py build-cache
  uv run python scripts/gen1b_pipeline.py sweep
  uv run python scripts/gen1b_pipeline.py oos
  uv run python scripts/gen1b_pipeline.py post-mortem
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import lightgbm as lgb
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scalp_agent import loader  # noqa: E402
from scalp_agent_bars.config import (  # noqa: E402
    CANDIDATE_MIN_N,
    CANDIDATE_MIN_RATIO,
    HORIZONS_S,
    IS_TRAIN_DAYS,
    IS_VAL_DAYS,
    LGBM_NUM_BOOST_ROUND,
    LGBM_PARAMS,
    MULTS,
    OOS_DAYS,
    TAUS,
    assert_days_role,
    assert_no_day_leakage,
    cell_key,
    config_hash,
)
from scalp_agent_bars.dataset import (  # noqa: E402
    ensure_cache,
    features_from_table,
    training_arrays,
)
from scalp_agent_bars.features import feature_schema_hash  # noqa: E402
from scalp_agent.dataset import side_fields_from_table  # noqa: E402
from scalp_agent.execution import Trade, max_concurrency, simulate_symbol_day  # noqa: E402
from scalp_agent.gates import select_frozen_cell, trade_metrics  # noqa: E402
from scalp_agent.nulls import side_shuffle_null, time_shuffle_null  # noqa: E402

ART = Path("artifacts/gen1b")
FROZEN_PATH = ART / "frozen_cell.json"
OOS_LOCK_PATH = ART / "oos_lock.json"
SWEEP_RESULTS_PATH = ART / "sweep_val_results.json"
OOS_RESULT_PATH = ART / "oos_result.json"
POST_MORTEM_PATH = ART / "oos_post_mortem_grid.json"


def _load_day_tables(day: str) -> dict[str, dict[str, np.ndarray]]:
    codes = loader.list_codes(day)
    tables = {}
    t0 = time.time()
    for i, code in enumerate(codes, 1):
        tables[code] = ensure_cache(day, code)
        print(f"  [{day}] {i}/{len(codes)} {code} "
              f"({len(tables[code]['b_ts'])} decisions, {time.time()-t0:.0f}s)",
              flush=True)
    return tables


def _train_booster(
    tables: dict[tuple[str, str], dict[str, np.ndarray]],
    horizon_s: float,
    mult: float,
) -> lgb.Booster | None:
    x, y = training_arrays(tables, horizon_s, mult)
    if len(y) < 1000 or len(np.unique(y)) < 3:
        print(f"  cell {cell_key(horizon_s, mult)}: degenerate training set "
              f"(n={len(y)}, classes={np.unique(y).tolist()}) — skip", flush=True)
        return None
    ds = lgb.Dataset(x, label=y, params={"max_bin": LGBM_PARAMS["max_bin"]})
    return lgb.train(LGBM_PARAMS, ds, num_boost_round=LGBM_NUM_BOOST_ROUND)


def _simulate_cells(
    booster: lgb.Booster,
    day: str,
    tables: dict[str, dict[str, np.ndarray]],
    horizon_s: float,
    mult: float,
    taus: tuple[float, ...],
) -> dict[float, list[Trade]]:
    ck = cell_key(horizon_s, mult)
    per_tau: dict[float, list[Trade]] = {t: [] for t in taus}
    for code, table in tables.items():
        if len(table["b_ts"]) == 0:
            continue
        scores = booster.predict(features_from_table(table))
        lf = side_fields_from_table(table, ck, "L")
        sf = side_fields_from_table(table, ck, "S")
        for tau in taus:
            per_tau[tau].extend(
                simulate_symbol_day(code, day, table["b_ts"], scores, lf, sf, tau)
            )
    return per_tau


def cmd_build_cache() -> None:
    """train/val のみ。OOS 日 (07-14) はロック前に触れない (gen1 と同一規律)。"""
    assert_no_day_leakage()
    for day in [*IS_TRAIN_DAYS, *IS_VAL_DAYS]:
        print(f"building bars cache for {day}", flush=True)
        _load_day_tables(day)
    print("cache build done (train/val only; OOS day stays sealed)", flush=True)


def cmd_sweep() -> None:
    assert_no_day_leakage()
    assert_days_role(IS_TRAIN_DAYS, "train")
    assert_days_role(IS_VAL_DAYS, "val")
    ART.mkdir(parents=True, exist_ok=True)
    print(f"gen1b config_hash = {config_hash()}", flush=True)

    train_tables = {}
    for day in IS_TRAIN_DAYS:
        for code, t in _load_day_tables(day).items():
            train_tables[(day, code)] = t
    val_tables_by_day = {day: _load_day_tables(day) for day in IS_VAL_DAYS}

    results: dict[tuple[float, float, float], dict] = {}
    for h in HORIZONS_S:
        for m in MULTS:
            booster = _train_booster(train_tables, h, m)
            if booster is None:
                for tau in TAUS:
                    results[(h, m, tau)] = trade_metrics([])
                continue
            for day, vt in val_tables_by_day.items():
                per_tau = _simulate_cells(booster, day, vt, h, m, TAUS)
                for tau, trades in per_tau.items():
                    results[(h, m, tau)] = trade_metrics(trades)
                    r = results[(h, m, tau)]
                    print(f"  {cell_key(h, m)} tau={tau}: n={r['n']} "
                          f"net={r['net_per_entry']} ratio={r['ratio']}", flush=True)

    serial = {f"{cell_key(h, m)}_t{int(tau*100)}": m_
              for (h, m, tau), m_ in results.items()}
    SWEEP_RESULTS_PATH.write_text(
        json.dumps({"config_hash": config_hash(),
                    "feature_schema_hash": feature_schema_hash(),
                    "results": serial}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    chosen = select_frozen_cell(results, CANDIDATE_MIN_N, CANDIDATE_MIN_RATIO)
    if chosen is None:
        FROZEN_PATH.write_text(json.dumps({
            "verdict": "IS-KILL",
            "reason": f"no cell met n>={CANDIDATE_MIN_N}, net>0, ratio>={CANDIDATE_MIN_RATIO} on IS-val",
            "config_hash": config_hash(),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print("IS-KILL: no candidate cell. 07-14 stays sealed.", flush=True)
        return
    h, m, tau = chosen
    FROZEN_PATH.write_text(json.dumps({
        "verdict": "FROZEN",
        "horizon_s": h, "mult": m, "tau": tau,
        "cell_key": cell_key(h, m),
        "is_val_metrics": results[chosen],
        "config_hash": config_hash(),
        "feature_schema_hash": feature_schema_hash(),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"FROZEN: {cell_key(h, m)} tau={tau} "
          f"net/entry={results[chosen]['net_per_entry']:.2f}bps "
          f"n={results[chosen]['n']} ratio={results[chosen]['ratio']:.2f}", flush=True)


def cmd_oos() -> None:
    assert_no_day_leakage()
    if not FROZEN_PATH.exists():
        raise SystemExit("frozen_cell.json がない。先に sweep を実行する。")
    frozen = json.loads(FROZEN_PATH.read_text(encoding="utf-8"))
    if frozen.get("verdict") != "FROZEN":
        raise SystemExit(f"凍結セルが無い (verdict={frozen.get('verdict')})。OOS は開けない。")
    if OOS_LOCK_PATH.exists():
        raise SystemExit("oos_lock.json が存在する。OOS は 1 回だけ。再実行は新 family。")
    if frozen["config_hash"] != config_hash():
        raise SystemExit("config_hash 不一致。凍結後に設定が変わっている。")
    h, m, tau = frozen["horizon_s"], frozen["mult"], frozen["tau"]
    ck = cell_key(h, m)

    refit_tables = {}
    for day in [*IS_TRAIN_DAYS, *IS_VAL_DAYS]:
        for code, t in _load_day_tables(day).items():
            refit_tables[(day, code)] = t
    booster = _train_booster(refit_tables, h, m)
    if booster is None:
        raise SystemExit("再学習が退化 — OOS を開けずに終了。")

    # ここで初めて OOS 日に触れる。直後にロックを置く。
    OOS_LOCK_PATH.write_text(json.dumps(
        {"opened_for": ck, "tau": tau, "config_hash": config_hash()},
        ensure_ascii=False, indent=1), encoding="utf-8")

    assert_days_role(OOS_DAYS, "oos")
    day = OOS_DAYS[0]
    oos_tables = _load_day_tables(day)
    trades = _simulate_cells(booster, day, oos_tables, h, m, (tau,))[tau]
    metrics = trade_metrics(trades)
    nulls = {
        "time_shuffle": time_shuffle_null(trades, oos_tables, ck),
        "side_shuffle": side_shuffle_null(trades, oos_tables, ck),
    }
    result = {
        "verdict": "EVALUATION-INCOMPLETE",
        "verdict_note": "3営業日構成のため G4 (n>=30, D>=20) を満たせない。"
                        "PASS/エッジ確認とは呼ばない。判定は D>=20 到達後。",
        "frozen": {"horizon_s": h, "mult": m, "tau": tau, "cell_key": ck},
        "oos_day": day,
        "metrics": metrics,
        "max_concurrency": max_concurrency(trades),
        "nulls": nulls,
        "config_hash": config_hash(),
        "trades": [asdict(t) for t in trades],
    }
    OOS_RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                               encoding="utf-8")
    print(json.dumps({k: v for k, v in metrics.items() if k not in ("by_code", "by_day")},
                     ensure_ascii=False, indent=1), flush=True)
    print(f"OOS done → {OOS_RESULT_PATH}", flush=True)


def cmd_post_mortem() -> None:
    if not OOS_LOCK_PATH.exists():
        raise SystemExit("OOS 未実施。post-mortem は凍結セルの結果確定後のみ。")
    refit_tables = {}
    for day in [*IS_TRAIN_DAYS, *IS_VAL_DAYS]:
        for code, t in _load_day_tables(day).items():
            refit_tables[(day, code)] = t
    day = OOS_DAYS[0]
    oos_tables = _load_day_tables(day)
    grid: dict[str, dict] = {}
    for h in HORIZONS_S:
        for m in MULTS:
            booster = _train_booster(refit_tables, h, m)
            if booster is None:
                continue
            per_tau = _simulate_cells(booster, day, oos_tables, h, m, TAUS)
            for tau, trades in per_tau.items():
                grid[f"{cell_key(h, m)}_t{int(tau*100)}"] = trade_metrics(trades)
    POST_MORTEM_PATH.write_text(json.dumps({
        "note": "diagnostic only — ここからの再選択は新 family かつ新 sealed データが必要",
        "grid": grid,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"post-mortem grid → {POST_MORTEM_PATH}", flush=True)


def main() -> None:
    cmds = {"build-cache": cmd_build_cache, "sweep": cmd_sweep,
            "oos": cmd_oos, "post-mortem": cmd_post_mortem}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        raise SystemExit(f"usage: gen1b_pipeline.py {{{'|'.join(cmds)}}}")
    cmds[sys.argv[1]]()


if __name__ == "__main__":
    main()
