from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb

from .config import Settings
from .signals import SignalParams, imbalance_target


@dataclass(frozen=True)
class Tick:
    ts_event: object
    bid_px: float
    ask_px: float
    mid: float
    imbalance: float | None


@dataclass(frozen=True)
class CostParams:
    commission_bps: float = 1.5  # per-side commission on notional (basis points)
    lot: int = 100  # shares per position unit (1 単元)
    fill_delay_ticks: int = 1  # decide at tick t, fill at t+delay (models latency, no leakage)


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    trade_date: date | None
    n_ticks: int
    n_fills: int
    gross_pnl: float  # yen, spread cost included, commission excluded
    commission: float  # yen
    net_pnl: float  # yen, after commission
    turnover_yen: float
    max_abs_position: int


def load_silver_ticks(
    settings: Settings, symbol: str, trade_date: date | None = None
) -> list[Tick]:
    """Load one symbol's normalized ticks from silver, ordered by event time."""
    date_glob = f"date={trade_date.isoformat()}" if trade_date else "date=*"
    glob = (settings.silver_root / date_glob / f"code={symbol}" / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        rows = con.execute(
            "SELECT ts_event, bid_px, ask_px, mid, imbalance "
            "FROM read_parquet($glob) ORDER BY ts_event",
            {"glob": glob},
        ).fetchall()
    finally:
        con.close()
    return [Tick(*row) for row in rows]


def run_backtest(
    ticks: list[Tick],
    symbol: str,
    signal_params: SignalParams,
    cost_params: CostParams,
    trade_date: date | None = None,
) -> BacktestResult:
    """Event-driven order-book backtest for one symbol.

    - Target position each tick comes from the shared signal on that tick's info.
    - Fills execute `fill_delay_ticks` later at the OPPOSITE touch (buy@ask, sell@bid),
      so crossing the spread is paid explicitly and there is no same-snapshot leakage.
    - Commission is charged per side on notional.
    - Day-trade rule: forced flat on the final tick (no overnight position).
    """
    n = len(ticks)
    targets = [imbalance_target(t.imbalance, signal_params) for t in ticks]
    rate = cost_params.commission_bps / 10_000.0
    delay = cost_params.fill_delay_ticks

    position = 0  # in units
    cash = 0.0
    commission = 0.0
    turnover = 0.0
    fills = 0
    max_abs = 0

    for t in range(n):
        desired = targets[t - delay] if t - delay >= 0 else 0
        if t == n - 1:
            desired = 0  # force flat at session close
        if desired == position:
            continue

        delta = desired - position
        shares = abs(delta) * cost_params.lot
        px = ticks[t].ask_px if delta > 0 else ticks[t].bid_px
        notional = shares * px
        cash += -notional if delta > 0 else notional
        commission += notional * rate
        turnover += notional
        fills += 1
        position = desired
        max_abs = max(max_abs, abs(position))

    return BacktestResult(
        symbol=symbol,
        trade_date=trade_date,
        n_ticks=n,
        n_fills=fills,
        gross_pnl=cash,
        commission=commission,
        net_pnl=cash - commission,
        turnover_yen=turnover,
        max_abs_position=max_abs,
    )


def write_gold_signals(
    settings: Settings,
    symbol: str,
    signal_params: SignalParams,
    trade_date: date,
) -> Path:
    """Persist the per-tick target position to gold in a single SQL pass.

    The threshold->{-1,0,1} mapping mirrors `signals.imbalance_target` (kept as
    the authority for the backtest); the threshold value itself comes from
    `signal_params`, so gold and the backtest never use different cutoffs.
    """
    silver_glob = (
        settings.silver_root / f"date={trade_date.isoformat()}" / f"code={symbol}" / "*.parquet"
    ).as_posix()
    target_dir = settings.gold_root / f"date={trade_date.isoformat()}" / f"code={symbol}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "signals.parquet"

    theta = signal_params.threshold
    con = duckdb.connect()
    try:
        con.execute(
            f"COPY (SELECT ts_event, mid, bid_px, ask_px, imbalance, "
            f"CASE WHEN imbalance > {theta} THEN 1 "
            f"WHEN imbalance < {-theta} THEN -1 ELSE 0 END AS target "
            f"FROM read_parquet('{silver_glob}') ORDER BY ts_event) "
            f"TO '{target.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        con.close()
    return target
