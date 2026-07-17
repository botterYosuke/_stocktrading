"""板読み nightly retrain — DESIGN 決定 12 (毎営業日 point-in-time 再学習) の OPS 足場。

flow (docs/nightly-retrain-runbook.md が運用正本):
  1. RECORDING CHECK — 当日 T の録画健全性 (duckdb 存在・行数床・universe カバレッジ・
     heartbeat 最終時刻 >= 15:25)。不合格なら何も書かず EXIT 3 (前日 champion 継続)。
  2. AS-OF RETRAIN — レシピ (特徴量・LGBM パラメータ・セル h5s×m3.0) は凍結のまま、
     学習日窓だけを「T で終わる直近 N 日 (N = 凍結レシピと同数)」へスライド。
     OOS 封印日 (07-14) は絶対に使わない (daily_model.assert_daily_train_days)。
  3. TECHNICAL GATE — 書いたモデルの再ロード・meta ハッシュ一致・予測の非退化。
  4. PROMOTE — artifacts/calibration/champion.json を原子的更新。旧 daily は N=10 世代保持。

frozen shadow_h5_m30 (較正専用) には一切触れない。scripts/train_calibration_model.py の
挙動・成果物もそのまま (本スクリプトは同じ凍結定数・同じ dataset 関数を import して使う)。

exit codes: 0 = 昇格 / 3 = champion 継続 (回復可能な不成立すべて) / 1 = ハードエラー
usage: uv run python scripts/nightly_retrain.py [--date YYYY-MM-DD]
       (_bellwether daily_evening_pipeline STEP 4 が nightly_retrain.ps1 経由で毎夕呼ぶ)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import traceback
from datetime import date, datetime, time as dtime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
os.chdir(REPO)  # dataset.CACHE_DIR ("artifacts/cache/gen1") が相対パスのため

try:  # Windows console 既定 cp932 で日本語ログが化ける/落ちるのを回避 (runner.py と同じ)
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import duckdb  # noqa: E402
import lightgbm as lgb  # noqa: E402
import numpy as np  # noqa: E402

from scalp_agent import loader  # noqa: E402
from scalp_agent.config import (  # noqa: E402
    LGBM_NUM_BOOST_ROUND,
    LGBM_PARAMS,
    OOS_DAYS,
    assert_no_day_leakage,
    cell_key,
)
from scalp_agent.dataset import (  # noqa: E402
    _source_fingerprint,
    ensure_cache,
    load_cache,
    training_arrays,
)
from scalp_agent.runtime import calibration, daily_model  # noqa: E402

EXIT_PROMOTED = 0
EXIT_CONTINUE = 3  # champion 継続 (回復可能)
EXIT_HARD = 1

# ── 録画健全性の床 (完全営業日は 1.1M〜3.0M 行の実績) ──
ROW_FLOOR = 500_000
UNIVERSE_COVERAGE_MIN = 0.90
HEARTBEAT_MIN_TOD = dtime(15, 25)
UNIVERSE_PATH = REPO / "scripts" / "board_recorder_universe.txt"
GATE_SAMPLE_ROWS = 512


def log(msg: str) -> None:
    print(f"[nightly_retrain] {msg}", flush=True)


def read_universe_codes(path: Path) -> list[str]:
    codes = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        codes.append(line.replace(",", "\t").split("\t")[0].strip().upper())
    return codes


def db_row_stats(day: str) -> tuple[int, list[str]]:
    """(行数, distinct codes)。読めない (writer lock / WAL 残骸) は例外のまま上へ。"""
    with duckdb.connect(str(loader.db_path(day)), read_only=True) as con:
        con.execute("SET enable_progress_bar=false")
        n = con.execute("select count(*) from board_push").fetchone()[0]
        codes = [r[0] for r in con.execute(
            "select distinct code from board_push order by code").fetchall()]
    return int(n), codes


def recording_check(day: str) -> str | None:
    """day T の録画健全性。NG なら理由文字列 (None = 合格)。"""
    db = loader.db_path(day)
    if not db.exists():
        return f"録画 duckdb が無い: {db}"
    try:
        n_rows, codes = db_row_stats(day)
    except Exception as e:
        return f"録画 duckdb を読めない (録画プロセスが未終了/WAL 残骸の可能性): {e}"
    if n_rows < ROW_FLOOR:
        return f"行数が床未満: {n_rows:,} < {ROW_FLOOR:,} (部分録画/事故日)"
    universe = read_universe_codes(UNIVERSE_PATH)
    covered = set(codes) & set(universe)
    coverage = len(covered) / len(universe) if universe else 0.0
    if coverage < UNIVERSE_COVERAGE_MIN:
        missing = sorted(set(universe) - covered)
        return (f"universe カバレッジ不足: {len(covered)}/{len(universe)}"
                f" ({coverage:.0%} < {UNIVERSE_COVERAGE_MIN:.0%}) missing={missing[:10]}")
    hb = loader.SNAPSHOT_DIR / f"heartbeat_kabu_{day}.log"
    if not hb.exists():
        return f"heartbeat が無い: {hb}"
    try:
        last_line = hb.read_text(encoding="utf-8").strip().splitlines()[-1]
        last_ts = datetime.fromisoformat(last_line.split("\t")[0])
    except Exception as e:
        return f"heartbeat 末尾を解釈できない: {e}"
    if last_ts.date().isoformat() != day:
        return f"heartbeat 最終行の日付が {day} でない: {last_ts}"
    if last_ts.time() < HEARTBEAT_MIN_TOD:
        return (f"heartbeat 最終時刻 {last_ts.time()} < {HEARTBEAT_MIN_TOD}"
                " (大引けまで録画できていない)")
    log(f"recording check OK: rows={n_rows:,} codes={len(codes)} "
        f"coverage={len(covered)}/{len(universe)} heartbeat_last={last_ts}")
    return None


def eligible_train_days(day_t: str) -> list[str]:
    """as-of 窓の候補: 実録画あり・OOS 封印日でない・行数床を満たす日 (昇順)。"""
    out = []
    for d in loader.available_days():
        if d > day_t:
            continue
        if d in OOS_DAYS:
            log(f"  候補除外 {d}: OOS 封印日 (触れない)")
            continue
        try:
            n_rows, _ = db_row_stats(d)
        except Exception as e:
            log(f"  候補除外 {d}: 読めない ({e})")
            continue
        if n_rows < ROW_FLOOR:
            log(f"  候補除外 {d}: 行数床未満 ({n_rows:,})")
            continue
        out.append(d)
    return out


def git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
            text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def data_fingerprint(day: str) -> dict:
    """meta へ残す学習データの指紋 (dataset のキャッシュ整合と同じ規約)。"""
    if loader.db_path(day).exists():
        return _source_fingerprint(day)
    cache_dir = Path("artifacts/cache/gen1") / day
    parts = [(p.name, p.stat().st_size) for p in sorted(cache_dir.glob("*.parquet"))]
    return {"path": str(cache_dir), "source": "parquet_cache", "files": parts}


def load_training_tables(train_days: list[str]) -> dict:
    """train_calibration_model.py と同じ読み出し規約 (duckdb → 無ければ parquet cache)。"""
    tables = {}
    for day in train_days:
        if loader.db_path(day).exists():
            codes = loader.list_codes(day)
            get = ensure_cache
        else:
            cache_day_dir = Path("artifacts/cache/gen1") / day
            codes = sorted(p.stem for p in cache_day_dir.glob("*.parquet"))
            if not codes:
                raise SystemExit(f"{day}: 録画もキャッシュも見つからない")
            get = load_cache
            log(f"  {day}: S: 不在 — ローカル parquet cache から読む")
        for i, code in enumerate(codes, 1):
            tables[(day, code)] = get(day, code)
            if i % 10 == 0 or i == len(codes):
                log(f"  [{day}] {i}/{len(codes)} {code}")
    return tables


def technical_gate(out_dir: Path, x_sample: np.ndarray) -> str | None:
    """書いた成果物だけを使う再ロード検査。NG なら理由 (None = 合格)。"""
    model_path = out_dir / "model.txt"
    meta_path = out_dir / "meta.json"
    try:
        booster = lgb.Booster(model_file=str(model_path))
    except Exception as e:
        return f"model.txt を lightgbm で再ロードできない: {e}"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as e:
        return f"meta.json を読めない: {e}"
    expected = daily_model.champion_meta_expected()
    for k, v in expected.items():
        if meta.get(k) != v:
            return f"meta 不一致: {k}: saved={meta.get(k)} expected={v}"
    if len(x_sample) == 0:
        return "gate 用サンプルが空"
    preds = booster.predict(x_sample[:GATE_SAMPLE_ROWS])
    preds = np.asarray(preds)
    if preds.ndim != 2 or preds.shape[1] != 3:
        return f"予測 shape が異常: {preds.shape}"
    if not np.isfinite(preds).all():
        return "予測に NaN/inf が含まれる"
    if len(preds) > 1 and float(np.ptp(preds, axis=0).max()) < 1e-9:
        return "予測が全行定数 (退化モデル)"
    log(f"technical gate OK: reload+meta+preds n={len(preds)} "
        f"pred_spread={float(np.ptp(preds, axis=0).max()):.4f}")
    return None


def promote(out_dir: Path, train_days: list[str]) -> None:
    """champion.json を原子的に更新する (tmp → os.replace)。"""
    model_sha = daily_model.sha256_file(out_dir / "model.txt")
    prev = None
    if daily_model.CHAMPION_PATH.exists():
        try:
            old = json.loads(daily_model.CHAMPION_PATH.read_text(encoding="utf-8"))
            prev = {k: old.get(k) for k in
                    ("model_dir", "model_sha256", "promoted_at", "train_days")}
        except Exception:
            prev = None
    pointer = {
        "model_dir": out_dir.relative_to(REPO).as_posix(),
        "model_sha256": model_sha,
        "promoted_at": datetime.now().isoformat(timespec="seconds"),
        "train_days": train_days,
        "previous": prev,
    }
    tmp = daily_model.CHAMPION_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(pointer, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, daily_model.CHAMPION_PATH)
    log(f"champion 昇格: {pointer['model_dir']} (sha256={model_sha[:12]})")


def prune_dailies() -> None:
    """daily モデルを新しい順に KEEP_DAILY 世代残し、古いものを削除する。

    現 champion / previous が指す dir は世代数に関わらず削除しない。
    """
    if not daily_model.DAILY_ROOT.exists():
        return
    protected = set()
    if daily_model.CHAMPION_PATH.exists():
        try:
            ptr = json.loads(daily_model.CHAMPION_PATH.read_text(encoding="utf-8"))
            for p in (ptr, ptr.get("previous") or {}):
                if p.get("model_dir"):
                    protected.add((REPO / p["model_dir"]).resolve())
        except Exception:
            pass
    dirs = sorted(d for d in daily_model.DAILY_ROOT.iterdir() if d.is_dir())
    if len(dirs) <= daily_model.KEEP_DAILY:
        return
    for d in dirs[:-daily_model.KEEP_DAILY]:
        if d.resolve() in protected:
            continue
        shutil.rmtree(d, ignore_errors=True)
        log(f"prune: {d.name} を削除 (保持 {daily_model.KEEP_DAILY} 世代)")


def run(day_t: str) -> int:
    log(f"day T = {day_t} / BOARD_PUSH_DIR = {loader.SNAPSHOT_DIR}")

    # 1. RECORDING CHECK
    reason = recording_check(day_t)
    if reason is not None:
        log(f"EXIT 3 (champion 継続): recording check NG — {reason}")
        return EXIT_CONTINUE

    # 2. AS-OF 窓の選定 (OOS 封印はここで絶対に守る)
    assert_no_day_leakage()
    if day_t in OOS_DAYS:
        log(f"EXIT 3 (champion 継続): {day_t} は OOS 封印日 — role guard により学習禁止")
        return EXIT_CONTINUE
    eligible = eligible_train_days(day_t)
    try:
        train_days = daily_model.select_train_days(day_t, eligible)
    except (ValueError, AssertionError) as e:
        log(f"EXIT 3 (champion 継続): as-of 窓を組めない — {e}")
        return EXIT_CONTINUE
    log(f"as-of train_days = {train_days} "
        f"(N={daily_model.N_TRAIN_DAYS}, 凍結レシピと同数・窓のみスライド)")

    # 3. 学習 (レシピは calibration の凍結定数をそのまま使う)
    tables = load_training_tables(train_days)
    x, y = training_arrays(tables, calibration.CAL_HORIZON_S, calibration.CAL_MULT)
    if len(y) < 1000 or len(np.unique(y)) < 3:
        log(f"EXIT 3 (champion 継続): 退化した学習集合 (n={len(y)}, "
            f"classes={len(np.unique(y))})")
        return EXIT_CONTINUE
    log(f"training: cell={cell_key(calibration.CAL_HORIZON_S, calibration.CAL_MULT)} "
        f"n={len(y)} class_counts={np.bincount(y).tolist()}")
    ds = lgb.Dataset(x, label=y, params={"max_bin": LGBM_PARAMS["max_bin"]})
    booster = lgb.train(LGBM_PARAMS, ds, num_boost_round=LGBM_NUM_BOOST_ROUND)

    # 4. challenger の書き出し (frozen shadow には触れない)
    out_dir = daily_model.DAILY_ROOT / day_t.replace("-", "")
    out_dir.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(out_dir / "model.txt"))
    meta = {
        **calibration.model_meta(),  # 同スキーマ (horizon/mult/tau/hash 類)
        **daily_model.DAILY_TAGS,    # policy/purpose を daily_champion へ上書き
        "train_days": train_days,    # スライド窓 (shadow の凍結日を上書き)
        "n_train_rows": int(len(y)),
        "data_hashes": {d: data_fingerprint(d) for d in train_days},
        "git_sha": git_sha(),
        "trained_at": datetime.now().isoformat(timespec="seconds"),
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"challenger 書き出し: {out_dir}")

    # 5. TECHNICAL GATE → 昇格 or 継続
    reason = technical_gate(out_dir, x)
    if reason is not None:
        log(f"EXIT 3 (champion 継続): technical gate NG — {reason}")
        return EXIT_CONTINUE
    promote(out_dir, train_days)
    prune_dailies()
    log("NOTE: daily champion も paper 出力は較正データ扱い。判定・台帳・セル選択に使わない。")
    return EXIT_PROMOTED


def main() -> int:
    ap = argparse.ArgumentParser(
        description="板読み nightly retrain (DESIGN 決定 12 OPS。exit 0=昇格/3=継続/1=hard)")
    ap.add_argument("--date", default=date.today().isoformat(),
                    help="day T (YYYY-MM-DD, 既定=今日)")
    args = ap.parse_args()
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        log(f"--date の形式が不正: {args.date}")
        return EXIT_HARD
    try:
        return run(args.date)
    except SystemExit as e:
        raise e
    except Exception:
        traceback.print_exc()
        log("EXIT 1 (ハードエラー)")
        return EXIT_HARD


if __name__ == "__main__":
    raise SystemExit(main())
