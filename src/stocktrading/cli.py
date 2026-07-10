from __future__ import annotations

import argparse
from datetime import date

from .backtest import CostParams, load_silver_ticks, run_backtest, write_gold_signals
from .bronze import export_to_bronze
from .config import load_settings
from .medallion import ensure_medallion_dirs
from .signals import SignalParams
from .silver import build_all_silver, build_silver


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def doctor() -> int:
    settings = load_settings()
    ensure_medallion_dirs(settings)

    checks = [
        ("backcast_root", settings.backcast_root, settings.backcast_root.exists()),
        ("board_source_root", settings.board_source_root, settings.board_source_root.exists()),
        ("medallion_root", settings.medallion_root, settings.medallion_root.exists()),
    ]
    for name, path, ok in checks:
        mark = "OK" if ok else "MISSING"
        print(f"{mark} {name}: {path}")
    return 0 if all(ok for _, _, ok in checks) else 1


def ingest_bronze(limit: int | None) -> int:
    settings = load_settings()
    ensure_medallion_dirs(settings)
    exports = export_to_bronze(settings, limit=limit)
    for export in exports:
        print(f"bronze {export.trade_date}: {export.rows} rows -> {export.target_dir}")
    print(f"exported {len(exports)} day(s)")
    return 0


def build_silver_cmd(trade_date: date | None) -> int:
    settings = load_settings()
    ensure_medallion_dirs(settings)
    builds = [build_silver(settings, trade_date)] if trade_date else build_all_silver(settings)
    for build in builds:
        print(f"silver {build.trade_date}: {build.rows} rows -> {build.target_dir}")
    print(f"built {len(builds)} day(s)")
    return 0


def backtest_cmd(
    symbol: str,
    trade_date: date | None,
    threshold: float,
    commission_bps: float,
    lot: int,
) -> int:
    settings = load_settings()
    ensure_medallion_dirs(settings)
    ticks = load_silver_ticks(settings, symbol, trade_date)
    if not ticks:
        print(f"no silver ticks for {symbol} (date={trade_date}); build silver first")
        return 1

    signal_params = SignalParams(threshold=threshold)
    cost_params = CostParams(commission_bps=commission_bps, lot=lot)
    result = run_backtest(ticks, symbol, signal_params, cost_params, trade_date=trade_date)

    if trade_date:
        write_gold_signals(settings, symbol, signal_params, trade_date)

    print(f"symbol           {result.symbol}")
    print(f"date             {result.trade_date or 'all'}")
    print(f"ticks            {result.n_ticks:,}")
    print(f"fills            {result.n_fills:,}")
    print(f"max |position|   {result.max_abs_position} unit(s) x {lot} sh")
    print(f"turnover         {result.turnover_yen:,.0f} JPY")
    print(f"gross PnL        {result.gross_pnl:,.0f} JPY")
    print(f"commission       {result.commission:,.0f} JPY")
    print(f"net PnL          {result.net_pnl:,.0f} JPY")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="stocktrading")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor")

    ingest = subparsers.add_parser("ingest-bronze")
    ingest.add_argument("--limit", type=int, default=None)

    silver = subparsers.add_parser("build-silver")
    silver.add_argument("--date", type=str, default=None)

    bt = subparsers.add_parser("backtest")
    bt.add_argument("--symbol", required=True)
    bt.add_argument("--date", type=str, default=None)
    bt.add_argument("--threshold", type=float, default=0.30)
    bt.add_argument("--commission-bps", type=float, default=1.5)
    bt.add_argument("--lot", type=int, default=100)

    args = parser.parse_args()
    if args.command == "doctor":
        return doctor()
    if args.command == "ingest-bronze":
        return ingest_bronze(args.limit)
    if args.command == "build-silver":
        return build_silver_cmd(_parse_date(args.date))
    if args.command == "backtest":
        return backtest_cmd(
            args.symbol,
            _parse_date(args.date),
            args.threshold,
            args.commission_bps,
            args.lot,
        )
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
