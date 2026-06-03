"""パラメータスイープを順番に実行し、run_id を stdout に出力する。

e-station ルートから実行すること:
    cd ../e-station
    uv run python "../🐃_sacrificial-lamb/.claude/skills/sacrificial-lamb-replay/scripts/sweep_runner.py" \
        --strategy "../🐃_sacrificial-lamb/strategies/mean_reversion_01.py" \
        --params "window=5,10,20" \
        --params "k=1.0,1.5,2.0" \
        --params "holding_minutes=30"

完了した run_id は stdout に 1 行ずつ出力される（パイプで ingest_run.py に渡せる）。
失敗したセルは stderr に WARN を出して次のセルへ続行する。
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def parse_param_spec(specs: list[str]) -> dict[str, list[str]]:
    """["window=5,10,20", "k=1.0,1.5"] → {"window": ["5", "10", "20"], "k": ["1.0", "1.5"]}"""
    result: dict[str, list[str]] = {}
    for spec in specs:
        if "=" not in spec:
            raise ValueError(f"Invalid param spec (expected key=v1,v2,...): {spec!r}")
        key, vals = spec.split("=", 1)
        result[key.strip()] = [v.strip() for v in vals.split(",")]
    return result


def build_cells(params: dict[str, list[str]]) -> list[dict[str, str]]:
    keys = list(params.keys())
    combos = list(itertools.product(*params.values()))
    return [dict(zip(keys, combo)) for combo in combos]


def find_latest_run_id(
    run_buffer: Path, strategy_stem: str, before_ts: float
) -> str | None:
    """スイープ 1 セル実行後に追加された最新 run_id を特定する。"""
    candidates = []
    for d in run_buffer.iterdir():
        if not d.is_dir():
            continue
        try:
            ts = int(d.name.split("-")[0])
        except ValueError:
            continue
        if ts >= before_ts and strategy_stem in d.name:
            candidates.append((ts, d.name))
    if not candidates:
        return None
    return sorted(candidates, reverse=True)[0][1]


def run_replay(
    strategy: Path, cell: dict[str, str], run_buffer: Path, timeout_sec: int = 1800
) -> str | None:
    """1 セルを replay 実行し、完成した run_id を返す。失敗時は None。"""
    env = os.environ.copy()
    for k, v in cell.items():
        env[f"STRATEGY_PARAM_{k.upper()}"] = v

    cmd = [
        "uv",
        "run",
        "python",
        "-m",
        "engine.replay_session",
        "run",
        "--strategy",
        str(strategy),
        "--mode",
        "inprocess",
    ]
    for k, v in cell.items():
        cmd += [f"--param", f"{k}={v}"]

    ts_before = time.time()
    print(f"  running: {cell}", file=sys.stderr)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        print(f"  WARN: timeout for {cell}", file=sys.stderr)
        return None

    if result.returncode != 0:
        print(f"  WARN: exit {result.returncode} for {cell}", file=sys.stderr)
        print(result.stderr[-500:] if result.stderr else "", file=sys.stderr)
        return None

    strategy_stem = strategy.stem
    run_id = find_latest_run_id(run_buffer, strategy_stem, ts_before)
    if run_id is None:
        print(f"  WARN: could not find run_id for {cell}", file=sys.stderr)
    return run_id


def default_run_buffer() -> Path:
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / "flowsurface" / "run-buffer"
    elif sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "flowsurface"
            / "run-buffer"
        )
    else:
        return Path.home() / ".local" / "share" / "flowsurface" / "run-buffer"


def main() -> None:
    parser = argparse.ArgumentParser(description="パラメータスイープ実行")
    parser.add_argument("--strategy", required=True, help="戦略ファイルのパス")
    parser.add_argument(
        "--params",
        action="append",
        default=[],
        metavar="KEY=V1,V2,...",
        help="パラメータ仕様（複数指定可）",
    )
    parser.add_argument(
        "--output-dir", default=None, help="run-buffer ディレクトリ（省略時は自動検出）"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="1 セルあたりのタイムアウト秒（default: 1800 = 30 min）",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="セル数だけ表示してリプレイは実行しない"
    )
    args = parser.parse_args()

    strategy = Path(args.strategy).resolve()
    if not strategy.exists():
        print(f"ERROR: strategy file not found: {strategy}", file=sys.stderr)
        sys.exit(1)

    run_buffer = Path(args.output_dir) if args.output_dir else default_run_buffer()

    params = parse_param_spec(args.params)
    cells = build_cells(params)

    print(f"スイープセル数: {len(cells)}", file=sys.stderr)
    if args.dry_run:
        for i, cell in enumerate(cells, 1):
            print(f"  [{i:2d}] {cell}", file=sys.stderr)
        return

    run_ids: list[str] = []
    failed = 0
    for i, cell in enumerate(cells, 1):
        print(f"\n[{i}/{len(cells)}] {cell}", file=sys.stderr)
        run_id = run_replay(strategy, cell, run_buffer, timeout_sec=args.timeout)
        if run_id:
            run_ids.append(run_id)
            print(run_id)  # stdout: パイプ用
        else:
            failed += 1

    print(
        f"\n完了: {len(run_ids)}/{len(cells)} 成功  (失敗: {failed})", file=sys.stderr
    )

    if run_ids:
        sweep_file = Path("sweep_run_ids.txt")
        sweep_file.write_text("\n".join(run_ids) + "\n", encoding="utf-8")
        print(f"run_id リスト → {sweep_file}", file=sys.stderr)


if __name__ == "__main__":
    main()
