from __future__ import annotations

import csv
import gzip
import tempfile
import unittest
from pathlib import Path

from data_source import code_to_symbol, load_daily_bars, newest_close_as_of


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

    def _write_daily(self, path: Path, rows: list[list[str]]) -> None:
        with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["Date", "Code", "O", "H", "L", "C", "UL", "LL", "Vo", "Va", "AdjFactor"])
            writer.writerows(rows)


if __name__ == "__main__":
    unittest.main()
