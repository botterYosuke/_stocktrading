"""Silver/runs を横断比較して収益ランキングを出力する。

Usage:
    uv run python .claude/skills/sacrificial-lamb-replay/scripts/compare_runs.py \
        --silver Silver/runs \
        [--top 10] \
        [--strategy-filter mean_reversion_01] \
        [--sort total_pnl|win_rate|trade_count]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_summaries(silver_dir: Path, strategy_filter: str | None) -> list[dict]:
    rows = []
    for run_dir in silver_dir.iterdir():
        if not run_dir.is_dir():
            continue
        summary_path = run_dir / "summary.json"
        if not summary_path.exists():
            continue
        run_id = run_dir.name
        if strategy_filter and strategy_filter not in run_id:
            continue
        try:
            data = json.loads(summary_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print(f"WARNING: skip {run_id} (unreadable summary.json)", file=sys.stderr)
            continue
        rows.append({"run_id": run_id, **data})
    return rows


def format_table(rows: list[dict], top: int) -> str:
    if not rows:
        return "結果なし (Silver/runs が空か strategy-filter に一致するランがない)"

    header = f"{'rank':<5} {'run_id':<52} {'total_pnl':>10} {'max_dd':>10} {'win_rate':>9} {'trades':>7} {'fees':>8}"
    sep = "-" * len(header)
    lines = [header, sep]

    for i, r in enumerate(rows[:top], 1):
        win = r.get("win_rate")
        win_str = f"{win:.2f}" if win is not None else "  n/a"
        lines.append(
            f"{i:<5} {r['run_id']:<52} "
            f"{r.get('total_pnl', 0):>10.0f} "
            f"{r.get('max_drawdown', 0):>10.0f} "
            f"{win_str:>9} "
            f"{r.get('trade_count', 0):>7} "
            f"{r.get('fee_total', 0):>8.0f}"
        )

    best = rows[0]
    lines += [
        sep,
        f"ベスト: {best['run_id']}",
        f"  total_pnl={best.get('total_pnl', 0):.0f}  max_drawdown={best.get('max_drawdown', 0):.0f}  "
        f"win_rate={best.get('win_rate')!r}  trades={best.get('trade_count', 0)}",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Silver 横断比較")
    parser.add_argument(
        "--silver", default="Silver/runs", help="Silver/runs ディレクトリ"
    )
    parser.add_argument("--top", type=int, default=10, help="上位 N 件を表示")
    parser.add_argument(
        "--strategy-filter", default=None, help="run_id に含まれる文字列でフィルタ"
    )
    parser.add_argument(
        "--sort",
        choices=["total_pnl", "win_rate", "trade_count"],
        default="total_pnl",
        help="ソートキー",
    )
    parser.add_argument("--json", action="store_true", help="JSON で出力（パイプ用）")
    args = parser.parse_args()

    silver_dir = Path(args.silver)
    if not silver_dir.exists():
        print(f"ERROR: {silver_dir} が見つからない", file=sys.stderr)
        sys.exit(1)

    rows = load_summaries(silver_dir, args.strategy_filter)

    def sort_key(r: dict) -> float:
        v = r.get(args.sort)
        if v is None:
            return float("-inf")
        return float(v)

    rows.sort(key=sort_key, reverse=True)

    if args.json:
        json.dump(rows[: args.top], sys.stdout, ensure_ascii=False, indent=2)
    else:
        print(format_table(rows, args.top))


if __name__ == "__main__":
    main()
