from __future__ import annotations

import csv
import gzip
import tempfile
import unittest
from pathlib import Path

from datetime import date

from universe import _daily_file_after_cutoff, select_universe


class DailyFileFilterTests(unittest.TestCase):
    """Perf prune (handoff §5a): skip daily files entirely after the PIT cutoff."""

    def test_month_token_after_cutoff_is_skipped(self) -> None:
        cutoff = date(2024, 1, 31)
        self.assertTrue(_daily_file_after_cutoff(Path("equities_bars_daily_202402.csv.gz"), cutoff))
        self.assertFalse(_daily_file_after_cutoff(Path("equities_bars_daily_202401.csv.gz"), cutoff))
        self.assertFalse(_daily_file_after_cutoff(Path("equities_bars_daily_202312.csv.gz"), cutoff))

    def test_day_token_boundary(self) -> None:
        cutoff = date(2024, 1, 31)
        self.assertTrue(_daily_file_after_cutoff(Path("equities_bars_daily_20240201.csv.gz"), cutoff))
        self.assertFalse(_daily_file_after_cutoff(Path("equities_bars_daily_20240131.csv.gz"), cutoff))

    def test_unrecognised_name_never_skipped(self) -> None:
        self.assertFalse(_daily_file_after_cutoff(Path("download_daily_bars.ps1"), date(2024, 1, 31)))


class FutureFileDoesNotAffectSelectionTests(unittest.TestCase):
    def test_future_month_file_is_ignored(self) -> None:
        """A future-month file (skipped by the filter) must not change results."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            base = UniverseTests()  # reuse row/series/write helpers
            rows = base._series("77770", days=5, close=1000, va=100000)
            rows += base._series("88880", days=5, close=1000, va=300000)
            base._write_daily(root / "equities_bars_daily_202401.csv.gz", rows)
            # A later month with a giant-Va row for 7777 that must be ignored.
            base._write_daily(
                root / "equities_bars_daily_202402.csv.gz",
                [base._row("2024-02-05", "77770", 1000, 99999999)],
            )
            sel = select_universe(
                as_of="2024-01-31", top_n=10, price_band=(700.0, 6000.0),
                va_window=5, cache_dir=root,
            )
            self.assertEqual(sel, ["8888", "7777"])


class UniverseTests(unittest.TestCase):
    def test_va_median_ranking_price_band_pit_and_topn(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rows: list[list[str]] = []

            # AAAA: highest Va, in price band -> rank 1
            rows += self._series("11110", days=5, close=1000, va=900000)
            # BBBB: mid Va, in price band -> rank 2
            rows += self._series("22220", days=5, close=2000, va=500000)
            # CCCC: low Va, in price band -> rank 3
            rows += self._series("33330", days=5, close=3000, va=100000)
            # DDDD: huge Va but price BELOW band (650 < 700) -> excluded
            rows += self._series("44440", days=5, close=650, va=9999999)
            # EEEE: huge Va but price ABOVE band (7000 > 6000) -> excluded
            rows += self._series("55550", days=5, close=7000, va=9999999)
            # FFFF: in band, big Va but only 3 days history -> below va_window -> excluded
            rows += self._series("66660", days=3, close=1500, va=8888888)

            self._write_daily(root / "equities_bars_daily_202401.csv.gz", rows)

            sel = select_universe(
                as_of="2024-01-31",
                top_n=2,
                price_band=(700.0, 6000.0),
                va_window=5,
                cache_dir=root,
            )
            # top_n=2 of the in-band, sufficient-history codes by Va median desc.
            self.assertEqual(sel, ["1111", "2222"])

            sel_all = select_universe(
                as_of="2024-01-31",
                top_n=100,
                price_band=(700.0, 6000.0),
                va_window=5,
                cache_dir=root,
            )
            self.assertEqual(sel_all, ["1111", "2222", "3333"])
            self.assertNotIn("4444", sel_all)  # below band
            self.assertNotIn("5555", sel_all)  # above band
            self.assertNotIn("6666", sel_all)  # insufficient history

    def test_point_in_time_ignores_future_rows(self) -> None:
        """Rows dated after as_of must not affect the Va ranking."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rows: list[list[str]] = []
            # 7777: 5 days at/before as_of with small Va, then a giant-Va future day.
            rows += self._series("77770", days=5, close=1000, va=100000, start_day=1)
            rows += [self._row("2024-02-05", "77770", 1000, 99999999)]  # future, ignored
            # 8888: 5 days at/before as_of with larger Va -> should outrank 7777.
            rows += self._series("88880", days=5, close=1000, va=300000, start_day=1)

            self._write_daily(root / "equities_bars_daily_202401.csv.gz", rows)

            sel = select_universe(
                as_of="2024-01-31",
                top_n=10,
                price_band=(700.0, 6000.0),
                va_window=5,
                cache_dir=root,
            )
            # If the future giant-Va row leaked, 7777 would rank first.
            self.assertEqual(sel, ["8888", "7777"])

    def test_tie_break_is_code_ascending(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rows: list[list[str]] = []
            rows += self._series("99990", days=5, close=1000, va=500000)
            rows += self._series("12340", days=5, close=1000, va=500000)
            self._write_daily(root / "equities_bars_daily_202401.csv.gz", rows)

            sel = select_universe(
                as_of="2024-01-31",
                top_n=10,
                price_band=(700.0, 6000.0),
                va_window=5,
                cache_dir=root,
            )
            # Equal Va median -> deterministic code-ascending order.
            self.assertEqual(sel, ["1234", "9999"])

    # --- helpers ---------------------------------------------------------

    def _series(
        self,
        code: str,
        *,
        days: int,
        close: float,
        va: float,
        start_day: int = 1,
    ) -> list[list[str]]:
        out = []
        for i in range(days):
            day = start_day + i
            out.append(self._row(f"2024-01-{day:02d}", code, close, va))
        return out

    @staticmethod
    def _row(date_str: str, code: str, close: float, va: float) -> list[str]:
        c = str(close)
        # Date,Code,O,H,L,C,UL,LL,Vo,Va,AdjFactor
        return [date_str, code, c, c, c, c, "", "", "1000", str(va), "1"]

    def _write_daily(self, path: Path, rows: list[list[str]]) -> None:
        with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                ["Date", "Code", "O", "H", "L", "C", "UL", "LL", "Vo", "Va", "AdjFactor"]
            )
            writer.writerows(rows)


if __name__ == "__main__":
    unittest.main()
