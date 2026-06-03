from __future__ import annotations

import csv
import gzip
import tempfile
import unittest
from datetime import date
from pathlib import Path

from data_source import (
    DailyBar,
    code_to_symbol,
    load_daily_bars,
    newest_close_as_of,
    select_pit_bars,
)


class DataSourceTests(unittest.TestCase):
    def test_load_daily_bars_filters_and_deduplicates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_daily(
                root / "equities_bars_daily_202601.csv.gz",
                [
                    ["2026-01-05", "72030", "100", "110", "90", "105", "", "", "1000", "100000", "1"],
                    ["2026-01-06", "72030", "105", "115", "95", "108", "", "", "2000", "200000", "1"],
                ],
            )
            self._write_daily(
                root / "equities_bars_daily_20260106.csv.gz",
                [
                    ["2026-01-06", "72030", "999", "999", "999", "999", "", "", "1", "1", "1"],
                ],
            )

            bars = load_daily_bars(cache_dir=root, start="2026-01-06", end="2026-01-06")

            self.assertEqual(list(bars), ["7203"])
            self.assertEqual(len(bars["7203"]), 1)
            self.assertEqual(bars["7203"][0].close, 108.0)
            self.assertEqual(newest_close_as_of(bars, "72030", "2026-01-06"), 108.0)

    def test_code_to_symbol_normalizes_five_digit_jquants_code(self) -> None:
        self.assertEqual(code_to_symbol("13060"), "1306.TSE")

    def test_select_pit_bars_no_lookahead_and_window(self) -> None:
        """B2-6: no returned bar is dated after as_of; window + min-rows enforced."""

        def mk(code: str, day: int, close: float) -> DailyBar:
            d = date(2026, 1, day)
            return DailyBar(date=d, code=code, open=close, high=close, low=close, close=close, volume=1)

        bars = {
            # 10 bars on/before as_of (01-01..01-10) plus a FUTURE bar (01-15)
            "7203": [mk("7203", day, 100 + day) for day in range(1, 11)] + [mk("7203", 15, 999.0)],
            # only 2 bars at/before as_of -> below train_window, must be dropped
            "6758": [mk("6758", 9, 1.0), mk("6758", 10, 2.0)],
        }

        out = select_pit_bars(bars, "2026-01-10", train_window=5)

        self.assertIn("7203", out)
        self.assertEqual(len(out["7203"]), 5)
        self.assertTrue(all(b.date <= date(2026, 1, 10) for b in out["7203"]))  # no look-ahead
        self.assertEqual(out["7203"][0].date, date(2026, 1, 6))  # tail(5) = 06..10
        self.assertEqual(out["7203"][-1].date, date(2026, 1, 10))
        self.assertNotIn("6758", out)  # insufficient history excluded

    def _write_daily(self, path: Path, rows: list[list[str]]) -> None:
        with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["Date", "Code", "O", "H", "L", "C", "UL", "LL", "Vo", "Va", "AdjFactor"])
            writer.writerows(rows)


if __name__ == "__main__":
    unittest.main()
