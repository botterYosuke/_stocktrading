from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from itertools import groupby
from pathlib import Path
from tempfile import TemporaryDirectory

import duckdb

from .config import Settings
from .signals import Observation, SignalParams, SignalState, fold_states
from .sql import sql_str


@dataclass(frozen=True, slots=True)
class Tick:
    ts_event: datetime
    bid_px: float
    ask_px: float
    mid: float
    imbalance: float | None


@dataclass(frozen=True, slots=True)
class Session:
    """One trading day's ticks, with the signal clock precomputed.

    Seconds run from the session's own first tick rather than the epoch, so the
    signal never depends on the timezone of a naive TIMESTAMP -- only differences
    matter -- and the overnight gap can never leak into a hold or cooldown.
    """

    ticks: tuple[Tick, ...]
    elapsed: tuple[float, ...]

    @property
    def observations(self) -> Iterator[Observation]:
        return zip(self.elapsed, (tick.imbalance for tick in self.ticks))


def prepare(ticks: Sequence[Tick]) -> tuple[Session, ...]:
    """Split ticks into per-day sessions and precompute each one's clock.

    Doing this once per symbol keeps the sweep from redoing the same datetime
    arithmetic for every parameter set.
    """
    sessions: list[Session] = []
    for _, day in groupby(ticks, key=lambda tick: tick.ts_event.date()):
        day_ticks = tuple(day)
        origin = day_ticks[0].ts_event
        elapsed = tuple((tick.ts_event - origin).total_seconds() for tick in day_ticks)
        sessions.append(Session(ticks=day_ticks, elapsed=elapsed))
    return tuple(sessions)


@dataclass(frozen=True)
class CostParams:
    commission_bps: float = 1.5  # per-side commission on notional (basis points)
    lot: int = 100  # shares per position unit (1 単元)
    fill_delay_ticks: int = 1  # decide at tick t, fill at t+delay (models latency, no leakage)

    def __post_init__(self) -> None:
        if self.commission_bps < 0.0:
            raise ValueError(f"commission_bps must be >= 0, got {self.commission_bps}")
        if self.lot <= 0:
            raise ValueError(f"lot must be > 0, got {self.lot}")
        if self.fill_delay_ticks < 0:
            raise ValueError(f"fill_delay_ticks must be >= 0, got {self.fill_delay_ticks}")


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
    n_sessions: int
    n_round_trips: int  # positions opened and subsequently closed or flipped
    time_in_market_secs: float

    @property
    def avg_hold_secs(self) -> float:
        return self.time_in_market_secs / self.n_round_trips if self.n_round_trips else 0.0


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


def signal_states(ticks: Sequence[Tick], signal_params: SignalParams) -> list[SignalState]:
    """Signal state after each tick, restarting the state machine every session."""
    states: list[SignalState] = []
    for session in prepare(ticks):
        states.extend(fold_states(session.observations, signal_params))
    return states


def run_prepared(
    sessions: Sequence[Session],
    symbol: str,
    signal_params: SignalParams,
    cost_params: CostParams,
    trade_date: date | None = None,
) -> BacktestResult:
    """Event-driven order-book backtest over already-prepared sessions.

    - Target position each tick comes from the shared signal state machine, which
      sees only information available at that tick.
    - Fills execute `fill_delay_ticks` later at the OPPOSITE touch (buy@ask, sell@bid),
      so crossing the spread is paid explicitly and there is no same-snapshot leakage.
    - Commission is charged per side on notional.
    - Day-trade rule: every session is independent. The position is forced flat on
      each session's final tick (this overrides `min_hold_secs`; the bell does not
      negotiate), and the signal, the fill-delay window and the clock all restart
      the next morning. Nothing survives the overnight gap.
    """
    rate = cost_params.commission_bps / 10_000.0
    delay = cost_params.fill_delay_ticks

    n_ticks = 0
    cash = 0.0
    commission = 0.0
    turnover = 0.0
    fills = 0
    max_abs = 0
    round_trips = 0
    time_in_market = 0.0

    for session in sessions:
        ticks = session.ticks
        elapsed = session.elapsed
        n = len(ticks)
        n_ticks += n
        targets = [state.target for state in fold_states(session.observations, signal_params)]

        position = 0  # in units; always 0 again by the end of the session
        entry_ts = 0.0

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

            if position != 0:
                time_in_market += elapsed[t] - entry_ts
                round_trips += 1
            if desired != 0:
                entry_ts = elapsed[t]

            position = desired
            max_abs = max(max_abs, abs(position))

    return BacktestResult(
        symbol=symbol,
        trade_date=trade_date,
        n_ticks=n_ticks,
        n_fills=fills,
        gross_pnl=cash,
        commission=commission,
        net_pnl=cash - commission,
        turnover_yen=turnover,
        max_abs_position=max_abs,
        n_sessions=len(sessions),
        n_round_trips=round_trips,
        time_in_market_secs=time_in_market,
    )


def run_backtest(
    ticks: Sequence[Tick],
    symbol: str,
    signal_params: SignalParams,
    cost_params: CostParams,
    trade_date: date | None = None,
) -> BacktestResult:
    """Convenience wrapper: split `ticks` into sessions, then run. See `run_prepared`."""
    return run_prepared(prepare(ticks), symbol, signal_params, cost_params, trade_date)


GOLD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("ts_event", "TIMESTAMP"),
    ("bid_px", "DOUBLE"),
    ("ask_px", "DOUBLE"),
    ("mid", "DOUBLE"),
    ("imbalance", "DOUBLE"),
    ("smoothed", "DOUBLE"),
    ("target", "INTEGER"),
)


def _csv_cell(value: object) -> str:
    if value is None:
        return ""  # DuckDB reads an empty field as NULL
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    return repr(value)  # float repr round-trips exactly


def write_gold_signals(
    settings: Settings,
    symbol: str,
    ticks: Sequence[Tick],
    signal_params: SignalParams,
    trade_date: date,
) -> Path:
    """Persist the per-tick smoothed imbalance and target position to gold.

    The targets come from the same fold the backtest runs, so gold cannot encode a
    different rule than the one that was measured. (The old SQL `CASE` could, and
    with hysteresis and hold gates it can no longer express the rule at all.)

    Rows are staged through a CSV rather than handed to DuckDB as Python values:
    both `executemany` and list-valued prepared parameters convert row-by-row and
    cost ~10 minutes for a single 93k-tick day, while the vectorized CSV reader
    takes well under a second.
    """
    target_dir = settings.gold_root / f"date={trade_date.isoformat()}" / f"code={symbol}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "signals.parquet"

    states = signal_states(ticks, signal_params)
    columns = "{" + ", ".join(f"'{name}': '{sql}'" for name, sql in GOLD_COLUMNS) + "}"

    with TemporaryDirectory() as tmp:
        staging = Path(tmp) / "signals.csv"
        with staging.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(",".join(name for name, _ in GOLD_COLUMNS) + "\n")
            for tick, state in zip(ticks, states, strict=True):
                cells = (
                    tick.ts_event,
                    tick.bid_px,
                    tick.ask_px,
                    tick.mid,
                    tick.imbalance,
                    state.smoothed,
                    state.target,
                )
                handle.write(",".join(_csv_cell(cell) for cell in cells) + "\n")

        con = duckdb.connect()
        try:
            # Values are inlined: DuckDB cannot prepare parameters inside COPY.
            con.execute(
                f"COPY (SELECT * FROM read_csv({sql_str(staging.as_posix())}, "
                f"header = true, columns = {columns})) "
                f"TO {sql_str(target.as_posix())} (FORMAT PARQUET)"
            )
        finally:
            con.close()
    return target
