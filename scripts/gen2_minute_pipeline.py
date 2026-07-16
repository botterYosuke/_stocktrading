"""足読み gen2 (stocks_minute) パイプライン。

usage:
  uv run python scripts/gen2_minute_pipeline.py calibrate    # friction + peer map
  uv run python scripts/gen2_minute_pipeline.py build-cache  # train+val のみ
  uv run python scripts/gen2_minute_pipeline.py sweep        # P1/P2/P3 × 6セル × 3τ
  uv run python scripts/gen2_minute_pipeline.py oos          # 凍結構成のみ 1 回

規約:
- OOS (2025-10-01〜2026-02-18) は sweep 完了・凍結まで一切読み込まない。
  oos は artifacts/gen2_minute/oos_lock.json で再実行を拒否する。
- friction は板録画 07-09 + 07-13 の実測 (07-14 は gen1/gen1b の封印日 — 触らない)。
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

from scalp_agent.dataset import side_fields_from_table  # noqa: E402
from scalp_agent.execution import Trade, max_concurrency, simulate_symbol_day  # noqa: E402
from scalp_agent.gates import trade_metrics  # noqa: E402
from scalp_agent_bars.minute import dataset as ds  # noqa: E402
from scalp_agent_bars.minute import source  # noqa: E402
from scalp_agent_bars.minute.config import (  # noqa: E402
    ATR_MULTS,
    CANDIDATE_MAX_CODE_SHARE,
    CANDIDATE_MIN_D,
    CANDIDATE_MIN_N,
    CANDIDATE_MIN_RATIO,
    CROSS_SECTION_TOP_K,
    FRICTION_CALIBRATION_PATH,
    HORIZON_BARS,
    LGBM_NUM_BOOST_ROUND,
    LGBM_PARAMS,
    OOS_RANGE,
    PATTERNS,
    TAUS,
    TRAIN_RANGE,
    UNIVERSE,
    VAL_RANGE,
    cell_key,
    config_hash,
)
from scalp_agent_bars.minute.features import (  # noqa: E402
    ALL_FEATURE_NAMES,
    OWN_FEATURE_NAMES,
    compute_peer_map,
    feature_schema_hash,
)
from scalp_agent_bars.minute.portfolio import simulate_cross_section  # noqa: E402

ART = Path("artifacts/gen2_minute")
PEER_MAP_PATH = ART / "peer_map.json"
SWEEP_RESULTS_PATH = ART / "sweep_val_results.json"
FROZEN_PATH = ART / "frozen_config.json"
OOS_LOCK_PATH = ART / "oos_lock.json"
OOS_RESULT_PATH = ART / "oos_result.json"

ISVAL_SCOPE = "isval"
OOS_SCOPE = "oos"


# ---- calibrate -----------------------------------------------------------------

def cmd_calibrate() -> None:
    """friction (板録画の実測スプレッド) と peer map (train 日次相関) を凍結する。"""
    ART.mkdir(parents=True, exist_ok=True)

    # friction: 07-09 + 07-13 の executable 行の median spread_bps
    from scalp_agent import loader as board_loader
    from scalp_agent.features import spread_bps as spread_bps_fn
    from scalp_agent.sessions import exec_subset

    friction_days = ("2026-07-09", "2026-07-13")  # 07-14 は封印 — 使わない
    med: dict[str, float] = {}
    for code in UNIVERSE:
        vals = []
        for day in friction_days:
            try:
                snap = board_loader.load_symbol_day(day, code)
            except Exception:
                continue
            ex = exec_subset(snap)
            if len(ex["ts"]):
                vals.append(spread_bps_fn(ex["bid_px_1"], ex["ask_px_1"]))
        if not vals:
            raise SystemExit(f"{code}: 板録画にスプレッド実測が無い — friction を決められない")
        med[code] = float(np.median(np.concatenate(vals)))
        print(f"  {code}: median spread = {med[code]:.2f} bps", flush=True)
    FRICTION_CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    FRICTION_CALIBRATION_PATH.write_text(json.dumps({
        "source_days": list(friction_days),
        "median_spread_bps": med,
        "note": "friction = median_spread_bps × FRICTION_SAFETY (config)。07-14 は封印日のため不使用",
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    # peer map: train 窓の日次終値相関 (val/OOS を見ない)
    import duckdb
    closes: dict[str, dict[str, float]] = {}
    for code in UNIVERSE:
        with duckdb.connect(str(source.db_path(code)), read_only=True) as con:
            rows = con.execute(
                """
                select cast(Date as varchar) as day, arg_max(Close, Time) as close
                from stocks_minute
                where Date >= ? and Date <= ? and Close > 0
                group by Date order by Date
                """,
                list(TRAIN_RANGE),
            ).fetchall()
        closes[code] = {r[0]: float(r[1]) for r in rows}
    peer = compute_peer_map(closes)
    PEER_MAP_PATH.write_text(json.dumps({
        "train_range": list(TRAIN_RANGE), "peer": peer,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"peer map: {peer}", flush=True)
    print(f"calibrate done → {FRICTION_CALIBRATION_PATH}, {PEER_MAP_PATH}", flush=True)


# ---- build-cache ----------------------------------------------------------------

def _load_calibration() -> tuple[dict[str, str], dict[str, float]]:
    peer = json.loads(PEER_MAP_PATH.read_text(encoding="utf-8"))["peer"]
    friction = ds.load_friction()
    return peer, friction


def _ensure_cache(scope: str, day_min: str, day_max: str) -> dict[str, dict[str, np.ndarray]]:
    peer, friction = _load_calibration()
    if all(ds.is_cache_valid(c, scope, day_min, day_max) for c in UNIVERSE):
        return {c: ds.load_cache(c, scope) for c in UNIVERSE}
    t0 = time.time()
    tables = ds.build_universe_tables(
        day_min, day_max, peer, friction,
        progress=lambda m: print(f"  [{scope}] {m} ({time.time()-t0:.0f}s)", flush=True),
    )
    for c, t in tables.items():
        ds.write_cache(c, scope, day_min, day_max, t)
    return tables


def cmd_build_cache() -> None:
    """train+val のみ。OOS はロック後に cmd_oos が作る。"""
    _ensure_cache(ISVAL_SCOPE, TRAIN_RANGE[0], VAL_RANGE[1])
    print("cache build done (train+val only; OOS stays sealed)", flush=True)


# ---- sweep ------------------------------------------------------------------------

def _train_booster(x: np.ndarray, y: np.ndarray) -> lgb.Booster | None:
    if len(y) < 1000 or len(np.unique(y)) < 3:
        return None
    dset = lgb.Dataset(x, label=y, params={"max_bin": LGBM_PARAMS["max_bin"]})
    return lgb.train(LGBM_PARAMS, dset, num_boost_round=LGBM_NUM_BOOST_ROUND)


def _cell_xy(table: dict[str, np.ndarray], ck: str, names, mask: np.ndarray):
    yv = np.asarray(table[f"yv_{ck}"], dtype=bool) & mask
    x = ds.features_matrix(table, names)[yv]
    y = np.asarray(table[f"y_{ck}"], dtype=np.int64)[yv] + 1
    return x, y


def _simulate_per_symbol(
    code: str, table: dict[str, np.ndarray], mask: np.ndarray,
    scores: np.ndarray, ck: str, taus, per_tau: dict[float, list[Trade]],
) -> None:
    """day ごとにスライスして simulate_symbol_day を呼ぶ (mask = val/oos 行)。"""
    days = np.asarray(table["day"])[mask]
    b_ts = np.asarray(table["b_ts"], dtype=np.float64)[mask]
    lf = {f: np.asarray(v)[mask] for f, v in side_fields_from_table(table, ck, "L").items()}
    sf = {f: np.asarray(v)[mask] for f, v in side_fields_from_table(table, ck, "S").items()}
    for day in np.unique(days):
        dm = days == day
        lfd = {f: v[dm] for f, v in lf.items()}
        sfd = {f: v[dm] for f, v in sf.items()}
        for tau in taus:
            per_tau[tau].extend(
                simulate_symbol_day(code, str(day), b_ts[dm], scores[dm], lfd, sfd, tau)
            )


def _simulate_p3(
    tables: dict[str, dict[str, np.ndarray]],
    masks: dict[str, np.ndarray],
    scores_by_code: dict[str, np.ndarray],
    ck: str, taus,
) -> dict[float, list[Trade]]:
    per_tau: dict[float, list[Trade]] = {t: [] for t in taus}
    all_days = sorted({
        str(d) for c in tables for d in np.unique(np.asarray(tables[c]["day"])[masks[c]])
    })
    prep: dict[str, dict[str, dict]] = {}
    for c, table in tables.items():
        m = masks[c]
        days = np.asarray(table["day"])[m]
        b_ts = np.asarray(table["b_ts"], dtype=np.float64)[m]
        lf = {f: np.asarray(v)[m] for f, v in side_fields_from_table(table, ck, "L").items()}
        sf = {f: np.asarray(v)[m] for f, v in side_fields_from_table(table, ck, "S").items()}
        sc = scores_by_code[c]
        for day in np.unique(days):
            dm = days == day
            prep.setdefault(str(day), {})[c] = {
                "b_ts": b_ts[dm],
                "scores": sc[dm],
                "lf": {f: v[dm] for f, v in lf.items()},
                "sf": {f: v[dm] for f, v in sf.items()},
            }
    for day in all_days:
        rows = prep.get(day, {})
        for tau in taus:
            per_tau[tau].extend(
                simulate_cross_section(day, rows, tau, CROSS_SECTION_TOP_K)
            )
    return per_tau


def is_candidate(m: dict) -> bool:
    return (
        m["n"] >= CANDIDATE_MIN_N
        and m["D"] >= CANDIDATE_MIN_D
        and m["net_per_entry"] is not None and m["net_per_entry"] > 0
        and m["ratio"] is not None and m["ratio"] >= CANDIDATE_MIN_RATIO
        and (m["max_code_share"] is None or m["max_code_share"] <= CANDIDATE_MAX_CODE_SHARE)
    )


def _sweep_pattern(
    pattern: str,
    tables: dict[str, dict[str, np.ndarray]],
    train_masks: dict[str, np.ndarray],
    eval_masks: dict[str, np.ndarray],
) -> dict[tuple, dict]:
    """1 パターン分の (cell, tau) → metrics。"""
    names = OWN_FEATURE_NAMES if pattern == "P1_single" else ALL_FEATURE_NAMES
    results: dict[tuple, dict] = {}
    for h in HORIZON_BARS:
        for mu in ATR_MULTS:
            ck = cell_key(h, mu)
            if pattern == "P1_single":
                per_tau: dict[float, list[Trade]] = {t: [] for t in TAUS}
                for c, table in tables.items():
                    x, y = _cell_xy(table, ck, names, train_masks[c])
                    booster = _train_booster(x, y)
                    if booster is None:
                        continue
                    m = eval_masks[c]
                    if not m.any():
                        continue
                    scores = booster.predict(ds.features_matrix(table, names)[m])
                    _simulate_per_symbol(c, table, m, scores, ck, TAUS, per_tau)
            else:
                xs, ys = [], []
                for c, table in tables.items():
                    x, y = _cell_xy(table, ck, names, train_masks[c])
                    xs.append(x)
                    ys.append(y)
                booster = _train_booster(np.concatenate(xs), np.concatenate(ys))
                if booster is None:
                    for tau in TAUS:
                        results[(pattern, h, mu, tau)] = trade_metrics([])
                    continue
                scores_by_code = {
                    c: booster.predict(ds.features_matrix(t, names)[eval_masks[c]])
                    for c, t in tables.items()
                }
                if pattern == "P2_cross_features":
                    per_tau = {t: [] for t in TAUS}
                    for c, table in tables.items():
                        if eval_masks[c].any():
                            _simulate_per_symbol(
                                c, table, eval_masks[c], scores_by_code[c], ck, TAUS, per_tau
                            )
                else:  # P3_pooled_topk
                    per_tau = _simulate_p3(tables, eval_masks, scores_by_code, ck, TAUS)
            for tau in TAUS:
                mtr = trade_metrics(per_tau[tau])
                results[(pattern, h, mu, tau)] = mtr
                print(f"  {pattern} {ck} tau={tau}: n={mtr['n']} D={mtr['D']} "
                      f"net={None if mtr['net_per_entry'] is None else round(mtr['net_per_entry'], 3)} "
                      f"ratio={None if mtr['ratio'] is None else round(mtr['ratio'], 3)} "
                      f"t={None if mtr['t_net'] is None else round(mtr['t_net'], 2)}", flush=True)
    return results


def _select(results: dict[tuple, dict]) -> tuple | None:
    cands = [(k, m) for k, m in results.items() if is_candidate(m)]
    if not cands:
        return None
    pat_order = {p: i for i, p in enumerate(PATTERNS)}

    def sort_key(item):
        (pat, h, mu, tau), m = item
        return (-m["net_per_entry"], -m["n"], h, mu, -tau, pat_order[pat])

    cands.sort(key=sort_key)
    return cands[0][0]


def cmd_sweep() -> None:
    ART.mkdir(parents=True, exist_ok=True)
    print(f"gen2 config_hash = {config_hash()}", flush=True)
    tables = _ensure_cache(ISVAL_SCOPE, TRAIN_RANGE[0], VAL_RANGE[1])
    train_masks = {c: ds.day_mask(t, *TRAIN_RANGE) for c, t in tables.items()}
    val_masks = {c: ds.day_mask(t, *VAL_RANGE) for c, t in tables.items()}
    for c in tables:
        assert not (train_masks[c] & val_masks[c]).any()

    results: dict[tuple, dict] = {}
    for pattern in PATTERNS:
        print(f"== pattern {pattern} ==", flush=True)
        results |= _sweep_pattern(pattern, tables, train_masks, val_masks)

    serial = {f"{p}|{cell_key(h, mu)}|t{int(tau*100)}": m
              for (p, h, mu, tau), m in results.items()}
    SWEEP_RESULTS_PATH.write_text(json.dumps({
        "config_hash": config_hash(),
        "feature_schema_hash": feature_schema_hash(),
        "results": serial,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    chosen = _select(results)
    if chosen is None:
        FROZEN_PATH.write_text(json.dumps({
            "verdict": "IS-KILL",
            "reason": f"no (pattern, cell, tau) met n>={CANDIDATE_MIN_N}, D>={CANDIDATE_MIN_D}, "
                      f"net>0, ratio>={CANDIDATE_MIN_RATIO}, code_share<={CANDIDATE_MAX_CODE_SHARE} on val",
            "config_hash": config_hash(),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print("IS-KILL: no candidate. OOS stays sealed.", flush=True)
        return
    p, h, mu, tau = chosen
    FROZEN_PATH.write_text(json.dumps({
        "verdict": "FROZEN",
        "pattern": p, "horizon_bars": h, "atr_mult": mu, "tau": tau,
        "cell_key": cell_key(h, mu),
        "val_metrics": results[chosen],
        "config_hash": config_hash(),
        "feature_schema_hash": feature_schema_hash(),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    m = results[chosen]
    print(f"FROZEN: {p} {cell_key(h, mu)} tau={tau} net/entry={m['net_per_entry']:.2f}bps "
          f"n={m['n']} D={m['D']} ratio={m['ratio']:.2f}", flush=True)


# ---- oos --------------------------------------------------------------------------

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
    p, h, mu, tau = frozen["pattern"], frozen["horizon_bars"], frozen["atr_mult"], frozen["tau"]
    ck = cell_key(h, mu)
    names = OWN_FEATURE_NAMES if p == "P1_single" else ALL_FEATURE_NAMES

    tables = _ensure_cache(ISVAL_SCOPE, TRAIN_RANGE[0], VAL_RANGE[1])
    isval_masks = {c: np.ones(len(t["b_ts"]), dtype=bool) for c, t in tables.items()}

    # ここで初めて OOS に触れる。直後にロック。
    OOS_LOCK_PATH.write_text(json.dumps(
        {"opened_for": [p, ck, tau], "config_hash": config_hash()},
        ensure_ascii=False, indent=1), encoding="utf-8")
    oos_tables = _ensure_cache(OOS_SCOPE, *OOS_RANGE)
    oos_masks = {c: np.ones(len(t["b_ts"]), dtype=bool) for c, t in oos_tables.items()}

    trades: list[Trade]
    if p == "P1_single":
        per_tau: dict[float, list[Trade]] = {tau: []}
        for c, table in tables.items():
            x, y = _cell_xy(table, ck, names, isval_masks[c])
            booster = _train_booster(x, y)
            if booster is None or c not in oos_tables:
                continue
            ot = oos_tables[c]
            scores = booster.predict(ds.features_matrix(ot, names))
            _simulate_per_symbol(c, ot, oos_masks[c], scores, ck, (tau,), per_tau)
        trades = per_tau[tau]
    else:
        xs, ys = [], []
        for c, table in tables.items():
            x, y = _cell_xy(table, ck, names, isval_masks[c])
            xs.append(x)
            ys.append(y)
        booster = _train_booster(np.concatenate(xs), np.concatenate(ys))
        if booster is None:
            raise SystemExit("再学習が退化 — OOS 中断。")
        scores_by_code = {
            c: booster.predict(ds.features_matrix(t, names)) for c, t in oos_tables.items()
        }
        if p == "P2_cross_features":
            per_tau = {tau: []}
            for c, t in oos_tables.items():
                _simulate_per_symbol(c, t, oos_masks[c], scores_by_code[c], ck, (tau,), per_tau)
            trades = per_tau[tau]
        else:
            trades = _simulate_p3(oos_tables, oos_masks, scores_by_code, ck, (tau,))[tau]

    metrics = trade_metrics(trades)
    OOS_RESULT_PATH.write_text(json.dumps({
        "verdict_note": "D>=20 の OOS。正式判定は ADR-0001 G1-G8 の採点で行う。",
        "frozen": {"pattern": p, "cell_key": ck, "tau": tau},
        "oos_range": list(OOS_RANGE),
        "metrics": metrics,
        "max_concurrency": max_concurrency(trades),
        "config_hash": config_hash(),
        "trades": [asdict(t) for t in trades],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in metrics.items() if k not in ("by_code", "by_day")},
                     ensure_ascii=False, indent=1), flush=True)
    print(f"OOS done → {OOS_RESULT_PATH}", flush=True)


def main() -> None:
    cmds = {"calibrate": cmd_calibrate, "build-cache": cmd_build_cache,
            "sweep": cmd_sweep, "oos": cmd_oos}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        raise SystemExit(f"usage: gen2_minute_pipeline.py {{{'|'.join(cmds)}}}")
    cmds[sys.argv[1]]()


if __name__ == "__main__":
    main()
