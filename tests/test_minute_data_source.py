from __future__ import annotations

import csv
import gzip
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from minute_data_source import (
    MinuteBar,
    load_minute_bars,
    resample_15min,
)


class MinuteDataSourceTests(unittest.TestCase):
    def test_load_minute_bars_first_wins_dedup(self) -> None:
        """Monthly + per-day fragment overlap: first occurrence wins."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Monthly file (sorts first by name) holds the authoritative row.
            self._write_minute(
                root / "equities_bars_minute_202401.csv.gz",
                [
                    ["2024-01-31", "09:00", "72030", "100", "110", "90", "105", "1000", "100000"],
                    ["2024-01-31", "09:01", "72030", "105", "115", "95", "108", "2000", "200000"],
                ],
            )
            # Per-day fragment has a CONFLICTING duplicate for 09:00.
            self._write_minute(
                root / "equities_bars_minute_20240131.csv.gz",
                [
                    ["2024-01-31", "09:00", "72030", "999", "999", "999", "999", "9", "9"],
                ],
            )

            bars = load_minute_bars(cache_dir=root)

            self.assertEqual(list(bars), ["7203"])
            self.assertEqual(len(bars["7203"]), 2)
            # first-wins: monthly 09:00 row (close 105), not fragment (999)
            self.assertEqual(bars["7203"][0].timestamp, datetime(2024, 1, 31, 9, 0))
            self.assertEqual(bars["7203"][0].close, 105.0)
            self.assertEqual(bars["7203"][1].close, 108.0)

    def test_load_minute_bars_date_and_code_filter(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_minute(
                root / "equities_bars_minute_202401.csv.gz",
                [
                    ["2024-01-30", "09:00", "72030", "1", "1", "1", "1", "1", "1"],
                    ["2024-01-31", "09:00", "72030", "2", "2", "2", "2", "2", "2"],
                    ["2024-01-31", "09:00", "67580", "3", "3", "3", "3", "3", "3"],
                ],
            )
            bars = load_minute_bars(
                cache_dir=root, start="2024-01-31", end="2024-01-31", codes=["7203"]
            )
            self.assertEqual(list(bars), ["7203"])
            self.assertEqual(len(bars["7203"]), 1)
            self.assertEqual(bars["7203"][0].open, 2.0)

    def test_resample_aggregates_ohlcv_first_max_min_last_sum(self) -> None:
        """09:00-09:29 -> two 15-min bins with first/max/min/last + summed Vo/Va."""
        bars = {
            "7203": [
                self._bar("09:00", 100, 120, 90, 110, 10, 1000),
                self._bar("09:07", 110, 130, 80, 115, 20, 2000),
                self._bar("09:14", 115, 125, 105, 118, 30, 3000),
                self._bar("09:15", 118, 140, 100, 130, 40, 4000),
                self._bar("09:29", 130, 150, 70, 145, 50, 5000),
            ]
        }
        out = resample_15min(bars)["7203"]
        self.assertEqual(len(out), 2)

        first = out[0]
        self.assertEqual(first.timestamp, datetime(2024, 1, 31, 9, 0))
        self.assertEqual(first.open, 100.0)   # first.open
        self.assertEqual(first.high, 130.0)   # max of 120,130,125
        self.assertEqual(first.low, 80.0)     # min of 90,80,105
        self.assertEqual(first.close, 118.0)  # last.close
        self.assertEqual(first.volume, 60)    # 10+20+30
        self.assertEqual(first.value, 6000.0)  # 1000+2000+3000

        second = out[1]
        self.assertEqual(second.timestamp, datetime(2024, 1, 31, 9, 15))
        self.assertEqual(second.open, 118.0)
        self.assertEqual(second.high, 150.0)
        self.assertEqual(second.low, 70.0)
        self.assertEqual(second.close, 145.0)
        self.assertEqual(second.volume, 90)
        self.assertEqual(second.value, 9000.0)

    def test_resample_session_boundary_separates_bins(self) -> None:
        """11:15-ish and 12:30-ish bars must land in distinct, anchor-correct bins."""
        bars = {
            "7203": [
                self._bar("11:15", 1, 1, 1, 1, 1, 1),
                self._bar("11:29", 2, 2, 2, 2, 2, 2),
                self._bar("12:30", 3, 3, 3, 3, 3, 3),
                self._bar("12:44", 4, 4, 4, 4, 4, 4),
            ]
        }
        out = resample_15min(bars)["7203"]
        timestamps = [b.timestamp for b in out]
        self.assertEqual(
            timestamps,
            [
                datetime(2024, 1, 31, 11, 15),  # morning anchor 09:00 + 9*15
                datetime(2024, 1, 31, 12, 30),  # afternoon anchor 12:30 + 0
            ],
        )
        # The 11:15 morning bin and 12:30 afternoon bin do not merge.
        self.assertEqual(out[0].close, 2.0)
        self.assertEqual(out[1].open, 3.0)

    def test_resample_does_not_synthesize_missing_bins(self) -> None:
        """A gap between bins must not create an empty (synthetic) bin."""
        bars = {
            "7203": [
                self._bar("09:00", 1, 1, 1, 1, 1, 1),
                # nothing in 09:15 and 09:30 bins
                self._bar("09:46", 2, 2, 2, 2, 2, 2),
            ]
        }
        out = resample_15min(bars)["7203"]
        self.assertEqual(
            [b.timestamp for b in out],
            [datetime(2024, 1, 31, 9, 0), datetime(2024, 1, 31, 9, 45)],
        )

    def test_resample_drops_lunch_gap_bars(self) -> None:
        """Bars in the 11:30-12:30 lunch gap are dropped, not binned."""
        bars = {
            "7203": [
                self._bar("09:00", 1, 1, 1, 1, 1, 1),
                self._bar("11:45", 9, 9, 9, 9, 9, 9),  # lunch gap -> drop
            ]
        }
        out = resample_15min(bars)["7203"]
        self.assertEqual([b.timestamp for b in out], [datetime(2024, 1, 31, 9, 0)])

    @staticmethod
    def _bar(
        hhmm: str, o: float, h: float, l: float, c: float, vol: int, va: float
    ) -> MinuteBar:
        ts = datetime.strptime(f"2024-01-31 {hhmm}", "%Y-%m-%d %H:%M")
        return MinuteBar(
            timestamp=ts, code="7203", open=o, high=h, low=l, close=c, volume=vol, value=va
        )

    def _write_minute(self, path: Path, rows: list[list[str]]) -> None:
        with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["Date", "Time", "Code", "O", "H", "L", "C", "Vo", "Va"])
            writer.writerows(rows)


if __name__ == "__main__":
    unittest.main()
