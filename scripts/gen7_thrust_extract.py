"""足読み gen7 — thrust イベント抽出 (owner 指定の「手動抽出」の実行体)。

usage:
  uv run python scripts/gen7_thrust_extract.py universe   # PIT 日次ユニバース 3 定義
  uv run python scripts/gen7_thrust_extract.py extract    # 凍結検出器で全件抽出
  uv run python scripts/gen7_thrust_extract.py report     # イベント数 + 帰結分布

owner 承認事項 (2026-07-17 grill-me):
  - 検出器は issue#1 から凍結移植。掃引しない。マッチは選び好みせず全件。
  - ユニバースは 3 定義を並べてイベント数を先に見る。
  - short (down-thrust) と long (up-thrust 鏡像) を同時に測る。

封印 (触らない):
  - 2025-04        : gen4/gen5/gen6 の OOS
  - 2025-10〜2026-02: gen2/gen3 の OOS
本抽出は TRAIN 2024-04-01〜2024-11-29 / VAL 2024-12-02〜2025-03-31 のみ。

gen4 からの意図的な差分: MINUTE_COVERAGE_REQ を抽出窓に合わせる。gen4 は封印中の
OOS 月 (2025-04-30) までの分足を要求しており、売買代金上位 400 のうち 53 銘柄
(13.2%) を経済的理由でなくデータ都合で落としていた。owner の実トレード銘柄 9107
(代金 44 位) もその 1 つ。本 family はこれを継承しない。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from scalp_agent_bars.thrust import detect as D
from scalp_agent_bars.thrust import universe as U
from scalp_agent_bars.xsec import daily as daily_mod
from scalp_agent_bars.xsec.config import MINUTE_DIR
from scalp_agent_bars.xsec.friction import friction_bps

ART = Path("artifacts/gen7_thrust")
DAILY_PANEL = Path("artifacts/gen4_xsec/daily_panel.parquet")
COVERAGE = Path("artifacts/gen4_xsec/minute_coverage.json")

EXTRACT_RANGE = ("2024-04-01", "2025-03-31")
TRAIN_RANGE = ("2024-04-01", "2024-11-29")
VAL_RANGE = ("2024-12-02", "2025-03-31")

UNIVERSE_PATH = ART / "universe_daily.json"
EVENTS_PATH = ART / "events.parquet"


def _split_of(day: str) -> str:
    if TRAIN_RANGE[0] <= day <= TRAIN_RANGE[1]:
        return "train"
    if VAL_RANGE[0] <= day <= VAL_RANGE[1]:
        return "val"
    return "out"


def _has_minute() -> set[str]:
    """抽出窓を満たす銘柄。gen4 と違い封印 OOS 月までは要求しない。"""
    cov = json.loads(COVERAGE.read_text(encoding="utf-8"))
    return {
        c for c, v in cov.items()
        if "error" not in v
        and v["from"] <= EXTRACT_RANGE[0]
        and v["to"] >= EXTRACT_RANGE[1]
    }


def cmd_universe() -> None:
    ART.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    t = pq.read_table(DAILY_PANEL)
    code = np.asarray(t["code"]).astype(str)
    day = np.asarray(t["day"]).astype(str)
    adj_close = np.asarray(t["adj_close"], dtype=np.float64)
    close = np.asarray(t["close"], dtype=np.float64)
    turnover = np.asarray(t["turnover"], dtype=np.float64)

    _, prime = daily_mod.load_sector_map()
    prime_norm = {c.upper() for c in prime} | set(prime)
    has_min = _has_minute()
    print(f"prime={len(prime_norm)} has_minute({EXTRACT_RANGE})={len(has_min)}", flush=True)

    order = np.lexsort((day, code))
    code, day = code[order], day[order]
    adj_close, close, turnover = adj_close[order], close[order], turnover[order]
    uniq, first = np.unique(code, return_index=True)
    bounds = np.concatenate([first, [code.size]])

    per_def: dict[str, dict[str, list[str]]] = {n: {} for n in U.UNIVERSE_NAMES}
    n_codes = 0
    for k in range(uniq.size):
        c = str(uniq[k])
        if c not in prime_norm or c not in has_min:
            continue
        lo, hi = int(bounds[k]), int(bounds[k + 1])
        d = day[lo:hi]
        flags = U.pit_flags(d, adj_close[lo:hi], close[lo:hi], turnover[lo:hi])
        masks = U.universe_masks(flags)
        used = False
        for name, m in masks.items():
            sel = m & (d >= EXTRACT_RANGE[0]) & (d <= EXTRACT_RANGE[1])
            for dd in d[sel]:
                per_def[name].setdefault(str(dd), []).append(c)
                used = True
        n_codes += int(used)
        if k % 500 == 0:
            print(f"  {k}/{uniq.size} ({time.time() - t0:.0f}s)", flush=True)

    UNIVERSE_PATH.write_text(json.dumps({
        "extract_range": EXTRACT_RANGE,
        "params": {
            "new_high_window": U.NEW_HIGH_WINDOW, "spike_window": U.SPIKE_WINDOW,
            "spike_mult": U.SPIKE_MULT, "min_median_close": U.MIN_MEDIAN_CLOSE,
            "min_median_turnover": U.MIN_MEDIAN_TURNOVER, "liq_window": U.LIQ_WINDOW,
        },
        "universe": per_def,
    }, ensure_ascii=False), encoding="utf-8")

    print(f"\ncodes with >=1 universe day: {n_codes}")
    for name in U.UNIVERSE_NAMES:
        days = per_def[name]
        tot = sum(len(v) for v in days.values())
        print(f"  {name:24s} D={len(days):4d} days  stock-days={tot:7d}  "
              f"avg={tot / max(len(days), 1):6.1f}/day")


def _load_minute(code: str):
    import duckdb

    f = MINUTE_DIR / f"{code}.duckdb"
    if not f.exists():
        return None
    with duckdb.connect(str(f), read_only=True) as con:
        r = con.execute(
            """
            select cast(Date as varchar) as d, Time, Open, High, Low, Close, Volume
            from stocks_minute
            where Date >= ? and Date <= ?
            order by Date, Time
            """,
            [EXTRACT_RANGE[0], EXTRACT_RANGE[1]],
        ).fetchall()
    if not r:
        return None
    return r


def cmd_extract() -> None:
    ART.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    uni = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))["universe"]

    # code -> {day -> set(universe names)}
    by_code: dict[str, dict[str, set[str]]] = {}
    for name, days in uni.items():
        for d, codes in days.items():
            for c in codes:
                by_code.setdefault(c, {}).setdefault(d, set()).add(name)

    rows: list[dict] = []
    codes = sorted(by_code)
    print(f"codes to scan: {len(codes)}", flush=True)
    for ci, c in enumerate(codes):
        recs = _load_minute(c)
        if recs is None:
            continue
        # group by day
        cur_day, buf = None, []

        def flush(dday: str, b: list) -> None:
            if dday not in by_code[c] or len(b) < U.SPIKE_WINDOW + 5:
                return
            o = np.array([x[2] for x in b], dtype=np.float64)
            h = np.array([x[3] for x in b], dtype=np.float64)
            lo_ = np.array([x[4] for x in b], dtype=np.float64)
            cl = np.array([x[5] for x in b], dtype=np.float64)
            vol = np.array([x[6] for x in b], dtype=np.float64)
            del o
            names = by_code[c][dday]
            for side, sname in ((D.SIDE_DOWN, "short"), (D.SIDE_UP, "long")):
                sig, cum_ret, vm = D.thrust_signals(cl, vol, side)
                idx = np.flatnonzero(sig)
                if idx.size == 0:
                    continue
                fp = D.forward_paths(cl, h, lo_, idx, side)
                race = D.first_touch_race(cl, h, lo_, idx, side)
                fr = friction_bps(fp["entry_px"])
                for j, i in enumerate(idx):
                    if fp["n_bars"][j] < 1:
                        continue
                    rows.append({
                        "code": c, "day": dday, "split": _split_of(dday),
                        "time": b[i][1], "side": sname,
                        "bar_idx": int(i),
                        "cum_ret": float(cum_ret[i]), "vol_mult": float(vm[i]),
                        "entry_px": float(fp["entry_px"][j]),
                        "mfe_bps": float(fp["mfe_bps"][j]),
                        "mae_bps": float(fp["mae_bps"][j]),
                        "ret5_bps": float(fp["ret_bps"][4, j]),
                        "ret12_bps": float(fp["ret_bps"][11, j]),
                        "n_bars": int(fp["n_bars"][j]),
                        "race": int(race[j]),
                        "friction_bps": float(fr[j]),
                        "u1": "U1_newhigh_and_spike" in names,
                        "u2": "U2_spike_only" in names,
                        "u3": "U3_newhigh_only" in names,
                    })

        for rec in recs:
            d = rec[0]
            if d != cur_day:
                if cur_day is not None:
                    flush(cur_day, buf)
                cur_day, buf = d, []
            buf.append(rec)
        if cur_day is not None:
            flush(cur_day, buf)

        if ci % 100 == 0:
            print(f"  {ci}/{len(codes)} events={len(rows)} ({time.time() - t0:.0f}s)",
                  flush=True)

    if not rows:
        print("events=0")
        return
    cols = list(rows[0])
    tbl = pa.table({k: pa.array([r[k] for r in rows]) for k in cols})
    pq.write_table(tbl, EVENTS_PATH)
    print(f"\nevents={len(rows)} -> {EVENTS_PATH} ({time.time() - t0:.0f}s)")


def _fmt(name: str, m: np.ndarray, t) -> None:
    if m.sum() == 0:
        print(f"  {name:26s} n=0")
        return
    day = np.asarray(t["day"]).astype(str)[m]
    mfe = np.asarray(t["mfe_bps"], dtype=np.float64)[m]
    mae = np.asarray(t["mae_bps"], dtype=np.float64)[m]
    r12 = np.asarray(t["ret12_bps"], dtype=np.float64)[m]
    race = np.asarray(t["race"], dtype=np.int64)[m]
    fr = np.asarray(t["friction_bps"], dtype=np.float64)[m]
    n, dd = int(m.sum()), len(set(day.tolist()))
    gross = float(np.nanmean(r12))
    frm = float(np.nanmean(fr))
    net = gross - frm
    print(f"  {name:26s} n={n:6d} D={dd:3d} | gross(12bar)={gross:7.2f} "
          f"fric={frm:5.2f} net={net:7.2f} ratio={gross / frm if frm else 0:5.2f} | "
          f"MFE={np.nanmean(mfe):6.2f} MAE={np.nanmean(mae):7.2f} | "
          f"TP先={100 * (race == 1).mean():4.1f}% stop先={100 * (race == -1).mean():4.1f}%")


def cmd_report() -> None:
    t = pq.read_table(EVENTS_PATH)
    side = np.asarray(t["side"]).astype(str)
    split = np.asarray(t["split"]).astype(str)
    u1 = np.asarray(t["u1"]); u2 = np.asarray(t["u2"]); u3 = np.asarray(t["u3"])
    print(f"total events: {len(side)}\n")
    print("凡例: gross=12bar後の符号付きリターン平均 / TP先=+29.41bps先着 / stop先=-21bps先着")
    print("      (TP は close 基準・stop は髭基準・同一bar両成立は stop 優先 = 保守側)\n")
    for uname, um in (("U1 新高値AND代金急増", u1), ("U2 代金急増のみ", u2),
                      ("U3 新高値のみ", u3)):
        print(f"[{uname}]")
        for sname in ("short", "long"):
            for sp in ("train", "val"):
                _fmt(f"{sname:5s} {sp}", um & (side == sname) & (split == sp), t)
        print()


def cmd_null() -> None:
    """ADR-0001 G2 マッチドヌル: 同一 stock-day のランダム bar と比較する。

    「thrust 検出器はそもそも何かを選んでいるのか」を検定する。同じ銘柄・同じ日・
    同じ発火本数を、その日のランダムな bar に割り当てて同じ前方経路を測る。
    """
    rng = np.random.default_rng(20260717)
    t = pq.read_table(EVENTS_PATH)
    ev_code = np.asarray(t["code"]).astype(str)
    ev_day = np.asarray(t["day"]).astype(str)
    ev_side = np.asarray(t["side"]).astype(str)

    # (code, day, side) -> 発火本数
    want: dict[tuple[str, str, str], int] = {}
    for c, d, s in zip(ev_code, ev_day, ev_side):
        want[(c, d, s)] = want.get((c, d, s), 0) + 1

    codes = sorted({c for c, _, _ in want})
    rows: list[dict] = []
    t0 = time.time()
    for ci, c in enumerate(codes):
        recs = _load_minute(c)
        if recs is None:
            continue
        cur_day, buf = None, []

        def flush(dday: str, b: list) -> None:
            if len(b) < U.SPIKE_WINDOW + 5:
                return
            h = np.array([x[3] for x in b], dtype=np.float64)
            lo_ = np.array([x[4] for x in b], dtype=np.float64)
            cl = np.array([x[5] for x in b], dtype=np.float64)
            n = cl.size
            valid = np.arange(U.SPIKE_WINDOW, n - 2)
            if valid.size == 0:
                return
            for side, sname in ((D.SIDE_DOWN, "short"), (D.SIDE_UP, "long")):
                k = want.get((c, dday, sname), 0)
                if k == 0:
                    continue
                idx = rng.choice(valid, size=min(k, valid.size), replace=False)
                fp = D.forward_paths(cl, h, lo_, idx, side)
                race = D.first_touch_race(cl, h, lo_, idx, side)
                for j in range(idx.size):
                    if fp["n_bars"][j] < 1:
                        continue
                    rows.append({
                        "code": c, "day": dday, "side": sname,
                        "ret12_bps": float(fp["ret_bps"][11, j]),
                        "mfe_bps": float(fp["mfe_bps"][j]),
                        "mae_bps": float(fp["mae_bps"][j]),
                        "race": int(race[j]),
                    })

        for rec in recs:
            d = rec[0]
            if d != cur_day:
                if cur_day is not None:
                    flush(cur_day, buf)
                cur_day, buf = d, []
            buf.append(rec)
        if cur_day is not None:
            flush(cur_day, buf)
        if ci % 100 == 0:
            print(f"  {ci}/{len(codes)} null={len(rows)} ({time.time() - t0:.0f}s)", flush=True)

    nt = pa.table({k: pa.array([r[k] for r in rows]) for k in rows[0]})
    pq.write_table(nt, ART / "null_events.parquet")

    print("\n=== G2 マッチドヌル: thrust 発火 vs 同一 stock-day のランダム bar ===")
    n_side = np.asarray(nt["side"]).astype(str)
    for s in ("short", "long"):
        me, mn = (ev_side == s), (n_side == s)
        e_ret = np.asarray(t["ret12_bps"], dtype=np.float64)[me]
        n_ret = np.asarray(nt["ret12_bps"], dtype=np.float64)[mn]
        e_race = np.asarray(t["race"], dtype=np.int64)[me]
        n_race = np.asarray(nt["race"], dtype=np.int64)[mn]
        gap = np.nanmean(e_ret) - np.nanmean(n_ret)
        se = np.sqrt(np.nanvar(e_ret) / max(e_ret.size, 1)
                     + np.nanvar(n_ret) / max(n_ret.size, 1))
        z = gap / se if se > 0 else 0.0
        e_ev = (e_race == 1).mean() * D.TP_BPS - (e_race == -1).mean() * D.OWNER_MAE_BPS
        n_ev = (n_race == 1).mean() * D.TP_BPS - (n_race == -1).mean() * D.OWNER_MAE_BPS
        print(f"  {s:5s} thrust n={e_ret.size:5d} gross={np.nanmean(e_ret):+6.2f} | "
              f"random n={n_ret.size:5d} gross={np.nanmean(n_ret):+6.2f} | "
              f"GAP={gap:+6.2f} z={z:+5.2f}")
        print(f"        摩擦ゼロEV: thrust={e_ev:+6.2f}  random={n_ev:+6.2f}  "
              f"差={e_ev - n_ev:+6.2f} bps")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    {"universe": cmd_universe, "extract": cmd_extract, "report": cmd_report,
     "null": cmd_null}[cmd]()
