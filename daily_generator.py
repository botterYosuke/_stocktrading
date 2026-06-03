"""Daily point-in-time signal generator (B3-2 skeleton).

This module enumerates business-day ``target_date``s over a range and maps each
to its ``as_of = prev_business_day(target_date)``. B3-2 is a SKELETON: the CLI
prints/returns that plan only. The heavy per-date train/predict/emit loop, model
cache, and resume are wired in B3-3+.
"""
from __future__ import annotations

import argparse
import datetime

from data_source import parse_date
from misc import Misc


def enumerate_business_days(start, end) -> list[datetime.date]:
    """Business days (weekends/JP holidays excluded), start..end **inclusive**.

    ``start > end`` returns ``[]`` (no error), per the B3-2 contract.
    """
    cur = parse_date(start)
    end_date = parse_date(end)
    one = datetime.timedelta(days=1)
    days: list[datetime.date] = []
    while cur <= end_date:
        if Misc.check_day_type(cur) == 0:  # 0 == weekday
            days.append(cur)
        cur += one
    return days


def plan_runs(start, end) -> list[dict]:
    """Dry-run mapping: each business ``target_date`` -> ``as_of`` (prev business day)."""
    return [
        {"target_date": d.isoformat(), "as_of": Misc.prev_business_day(d).isoformat()}
        for d in enumerate_business_days(start, end)
    ]


def main(argv=None) -> list[dict]:
    parser = argparse.ArgumentParser(
        prog="daily_generator",
        description="Daily point-in-time signal generator (B3-2 skeleton: prints the "
        "target_date<-as_of plan; does NOT generate signals yet).",
    )
    parser.add_argument("--start", required=True, help="range start (YYYY-MM-DD), inclusive")
    parser.add_argument("--end", required=True, help="range end (YYYY-MM-DD), inclusive")
    parser.add_argument("--out", default="signals", help="output dir (used once the loop is wired)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan only (the B3-2 skeleton always dry-runs)",
    )
    args = parser.parse_args(argv)

    plan = plan_runs(args.start, args.end)
    for row in plan:
        print(f"{row['target_date']} <- as_of {row['as_of']}")
    # NOTE: signal generation (prepare_data/fit/predict/emit_daily_signals) and
    # model cache / resume are wired in B3-3+. args.out is reserved for that.
    return plan


if __name__ == "__main__":
    main()
