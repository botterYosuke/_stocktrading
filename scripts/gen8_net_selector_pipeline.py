"""足読み gen8 (cost-aware complete-transaction selector) パイプライン。

usage:
  uv run python scripts/gen8_net_selector_pipeline.py sweep   # train→val 凍結 (+ヌル)
  uv run python scripts/gen8_net_selector_pipeline.py oos     # 凍結構成のみ 1 回

規約 (事前凍結 docs/analysis/gen8-net-selector-preregistration-2026-07-17.md):
- OOS (2025-10-01〜2026-02-18) は凍結完了まで一切読み込まない。
  oos は artifacts/gen8_net_selector/oos_lock.json で再実行を拒否する。
- データ・特徴・friction・執行は gen2 と同一。gen2 の isval キャッシュを再利用する。
- 2025-04 (gen4/5/6 sealed OOS) と 2025-07〜09 (gen2/3 val) では選定メトリクスを
  計算しない。VAL = 2025-05-01〜06-30 のみ。
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

from scalp_agent.config import LGBM_NUM_BOOST_ROUND  # noqa: E402
from scalp_agent.execution import Trade, max_concurrency  # noqa: E402
from scalp_agent.gates import trade_metrics  # noqa: E402
from scalp_agent.nulls import side_shuffle_null, time_shuffle_null  # noqa: E402
from scalp_agent_bars.minute import dataset as ds  # noqa: E402
from scalp_agent_bars.minute.config import (  # noqa: E402
    ATR_MULTS,
    HORIZON_BARS,
    OOS_RANGE as GEN2_OOS_RANGE,
    TRAIN_RANGE as GEN2_TRAIN_RANGE,
    UNIVERSE,
    VAL_RANGE as GEN2_VAL_RANGE,
    cell_key,
)
from scalp_agent_bars.minute.features import ALL_FEATURE_NAMES, feature_schema_hash  # noqa: E402
from scalp_agent_bars.minute.selector import (  # noqa: E402
    select_entries,
    side_fields_of,
    side_net_labels,
    simulate_symbol_day_selector,
)
from scalp_agent_bars.minute.selector_config import (  # noqa: E402
    ALPHAS,
    CANDIDATE_MAX_CODE_SHARE,
    CANDIDATE_MIN_D,
    CANDIDATE_MIN_N,
    CANDIDATE_MIN_RATIO,
    FINAL_FIT_RANGE,
    LGBM_MEAN_PARAMS,
    OOS_RANGE,
    TRAIN_RANGE,
    VAL_RANGE,
    config_hash,
    lgbm_quantile_params,
)

ART = Path("artifacts/gen8_net_selector")
SWEEP_RESULTS_PATH = ART / "sweep_val_results.json"
FROZEN_PATH = ART / "frozen_config.json"
NULLS_PATH = ART / "val_nulls.json"
OOS_LOCK_PATH = ART / "oos_lock.json"
OOS_RESULT_PATH = ART / "oos_result.json"

ISVAL_SCOPE = "isval"
OOS_SCOPE = "oos"

MIN_TRAIN_ROWS = 1000
TIME_NULL_P_UPPER_MAX = 0.05


# ---- gen2 キャッシュの再利用 -------------------------------------------------------

def _load_calibration() -> tuple[dict[str, str], dict[str, float]]:
    peer_path = Path("artifacts/gen2_minute/peer_map.json")
    peer = json.loads(peer_path.read_text(encoding="utf-8"))["peer"]
    return peer, ds.load_friction()


def _ensure_cache(scope: str, day_min: str, day_max: str) -> dict[str, dict[str, np.ndarray]]:
    """gen2 と同一 config のキャッシュ。無ければ gen2 の較正値から組む。"""
    if all(ds.is_cache_valid(c, scope, day_min, day_max) for c in UNIVERSE):
        return {c: ds.load_cache(c, scope) for c in UNIVERSE}
    peer, friction = _load_calibration()
    t0 = time.time()
    tables = ds.build_universe_tables(
        day_min, day_max, peer, friction,
        progress=lambda m: print(f"  [{scope}] {m} ({time.time()-t0:.0f}s)", flush=True),
    )
    for c, t in tables.items():
        ds.write_cache(c, scope, day_min, day_max, t)
    return tables


# ---- 学習・推論 -------------------------------------------------------------------

def _train_regressor(x: np.ndarray, y: np.ndarray, params: dict) -> lgb.Booster | None:
    if len(y) < MIN_TRAIN_ROWS:
        return None
    dset = lgb.Dataset(x, label=y, params={"max_bin": params["max_bin"]})
    return lgb.train(params, dset, num_boost_round=LGBM_NUM_BOOST_ROUND)


def _side_xy(table: dict[str, np.ndarray], ck: str, sp: str,
             mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    net, valid = side_net_labels(table, ck, sp)
    keep = valid & mask
    x = ds.features_matrix(table, ALL_FEATURE_NAMES)[keep]
    return x, net[keep]


def _train_cell_models(
    tables: dict[str, dict[str, np.ndarray]],
    train_masks: dict[str, np.ndarray],
    ck: str,
) -> dict | None:
    """セル 1 つ分のモデル束: {sp: {"mean": Booster, "q": {alpha: Booster}}}。"""
    out: dict = {}
    for sp in ("L", "S"):
        xs, ys = [], []
        for c, table in tables.items():
            x, y = _side_xy(table, ck, sp, train_masks[c])
            xs.append(x)
            ys.append(y)
        x_all = np.concatenate(xs)
        y_all = np.concatenate(ys)
        mean_b = _train_regressor(x_all, y_all, LGBM_MEAN_PARAMS)
        if mean_b is None:
            return None
        q_bs = {}
        for a in ALPHAS:
            qb = _train_regressor(x_all, y_all, lgbm_quantile_params(a))
            if qb is None:
                return None
            q_bs[a] = qb
        out[sp] = {"mean": mean_b, "q": q_bs}
    return out


def _simulate_config(
    tables: dict[str, dict[str, np.ndarray]],
    eval_masks: dict[str, np.ndarray],
    models: dict,
    ck: str,
    alpha: float,
) -> tuple[list[Trade], int, int]:
    """凍結売買規則で 1 構成をシミュレート。(trades, fired_rows, total_rows)。"""
    trades: list[Trade] = []
    fired = 0
    total = 0
    for c, table in tables.items():
        m = eval_masks[c]
        if not m.any():
            continue
        x = ds.features_matrix(table, ALL_FEATURE_NAMES)[m]
        sides = select_entries(
            models["L"]["q"][alpha].predict(x), models["L"]["mean"].predict(x),
            models["S"]["q"][alpha].predict(x), models["S"]["mean"].predict(x),
        )
        total += len(sides)
        fired += int((sides != 0).sum())
        days = np.asarray(table["day"])[m]
        b_ts = np.asarray(table["b_ts"], dtype=np.float64)[m]
        lf = side_fields_of(table, ck, "L", m)
        sf = side_fields_of(table, ck, "S", m)
        for day in np.unique(days):
            dm = days == day
            trades.extend(simulate_symbol_day_selector(
                c, str(day), b_ts[dm], sides[dm],
                {f: v[dm] for f, v in lf.items()},
                {f: v[dm] for f, v in sf.items()},
            ))
    return trades, fired, total


def is_candidate(m: dict) -> bool:
    return (
        m["n"] >= CANDIDATE_MIN_N
        and m["D"] >= CANDIDATE_MIN_D
        and m["net_per_entry"] is not None and m["net_per_entry"] > 0
        and m["ratio"] is not None and m["ratio"] >= CANDIDATE_MIN_RATIO
        and (m["max_code_share"] is None or m["max_code_share"] <= CANDIDATE_MAX_CODE_SHARE)
    )


def _select(results: dict[tuple, dict]) -> tuple | None:
    cands = [(k, m) for k, m in results.items() if is_candidate(m)]
    if not cands:
        return None

    def sort_key(item):
        (h, mu, alpha), m = item
        return (-m["net_per_entry"], -m["n"], h, mu, alpha)

    cands.sort(key=sort_key)
    return cands[0][0]


def _sliced_tables(
    tables: dict[str, dict[str, np.ndarray]], masks: dict[str, np.ndarray]
) -> dict[str, dict[str, np.ndarray]]:
    return {
        c: {k: np.asarray(v)[masks[c]] for k, v in t.items()}
        for c, t in tables.items() if masks[c].any()
    }


# ---- sweep -----------------------------------------------------------------------

def _write_is_kill(reason: str) -> None:
    FROZEN_PATH.write_text(json.dumps({
        "verdict": "IS-KILL",
        "reason": reason,
        "config_hash": config_hash(),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"IS-KILL: {reason}. OOS stays sealed.", flush=True)


def cmd_sweep() -> None:
    ART.mkdir(parents=True, exist_ok=True)
    print(f"gen8 config_hash = {config_hash()}", flush=True)
    assert TRAIN_RANGE[0] == GEN2_TRAIN_RANGE[0] and VAL_RANGE[1] <= GEN2_VAL_RANGE[1]
    assert OOS_RANGE == GEN2_OOS_RANGE
    tables = _ensure_cache(ISVAL_SCOPE, GEN2_TRAIN_RANGE[0], GEN2_VAL_RANGE[1])
    train_masks = {c: ds.day_mask(t, *TRAIN_RANGE) for c, t in tables.items()}
    val_masks = {c: ds.day_mask(t, *VAL_RANGE) for c, t in tables.items()}
    for c in tables:
        assert not (train_masks[c] & val_masks[c]).any()

    results: dict[tuple, dict] = {}
    reject: dict[tuple, dict] = {}
    for h in HORIZON_BARS:
        for mu in ATR_MULTS:
            ck = cell_key(h, mu)
            print(f"== cell {ck} ==", flush=True)
            models = _train_cell_models(tables, train_masks, ck)
            if models is None:
                for a in ALPHAS:
                    results[(h, mu, a)] = trade_metrics([])
                continue
            for a in ALPHAS:
                trades, fired, total = _simulate_config(tables, val_masks, models, ck, a)
                mtr = trade_metrics(trades)
                results[(h, mu, a)] = mtr
                reject[(h, mu, a)] = {"fired_rows": fired, "total_rows": total}
                print(f"  {ck} a={a}: fired={fired}/{total} n={mtr['n']} D={mtr['D']} "
                      f"net={None if mtr['net_per_entry'] is None else round(mtr['net_per_entry'], 3)} "
                      f"ratio={None if mtr['ratio'] is None else round(mtr['ratio'], 3)} "
                      f"t={None if mtr['t_net'] is None else round(mtr['t_net'], 2)}", flush=True)

    serial = {
        f"{cell_key(h, mu)}|a{int(a*100):02d}": {**m, "rejection": reject.get((h, mu, a))}
        for (h, mu, a), m in results.items()
    }
    SWEEP_RESULTS_PATH.write_text(json.dumps({
        "config_hash": config_hash(),
        "feature_schema_hash": feature_schema_hash(),
        "results": serial,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    chosen = _select(results)
    if chosen is None:
        _write_is_kill(
            f"no (cell, alpha) met n>={CANDIDATE_MIN_N}, D>={CANDIDATE_MIN_D}, net>0, "
            f"ratio>={CANDIDATE_MIN_RATIO}, code_share<={CANDIDATE_MAX_CODE_SHARE} on val"
        )
        return

    # 凍結候補にヌル検定 (事前凍結 §2: 時刻ヌル p_upper <= 0.05 が OOS 開封の前提)
    h, mu, alpha = chosen
    ck = cell_key(h, mu)
    models = _train_cell_models(tables, train_masks, ck)
    trades, fired, total = _simulate_config(tables, val_masks, models, ck, alpha)
    val_tables = _sliced_tables(tables, val_masks)
    t_null = time_shuffle_null(trades, val_tables, ck)
    s_null = side_shuffle_null(trades, val_tables, ck)
    NULLS_PATH.write_text(json.dumps({
        "config": {"cell_key": ck, "alpha": alpha},
        "time_shuffle": t_null,
        "side_shuffle": s_null,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    p_upper = t_null.get("p_upper")
    print(f"nulls: time p_upper={p_upper} gap={t_null.get('gap')} / "
          f"side p_upper={s_null.get('p_upper')}", flush=True)
    if p_upper is None or p_upper > TIME_NULL_P_UPPER_MAX:
        _write_is_kill(
            f"candidate {ck} a={alpha} failed time-shuffle null "
            f"(p_upper={p_upper} > {TIME_NULL_P_UPPER_MAX})"
        )
        return

    m = results[chosen]
    FROZEN_PATH.write_text(json.dumps({
        "verdict": "FROZEN",
        "horizon_bars": h, "atr_mult": mu, "alpha": alpha,
        "cell_key": ck,
        "val_metrics": m,
        "val_rejection": reject[chosen],
        "val_nulls": {"time_p_upper": p_upper, "side_p_upper": s_null.get("p_upper")},
        "config_hash": config_hash(),
        "feature_schema_hash": feature_schema_hash(),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"FROZEN: {ck} alpha={alpha} net/entry={m['net_per_entry']:.2f}bps "
          f"n={m['n']} D={m['D']} ratio={m['ratio']:.2f}", flush=True)


# ---- oos -------------------------------------------------------------------------

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
    h, mu, alpha = frozen["horizon_bars"], frozen["atr_mult"], frozen["alpha"]
    ck = cell_key(h, mu)

    # FINAL_FIT (2024-01-04〜2025-09-30) で再学習。2025-07〜09 は学習行としてのみ。
    tables = _ensure_cache(ISVAL_SCOPE, GEN2_TRAIN_RANGE[0], GEN2_VAL_RANGE[1])
    fit_masks = {c: ds.day_mask(t, *FINAL_FIT_RANGE) for c, t in tables.items()}
    models = _train_cell_models(tables, fit_masks, ck)
    if models is None:
        raise SystemExit("再学習が退化 — OOS 中断。")

    # ここで初めて OOS に触れる。直後にロック。
    OOS_LOCK_PATH.write_text(json.dumps(
        {"opened_for": [ck, alpha], "config_hash": config_hash()},
        ensure_ascii=False, indent=1), encoding="utf-8")
    oos_tables = _ensure_cache(OOS_SCOPE, *OOS_RANGE)
    oos_masks = {c: np.ones(len(t["b_ts"]), dtype=bool) for c, t in oos_tables.items()}

    trades, fired, total = _simulate_config(oos_tables, oos_masks, models, ck, alpha)
    metrics = trade_metrics(trades)
    t_null = time_shuffle_null(trades, oos_tables, ck)
    s_null = side_shuffle_null(trades, oos_tables, ck)
    OOS_RESULT_PATH.write_text(json.dumps({
        "verdict_note": "正式判定は ADR-0001 G1-G8 の採点で行う。",
        "frozen": {"cell_key": ck, "alpha": alpha},
        "oos_range": list(OOS_RANGE),
        "metrics": metrics,
        "rejection": {"fired_rows": fired, "total_rows": total},
        "nulls": {"time_shuffle": t_null, "side_shuffle": s_null},
        "max_concurrency": max_concurrency(trades),
        "config_hash": config_hash(),
        "trades": [asdict(t) for t in trades],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in metrics.items() if k not in ("by_code", "by_day")},
                     ensure_ascii=False, indent=1), flush=True)
    print(f"OOS done → {OOS_RESULT_PATH}", flush=True)


def main() -> None:
    cmds = {"sweep": cmd_sweep, "oos": cmd_oos}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        raise SystemExit(f"usage: gen8_net_selector_pipeline.py {{{'|'.join(cmds)}}}")
    cmds[sys.argv[1]]()


if __name__ == "__main__":
    main()
