"""足読み gen3 (系列 NN) パイプライン。新 family `gen3_minute_nn_v1`。

usage:
  uv run --group nn python scripts/gen3_minute_nn_pipeline.py build-seq
  uv run --group nn python scripts/gen3_minute_nn_pipeline.py train
  uv run --group nn python scripts/gen3_minute_nn_pipeline.py sweep
  uv run --group nn python scripts/gen3_minute_nn_pipeline.py oos

規約:
- ラベル・保守的バー執行・friction・分割は gen2 と同一 (gen2 isval キャッシュを
  行単位で再利用し、b_ts の完全一致を assert する)。
- 学習は FIT_RANGE、early stop は ESTOP_RANGE のみ。公式 val は sweep まで見ない。
- OOS は凍結構成のみ 1 回。**再学習なし** — train (≤2025-06-30) で学習済みの
  モデルをそのまま 2025-10 以降へ適用する (リーク方向にさらに保守的)。
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scalp_agent.dataset import side_fields_from_table  # noqa: E402
from scalp_agent.execution import Trade, max_concurrency, simulate_symbol_day  # noqa: E402
from scalp_agent.gates import trade_metrics  # noqa: E402
from scalp_agent_bars.minute import dataset as ds  # noqa: E402
from scalp_agent_bars.minute import source  # noqa: E402
from scalp_agent_bars.minute.config import (  # noqa: E402
    ATR_BARS,
    ENTRY_END_TOD,
    SESSION_AFTERNOON,
    SESSION_MORNING,
)
from scalp_agent_bars.minute.exec_bars import atr, eligible_decisions  # noqa: E402
from scalp_agent_bars.minute.nn_config import (  # noqa: E402
    CANDIDATE_MAX_CODE_SHARE,
    CANDIDATE_MIN_D,
    CANDIDATE_MIN_N,
    CANDIDATE_MIN_RATIO,
    CROSS_SECTION_TOP_K,
    ESTOP_RANGE,
    FIT_RANGE,
    OOS_RANGE,
    PATTERNS,
    TAUS,
    TRAIN_RANGE,
    UNIVERSE,
    VAL_RANGE,
    cell_key,
    config_hash,
    grid_cells,
)
from scalp_agent_bars.minute.portfolio import simulate_cross_section  # noqa: E402
from scalp_agent_bars.minute.sequences import day_sequences  # noqa: E402

ART = Path("artifacts/gen3_minute_nn")
SEQ_DIR = Path("artifacts/cache/gen3_seq")
MODEL_PATH = ART / "model.pt"
TRAIN_LOG_PATH = ART / "train_log.json"
SWEEP_RESULTS_PATH = ART / "sweep_val_results.json"
FROZEN_PATH = ART / "frozen_config.json"
OOS_LOCK_PATH = ART / "oos_lock.json"
OOS_RESULT_PATH = ART / "oos_result.json"


def _day_int(day: str) -> int:
    return int(day.replace("-", ""))


def _day_str(day_int: int) -> str:
    s = str(day_int)
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


# ---- build-seq ------------------------------------------------------------------

def _build_scope(scope: str, day_min: str, day_max: str) -> None:
    """系列テンソルを構築し、gen2 キャッシュと行単位で整合することを assert する。"""
    out_dir = SEQ_DIR / scope
    out_dir.mkdir(parents=True, exist_ok=True)
    cells = grid_cells()
    seqs, stas, ys, code_ids, day_ints, btss = [], [], [], [], [], []
    t0 = time.time()
    for ci, code in enumerate(sorted(UNIVERSE)):
        bars_by_day = source.load_symbol_days(code, day_min, day_max)
        code_seq, code_sta, code_bts, code_day = [], [], [], []
        for day in sorted(bars_by_day):
            b = bars_by_day[day]
            atr_arr = atr(b["high"], b["low"], b["close"], ATR_BARS)
            didx = eligible_decisions(
                b["start_tod"], b["open"], atr_arr,
                SESSION_MORNING, SESSION_AFTERNOON, ENTRY_END_TOD,
            )
            if len(didx) == 0:
                continue
            seq, sta = day_sequences(b, didx, atr_arr, ds._prev_ohlc_of(bars_by_day, day))
            code_seq.append(seq)
            code_sta.append(sta)
            code_bts.append(b["ts"][didx + 1])
            code_day.append(np.full(len(didx), _day_int(day), dtype=np.int32))
        bts = np.concatenate(code_bts)
        # gen2 キャッシュと同一の決定行であることを保証 (ラベルを行単位で再利用する前提)
        table = ds.load_cache(code, "isval" if scope == "isval" else "oos")
        assert np.allclose(bts, np.asarray(table["b_ts"], dtype=np.float64)), \
            f"{code}: 系列の決定行が gen2 キャッシュと一致しない"
        y = np.full((len(bts), len(cells)), -1, dtype=np.int8)
        for k, (h, m) in enumerate(cells):
            ck = cell_key(h, m)
            yv = np.asarray(table[f"yv_{ck}"], dtype=bool)
            y[:, k] = np.where(yv, np.asarray(table[f"y_{ck}"], dtype=np.int8) + 1, -1)
        seqs.append(np.concatenate(code_seq))
        stas.append(np.concatenate(code_sta))
        ys.append(y)
        code_ids.append(np.full(len(bts), ci, dtype=np.int16))
        day_ints.append(np.concatenate(code_day))
        btss.append(bts)
        print(f"  [{scope}] {ci+1}/{len(UNIVERSE)} {code}: {len(bts)} rows "
              f"({time.time()-t0:.0f}s)", flush=True)
    np.save(out_dir / "seq.npy", np.concatenate(seqs))
    np.save(out_dir / "sta.npy", np.concatenate(stas))
    np.save(out_dir / "y.npy", np.concatenate(ys))
    np.save(out_dir / "code_id.npy", np.concatenate(code_ids))
    np.save(out_dir / "day.npy", np.concatenate(day_ints))
    np.save(out_dir / "b_ts.npy", np.concatenate(btss))
    (out_dir / "meta.json").write_text(json.dumps({
        "config_hash": config_hash(),
        "codes": sorted(UNIVERSE),
        "day_range": [day_min, day_max],
        "n": int(sum(len(b) for b in btss)),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"build-seq {scope} done → {out_dir}", flush=True)


def _load_scope(scope: str):
    d = SEQ_DIR / scope
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["config_hash"] == config_hash(), "config 変更後は build-seq をやり直す"
    return {
        "seq": np.load(d / "seq.npy", mmap_mode="r"),
        "sta": np.load(d / "sta.npy", mmap_mode="r"),
        "y": np.load(d / "y.npy", mmap_mode="r"),
        "code_id": np.load(d / "code_id.npy"),
        "day": np.load(d / "day.npy"),
        "b_ts": np.load(d / "b_ts.npy"),
        "codes": meta["codes"],
    }


def cmd_build_seq() -> None:
    _build_scope("isval", TRAIN_RANGE[0], VAL_RANGE[1])


# ---- train ------------------------------------------------------------------------

def cmd_train() -> None:
    import torch

    from scalp_agent_bars.minute.nn_model import train_model

    ART.mkdir(parents=True, exist_ok=True)
    data = _load_scope("isval")
    day = data["day"]
    fit_idx = np.where((day >= _day_int(FIT_RANGE[0])) & (day <= _day_int(FIT_RANGE[1])))[0]
    estop_idx = np.where((day >= _day_int(ESTOP_RANGE[0])) & (day <= _day_int(ESTOP_RANGE[1])))[0]
    val_lo = _day_int(VAL_RANGE[0])
    assert day[fit_idx].max() < val_lo and day[estop_idx].max() < val_lo
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"gen3 config_hash = {config_hash()}", flush=True)
    print(f"device={device} fit={len(fit_idx)} estop={len(estop_idx)}", flush=True)
    state, history = train_model(
        data["seq"], data["sta"], data["y"], fit_idx, estop_idx, device,
    )
    torch.save(state, MODEL_PATH)
    TRAIN_LOG_PATH.write_text(json.dumps({
        "config_hash": config_hash(), "device": device, "history": history,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"train done → {MODEL_PATH}", flush=True)


# ---- sweep -------------------------------------------------------------------------

def _simulate_per_symbol(code, days, b_ts, lf, sf, scores, taus, per_tau) -> None:
    for day in np.unique(days):
        dm = days == day
        lfd = {f: v[dm] for f, v in lf.items()}
        sfd = {f: v[dm] for f, v in sf.items()}
        for tau in taus:
            per_tau[tau].extend(simulate_symbol_day(
                code, _day_str(int(day)), b_ts[dm], scores[dm], lfd, sfd, tau))


def is_candidate(m: dict) -> bool:
    return (
        m["n"] >= CANDIDATE_MIN_N and m["D"] >= CANDIDATE_MIN_D
        and m["net_per_entry"] is not None and m["net_per_entry"] > 0
        and m["ratio"] is not None and m["ratio"] >= CANDIDATE_MIN_RATIO
        and (m["max_code_share"] is None or m["max_code_share"] <= CANDIDATE_MAX_CODE_SHARE)
    )


def _evaluate(data, scores_all, day_lo: int, day_hi: int, scope: str,
              patterns=PATTERNS, taus=TAUS) -> dict[tuple, dict]:
    """(pattern, h, m, tau) → metrics。scores_all: (N, 6, 3)。"""
    cells = grid_cells()
    results: dict[tuple, dict] = {}
    codes = data["codes"]
    in_win = (data["day"] >= day_lo) & (data["day"] <= day_hi)
    per_code_rows = {}
    for ci, code in enumerate(codes):
        rows = np.where((data["code_id"] == ci) & in_win)[0]
        if len(rows) == 0:
            continue
        table = ds.load_cache(code, scope)
        tmask = ds.day_mask(table, _day_str(day_lo), _day_str(day_hi))
        assert np.allclose(data["b_ts"][rows],
                           np.asarray(table["b_ts"], dtype=np.float64)[tmask]), code
        per_code_rows[code] = (rows, table, tmask)
    for k, (h, m) in enumerate(cells):
        ck = cell_key(h, m)
        for pattern in patterns:
            per_tau: dict[float, list[Trade]] = {t: [] for t in taus}
            if pattern == "NN_pooled":
                for code, (rows, table, tmask) in per_code_rows.items():
                    lf = {f: np.asarray(v)[tmask] for f, v in side_fields_from_table(table, ck, "L").items()}
                    sf = {f: np.asarray(v)[tmask] for f, v in side_fields_from_table(table, ck, "S").items()}
                    _simulate_per_symbol(
                        code, data["day"][rows],
                        np.asarray(data["b_ts"][rows], dtype=np.float64),
                        lf, sf, scores_all[rows][:, k, :], taus, per_tau)
            else:  # NN_pooled_topk
                prep: dict[str, dict[str, dict]] = {}
                for code, (rows, table, tmask) in per_code_rows.items():
                    lf = {f: np.asarray(v)[tmask] for f, v in side_fields_from_table(table, ck, "L").items()}
                    sf = {f: np.asarray(v)[tmask] for f, v in side_fields_from_table(table, ck, "S").items()}
                    days = data["day"][rows]
                    bts = np.asarray(data["b_ts"][rows], dtype=np.float64)
                    sc = scores_all[rows][:, k, :]
                    for day in np.unique(days):
                        dm = days == day
                        prep.setdefault(_day_str(int(day)), {})[code] = {
                            "b_ts": bts[dm], "scores": sc[dm],
                            "lf": {f: v[dm] for f, v in lf.items()},
                            "sf": {f: v[dm] for f, v in sf.items()},
                        }
                for day in sorted(prep):
                    for tau in taus:
                        per_tau[tau].extend(simulate_cross_section(
                            day, prep[day], tau, CROSS_SECTION_TOP_K))
            for tau in taus:
                mtr = trade_metrics(per_tau[tau])
                results[(pattern, h, m, tau)] = mtr
                print(f"  {pattern} {ck} tau={tau}: n={mtr['n']} D={mtr['D']} "
                      f"net={None if mtr['net_per_entry'] is None else round(mtr['net_per_entry'], 3)} "
                      f"ratio={None if mtr['ratio'] is None else round(mtr['ratio'], 3)} "
                      f"t={None if mtr['t_net'] is None else round(mtr['t_net'], 2)}", flush=True)
    return results


def cmd_sweep() -> None:
    import torch

    from scalp_agent_bars.minute.nn_model import predict_scores

    data = _load_scope("isval")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    state = torch.load(MODEL_PATH, weights_only=True)
    val_rows = np.where((data["day"] >= _day_int(VAL_RANGE[0]))
                        & (data["day"] <= _day_int(VAL_RANGE[1])))[0]
    scores_val = np.zeros((len(data["day"]), 6, 3), dtype=np.float32)
    scores_val[val_rows] = predict_scores(state, data["seq"], data["sta"], val_rows, device)
    print(f"gen3 config_hash = {config_hash()} (val rows={len(val_rows)})", flush=True)

    results = _evaluate(data, scores_val, _day_int(VAL_RANGE[0]), _day_int(VAL_RANGE[1]), "isval")
    serial = {f"{p}|{cell_key(h, m)}|t{int(tau*100)}": mm
              for (p, h, m, tau), mm in results.items()}
    SWEEP_RESULTS_PATH.write_text(json.dumps({
        "config_hash": config_hash(), "results": serial,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    cands = [(kk, mm) for kk, mm in results.items() if is_candidate(mm)]
    if not cands:
        FROZEN_PATH.write_text(json.dumps({
            "verdict": "IS-KILL",
            "reason": f"no (pattern, cell, tau) met n>={CANDIDATE_MIN_N}, D>={CANDIDATE_MIN_D}, "
                      f"net>0, ratio>={CANDIDATE_MIN_RATIO}, code_share<={CANDIDATE_MAX_CODE_SHARE} on val",
            "config_hash": config_hash(),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print("IS-KILL: no candidate. OOS stays sealed.", flush=True)
        return
    pat_order = {p: i for i, p in enumerate(PATTERNS)}
    cands.sort(key=lambda item: (-item[1]["net_per_entry"], -item[1]["n"],
                                 item[0][1], item[0][2], -item[0][3], pat_order[item[0][0]]))
    (p, h, m, tau), mm = cands[0]
    FROZEN_PATH.write_text(json.dumps({
        "verdict": "FROZEN", "pattern": p, "horizon_bars": h, "atr_mult": m,
        "tau": tau, "cell_key": cell_key(h, m), "val_metrics": mm,
        "config_hash": config_hash(),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"FROZEN: {p} {cell_key(h, m)} tau={tau} net={mm['net_per_entry']:.2f} "
          f"n={mm['n']} D={mm['D']} ratio={mm['ratio']:.2f}", flush=True)


def cmd_oos() -> None:
    import torch

    from scalp_agent_bars.minute.nn_model import predict_scores

    if not FROZEN_PATH.exists():
        raise SystemExit("frozen_config.json がない。先に sweep。")
    frozen = json.loads(FROZEN_PATH.read_text(encoding="utf-8"))
    if frozen.get("verdict") != "FROZEN":
        raise SystemExit(f"凍結構成が無い (verdict={frozen.get('verdict')})。OOS は開けない。")
    if OOS_LOCK_PATH.exists():
        raise SystemExit("oos_lock.json が存在する。OOS は 1 回だけ。")
    if frozen["config_hash"] != config_hash():
        raise SystemExit("config_hash 不一致。")
    OOS_LOCK_PATH.write_text(json.dumps(
        {"opened_for": [frozen["pattern"], frozen["cell_key"], frozen["tau"]],
         "config_hash": config_hash()}, ensure_ascii=False, indent=1), encoding="utf-8")

    # OOS 側の gen2 キャッシュと系列を構築 (ここで初めて OOS 日に触れる)
    peer, friction = json.loads((Path("artifacts/gen2_minute/peer_map.json"))
                                .read_text(encoding="utf-8"))["peer"], ds.load_friction()
    if not all(ds.is_cache_valid(c, "oos", *OOS_RANGE) for c in UNIVERSE):
        tables = ds.build_universe_tables(*OOS_RANGE, peer, friction,
                                          progress=lambda mm: print("  [oos]", mm, flush=True))
        for c, t in tables.items():
            ds.write_cache(c, "oos", *OOS_RANGE, t)
    if not (SEQ_DIR / "oos" / "meta.json").exists():
        _build_scope("oos", *OOS_RANGE)

    data = _load_scope("oos")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    state = torch.load(MODEL_PATH, weights_only=True)
    rows = np.arange(len(data["day"]))
    scores = np.zeros((len(rows), 6, 3), dtype=np.float32)
    scores[rows] = predict_scores(state, data["seq"], data["sta"], rows, device)
    p, h, m, tau = frozen["pattern"], frozen["horizon_bars"], frozen["atr_mult"], frozen["tau"]
    results = _evaluate(data, scores, _day_int(OOS_RANGE[0]), _day_int(OOS_RANGE[1]),
                        "oos", patterns=(p,), taus=(tau,))
    mtr = results[(p, h, m, tau)]
    OOS_RESULT_PATH.write_text(json.dumps({
        "verdict_note": "D>=20 の OOS。正式判定は ADR-0001 G1-G8 の採点で行う。再学習なし (train<=2025-06-30 のモデルを適用)。",
        "frozen": {"pattern": p, "cell_key": cell_key(h, m), "tau": tau},
        "oos_range": list(OOS_RANGE),
        "metrics": mtr,
        "config_hash": config_hash(),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({kk: vv for kk, vv in mtr.items() if kk not in ("by_code", "by_day")},
                     ensure_ascii=False, indent=1), flush=True)
    print(f"OOS done → {OOS_RESULT_PATH}", flush=True)


def main() -> None:
    cmds = {"build-seq": cmd_build_seq, "train": cmd_train,
            "sweep": cmd_sweep, "oos": cmd_oos}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        raise SystemExit(f"usage: gen3_minute_nn_pipeline.py {{{'|'.join(cmds)}}}")
    cmds[sys.argv[1]]()


if __name__ == "__main__":
    main()
