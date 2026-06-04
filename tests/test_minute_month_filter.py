from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from minute_data_source import _file_covers_range


def _p(name: str) -> Path:
    return Path("/fake/j-quants") / name


class MinuteMonthFilterTests(unittest.TestCase):
    """Phase 0a follow-up #1: skip minute files that cannot overlap the window."""

    def test_monthly_token_overlap(self) -> None:
        f = _p("equities_bars_minute_202401.csv.gz")
        # window fully inside the month -> keep
        self.assertTrue(_file_covers_range(f, date(2024, 1, 10), date(2024, 1, 20)))
        # window touches first/last day -> keep
        self.assertTrue(_file_covers_range(f, date(2024, 1, 31), date(2024, 2, 5)))
        self.assertTrue(_file_covers_range(f, date(2023, 12, 20), date(2024, 1, 1)))

    def test_monthly_token_out_of_range_dropped(self) -> None:
        f = _p("equities_bars_minute_202401.csv.gz")
        # window entirely after the month
        self.assertFalse(_file_covers_range(f, date(2024, 2, 1), date(2024, 2, 28)))
        # window entirely before the month
        self.assertFalse(_file_covers_range(f, date(2023, 11, 1), date(2023, 12, 31)))

    def test_per_day_fragment_token(self) -> None:
        f = _p("equities_bars_minute_20260202.csv.gz")
        self.assertTrue(_file_covers_range(f, date(2026, 2, 1), date(2026, 2, 28)))
        self.assertTrue(_file_covers_range(f, date(2026, 2, 2), date(2026, 2, 2)))
        self.assertFalse(_file_covers_range(f, date(2026, 2, 3), date(2026, 2, 28)))
        self.assertFalse(_file_covers_range(f, date(2026, 1, 1), date(2026, 2, 1)))

    def test_open_ended_bounds_keep_everything(self) -> None:
        f = _p("equities_bars_minute_202401.csv.gz")
        self.assertTrue(_file_covers_range(f, None, None))
        self.assertTrue(_file_covers_range(f, None, date(2024, 1, 1)))
        self.assertTrue(_file_covers_range(f, date(2024, 1, 31), None))

    def test_unrecognized_name_is_kept(self) -> None:
        # Never silently drop a file we cannot classify.
        f = _p("equities_bars_minute_latest.csv.gz")
        self.assertTrue(_file_covers_range(f, date(2024, 1, 1), date(2024, 1, 2)))


if __name__ == "__main__":
    unittest.main()
