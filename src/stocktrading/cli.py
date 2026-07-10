from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from itertools import product

from .backtest import (
    BacktestResult,
    CostParams,
    Session,
    load_silver_ticks,
    prepare,
    run_prepared,
    write_gold_signals,
)
from .bronze import export_to_bronze
from .config import Settings, load_settings
from .medallion import ensure_medallion_dirs
from .signals import BASELINE, CHURN_CONTROLLED, SignalParams
from .silver import build_all_silver, build_silver

DEFAULT_SWEEP_SYMBOLS = "9984,285A,5803"
DEFAULT_COST = CostParams()


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _floats(value: str) -> list[float]:
    return [float(part) for part in value.split(",") if part.strip()]


def _symbols(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


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


def _signal_params(args: argparse.Namespace) -> SignalParams:
    return SignalParams(
        enter_threshold=args.enter_threshold,
        exit_threshold=args.exit_threshold,
        halflife_secs=args.halflife_secs,
        min_hold_secs=args.min_hold_secs,
        cooldown_secs=args.cooldown_secs,
    )


def _cost_params(args: argparse.Namespace) -> CostParams:
    return CostParams(
        commission_bps=args.commission_bps,
        lot=args.lot,
        fill_delay_ticks=args.fill_delay_ticks,
    )


def backtest_cmd(
    symbol: str,
    trade_date: date | None,
    signal_params: SignalParams,
    cost_params: CostParams,
    write_gold: bool,
) -> int:
    settings = load_settings()
    ensure_medallion_dirs(settings)
    ticks = load_silver_ticks(settings, symbol, trade_date)
    if not ticks:
        print(f"no silver ticks for {symbol} (date={trade_date}); build silver first")
        return 1

    result = run_prepared(prepare(ticks), symbol, signal_params, cost_params, trade_date)
    if write_gold:
        if trade_date:
            write_gold_signals(settings, symbol, ticks, signal_params, trade_date)
        else:
            # Gold is partitioned by date; a multi-day run has no single partition.
            print("note: gold not written (pass --date to write gold signals)")

    p = signal_params
    print(f"symbol           {result.symbol}")
    print(f"date             {result.trade_date or 'all'}")
    print(
        f"signal           enter={p.enter_threshold} exit={p.exit_threshold} "
        f"halflife={p.halflife_secs}s hold={p.min_hold_secs}s cooldown={p.cooldown_secs}s"
    )
    print(f"ticks            {result.n_ticks:,}")
    print(f"sessions         {result.n_sessions}")
    print(f"fills            {result.n_fills:,}")
    print(f"round trips      {result.n_round_trips:,}")
    print(f"avg hold         {result.avg_hold_secs:,.1f} s")
    print(f"max |position|   {result.max_abs_position} unit(s) x {cost_params.lot} sh")
    print(f"turnover         {result.turnover_yen:,.0f} JPY")
    print(f"gross PnL        {result.gross_pnl:,.0f} JPY")
    print(f"commission       {result.commission:,.0f} JPY")
    print(f"net PnL          {result.net_pnl:,.0f} JPY")
    return 0


@dataclass(frozen=True)
class SweepRow:
    params: SignalParams
    results: dict[str, BacktestResult]

    @property
    def net(self) -> float:
        return sum(r.net_pnl for r in self.results.values())

    @property
    def gross(self) -> float:
        return sum(r.gross_pnl for r in self.results.values())

    @property
    def fills(self) -> int:
        return sum(r.n_fills for r in self.results.values())

    @property
    def round_trips(self) -> int:
        return sum(r.n_round_trips for r in self.results.values())

    @property
    def net_per_trip(self) -> float:
        """Yen earned per completed round trip -- the sign that decides viability.

        Churn control can always drive `net` up to 0 by never trading. Only a
        positive net-per-trip means the signal actually pays for its own costs.
        """
        return self.net / self.round_trips if self.round_trips else 0.0

    @property
    def avg_hold_secs(self) -> float:
        held = sum(r.time_in_market_secs for r in self.results.values())
        return held / self.round_trips if self.round_trips else 0.0


def _grid(args: argparse.Namespace) -> tuple[list[SignalParams], int]:
    """Every valid corner of the parameter box, in a stable order.

    Combinations with exit > enter are not hysteresis, they are the opposite, and
    `SignalParams` rejects them. They are skipped rather than crashing the sweep,
    and counted so the caller can say so instead of silently shrinking the grid.
    """
    grid: list[SignalParams] = []
    skipped = 0
    for enter, exit_, halflife, hold, cooldown in product(
        args.enter_threshold,
        args.exit_threshold,
        args.halflife_secs,
        args.min_hold_secs,
        args.cooldown_secs,
    ):
        if exit_ > enter:
            skipped += 1
            continue
        grid.append(
            SignalParams(
                enter_threshold=enter,
                exit_threshold=exit_,
                halflife_secs=halflife,
                min_hold_secs=hold,
                cooldown_secs=cooldown,
            )
        )
    return grid, skipped


def _load_all(
    settings: Settings, symbols: list[str], trade_date: date | None
) -> dict[str, tuple[Session, ...]]:
    """Load and prepare each symbol once; every parameter set reuses the result."""
    loaded: dict[str, tuple[Session, ...]] = {}
    for symbol in symbols:
        ticks = load_silver_ticks(settings, symbol, trade_date)
        if not ticks:
            print(f"warning: no silver ticks for {symbol} (date={trade_date}); skipping")
            continue
        loaded[symbol] = prepare(ticks)
    return loaded


def _sweep_row(
    params: SignalParams,
    sessions_by_symbol: dict[str, tuple[Session, ...]],
    cost: CostParams,
    trade_date: date | None,
) -> SweepRow:
    return SweepRow(
        params=params,
        results={
            symbol: run_prepared(sessions, symbol, params, cost, trade_date)
            for symbol, sessions in sessions_by_symbol.items()
        },
    )


def _sweep_line(row: SweepRow, symbols: list[str], baseline: SweepRow, tag: str = "") -> str:
    p = row.params
    better = sum(
        1
        for s in symbols
        if s in row.results and row.results[s].net_pnl > baseline.results[s].net_pnl
    )
    cells = " ".join(
        f"{row.results[s].net_pnl:>12,.0f}" if s in row.results else f"{'-':>12}"
        for s in symbols
    )
    return (
        f"{p.enter_threshold:>6.2f} {p.exit_threshold:>6.2f} {p.halflife_secs:>7.2f} "
        f"{p.min_hold_secs:>6.1f} {p.cooldown_secs:>6.1f} "
        f"{row.fills:>9,} {row.round_trips:>8,} {row.gross:>12,.0f} {row.net:>12,.0f} "
        f"{row.net_per_trip:>9,.1f} {row.avg_hold_secs:>9,.1f}  {cells} "
        f"{better:>4}/{len(symbols)}{tag}"
    )


def _print_sweep(
    rows: list[SweepRow],
    symbols: list[str],
    baseline: SweepRow,
    top: int,
    min_round_trips: int,
) -> None:
    head = (
        f"{'enter':>6} {'exit':>6} {'half-l':>7} {'hold':>6} {'cool':>6} "
        f"{'fills':>9} {'trips':>8} {'gross':>12} {'net':>12} "
        f"{'net/trip':>9} {'avg hold':>9}  "
        + " ".join(f"{s:>12}" for s in symbols)
        + f" {'better':>7}"
    )
    print(head)
    print("-" * len(head))
    print(_sweep_line(baseline, symbols, baseline, tag="  <- baseline"))
    print("-" * len(head))
    for row in rows[:top]:
        print(_sweep_line(row, symbols, baseline))

    if not rows or rows[0].round_trips >= min_round_trips:
        return

    # Ranking by net rewards trading less, all the way down to a handful of
    # coin-flip trades whose net is pure noise. Whenever the winner is one of
    # those, surface the best row with a defensible sample alongside it so the
    # degenerate corner cannot be read as an edge.
    print("-" * len(head))
    sampled = [row for row in rows if row.round_trips >= min_round_trips]
    if sampled:
        tag = f"  <- best with >={min_round_trips} trips"
        print(_sweep_line(sampled[0], symbols, baseline, tag=tag))
    else:
        print(f"no configuration reached {min_round_trips:,} round trips")
    print(
        f"note: the top-ranked row took only {rows[0].round_trips} round trip(s). "
        f"net -> 0 by not trading; judge a signal on net/trip, not net."
    )


def sweep_cmd(args: argparse.Namespace) -> int:
    settings = load_settings()
    ensure_medallion_dirs(settings)
    trade_date = _parse_date(args.date)

    sessions_by_symbol = _load_all(settings, args.symbols, trade_date)
    if not sessions_by_symbol:
        print("no silver ticks for any requested symbol; build silver first")
        return 1
    symbols = list(sessions_by_symbol)

    cost = _cost_params(args)
    grid, skipped = _grid(args)
    if not grid:
        print("empty parameter grid")
        return 1
    print(
        f"sweep: {len(grid)} param sets x {len(symbols)} symbol(s) "
        f"= {len(grid) * len(symbols)} backtests "
        f"(date={trade_date or 'all'}, commission={cost.commission_bps}bps, "
        f"fill_delay={cost.fill_delay_ticks})"
    )
    if skipped:
        print(f"skipped {skipped} combination(s) where exit_threshold > enter_threshold")

    baseline = _sweep_row(BASELINE, sessions_by_symbol, cost, trade_date)
    rows = [_sweep_row(params, sessions_by_symbol, cost, trade_date) for params in grid]
    rows.sort(key=lambda r: r.net, reverse=True)
    _print_sweep(rows, symbols, baseline, args.top, args.min_round_trips)
    return 0


def _add_signal_args(parser: argparse.ArgumentParser, defaults: SignalParams) -> None:
    parser.add_argument("--enter-threshold", type=float, default=defaults.enter_threshold)
    parser.add_argument("--exit-threshold", type=float, default=defaults.exit_threshold)
    parser.add_argument("--halflife-secs", type=float, default=defaults.halflife_secs)
    parser.add_argument("--min-hold-secs", type=float, default=defaults.min_hold_secs)
    parser.add_argument("--cooldown-secs", type=float, default=defaults.cooldown_secs)


def _add_cost_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--commission-bps", type=float, default=DEFAULT_COST.commission_bps)
    parser.add_argument("--lot", type=int, default=DEFAULT_COST.lot)
    parser.add_argument("--fill-delay-ticks", type=int, default=DEFAULT_COST.fill_delay_ticks)


def main() -> int:
    parser = argparse.ArgumentParser(prog="stocktrading")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor")

    ingest = subparsers.add_parser("ingest-bronze")
    ingest.add_argument("--limit", type=int, default=None)

    silver = subparsers.add_parser("build-silver")
    silver.add_argument("--date", type=str, default=None)

    bt = subparsers.add_parser("backtest", help="run the signal on one symbol")
    bt.add_argument("--symbol", required=True)
    bt.add_argument("--date", type=str, default=None)
    _add_signal_args(bt, CHURN_CONTROLLED)
    _add_cost_args(bt)
    bt.add_argument("--no-gold", action="store_true", help="skip writing gold signals")

    sw = subparsers.add_parser("sweep", help="grid-search signal params across symbols")
    sw.add_argument("--symbols", type=_symbols, default=_symbols(DEFAULT_SWEEP_SYMBOLS))
    sw.add_argument("--date", type=str, default=None)
    sw.add_argument("--enter-threshold", type=_floats, default=[0.30])
    sw.add_argument("--exit-threshold", type=_floats, default=[0.30])
    sw.add_argument("--halflife-secs", type=_floats, default=[0.0])
    sw.add_argument("--min-hold-secs", type=_floats, default=[0.0])
    sw.add_argument("--cooldown-secs", type=_floats, default=[0.0])
    _add_cost_args(sw)
    sw.add_argument("--top", type=int, default=20)
    sw.add_argument(
        "--min-round-trips",
        type=int,
        default=100,
        help="sample floor for the 'best with >=N trips' row (default: 100)",
    )

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
            _signal_params(args),
            _cost_params(args),
            write_gold=not args.no_gold,
        )
    if args.command == "sweep":
        return sweep_cmd(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
