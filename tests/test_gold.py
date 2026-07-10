from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from stocktrading.backtest import Tick, signal_states, write_gold_signals
from stocktrading.config import Settings
from stocktrading.signals import SignalParams
from stocktrading.sql import sql_str

TRADE_DATE = date(2026, 7, 9)
T0 = datetime(2026, 7, 9, 9, 0, 0)
# hysteresis + smoothing + both gates: a rule no single SQL CASE can express
PARAMS = SignalParams(
    enter_threshold=0.30,
    exit_threshold=0.10,
    halflife_secs=2.0,
    min_hold_secs=1.0,
    cooldown_secs=1.0,
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        backcast_root=tmp_path / "backcast",
        board_source_root=tmp_path / "board",
        medallion_root=tmp_path / "data",
        market_timezone="Asia/Tokyo",
        session_open="09:00:00",
        session_close="15:30:00",
    )


def _ticks(imbalances: list[float | None]) -> list[Tick]:
    return [
        Tick(
            ts_event=T0 + timedelta(seconds=i * 0.5),
            bid_px=100.0,
            ask_px=101.0,
            mid=100.5,
            imbalance=imb,
        )
        for i, imb in enumerate(imbalances)
    ]


def _read(path: Path) -> list[tuple]:
    con = duckdb.connect()
    try:
        return con.execute(
            f"SELECT ts_event, imbalance, smoothed, target "
            f"FROM read_parquet({sql_str(path.as_posix())}) ORDER BY ts_event"
        ).fetchall()
    finally:
        con.close()


def test_gold_targets_match_the_backtest_fold(settings: Settings) -> None:
    ticks = _ticks([0.5, 0.4, 0.2, -0.05, -0.6, -0.6, 0.9, 0.1, 0.0, 0.0])
    path = write_gold_signals(settings, "9984", ticks, PARAMS, TRADE_DATE)

    rows = _read(path)
    expected = signal_states(ticks, PARAMS)
    assert [row[3] for row in rows] == [state.target for state in expected]
    assert [row[2] for row in rows] == pytest.approx([state.smoothed for state in expected])


def test_gold_round_trips_timestamps_and_nulls(settings: Settings) -> None:
    ticks = _ticks([0.5, None, 0.4])
    rows = _read(write_gold_signals(settings, "9984", ticks, PARAMS, TRADE_DATE))

    assert [row[0] for row in rows] == [t.ts_event for t in ticks]
    assert rows[1][1] is None  # the missing imbalance stays missing
    assert rows[1][2] == pytest.approx(0.5)  # ...and the smoothed mean is carried forward


def test_gold_lands_in_the_partitioned_path(settings: Settings) -> None:
    path = write_gold_signals(settings, "285A", _ticks([0.5]), PARAMS, TRADE_DATE)
    assert path == settings.gold_root / "date=2026-07-09" / "code=285A" / "signals.parquet"
    assert path.exists()


def test_gold_overwrites_a_previous_run(settings: Settings) -> None:
    write_gold_signals(settings, "9984", _ticks([0.5, 0.5, 0.5]), PARAMS, TRADE_DATE)
    path = write_gold_signals(settings, "9984", _ticks([0.5]), PARAMS, TRADE_DATE)
    assert len(_read(path)) == 1


def test_gold_handles_no_ticks(settings: Settings) -> None:
    path = write_gold_signals(settings, "9984", [], PARAMS, TRADE_DATE)
    assert _read(path) == []


def test_gold_path_with_a_quote_is_escaped_not_injected(settings: Settings) -> None:
    # `--symbol` reaches an inlined SQL literal; a bare apostrophe must not break out.
    path = write_gold_signals(settings, "a'b", _ticks([0.5]), PARAMS, TRADE_DATE)
    assert path.exists()
    assert len(_read(path)) == 1
