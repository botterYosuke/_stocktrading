from __future__ import annotations

import argparse
import os

from minute_data_source import load_minute_bars, resample_15min
from universe import select_universe

# TSE morning session ends 11:30; afternoon opens 12:30.
_MORNING_CUTOFF = (11, 30)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Phase 0a smoke: build a 1-day 15-minute panel for the universe."
    )
    parser.add_argument("--as-of", required=True, help="trading date, YYYY-MM-DD")
    parser.add_argument(
        "--cache-dir",
        default=os.environ.get("DEV_J_QUANTS_CACHE"),
        help="J-Quants CSV.gz dir (default: $DEV_J_QUANTS_CACHE)",
    )
    parser.add_argument("--top-n", type=int, default=100, help="universe size")
    args = parser.parse_args()

    universe = select_universe(
        as_of=args.as_of, top_n=args.top_n, cache_dir=args.cache_dir
    )

    bars_by_code = load_minute_bars(
        cache_dir=args.cache_dir,
        start=args.as_of,
        end=args.as_of,
        codes=universe,
    )
    panel_by_code = resample_15min(bars_by_code)

    # Flatten to (code, timestamp) rows, sorted deterministically.
    rows = [
        (code, bar.timestamp, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.value)
        for code, bars in panel_by_code.items()
        for bar in bars
    ]
    rows.sort(key=lambda r: (r[1], r[0]))

    bins = {r[1] for r in rows}
    morning_bins = {ts for ts in bins if (ts.hour, ts.minute) < _MORNING_CUTOFF}
    afternoon_bins = bins - morning_bins

    print(f"as_of                : {args.as_of}")
    print(f"cache_dir            : {args.cache_dir}")
    print(f"universe size        : {len(universe)}")
    print(f"codes with panel rows: {len(panel_by_code)}")
    print(f"panel total rows     : {len(rows)}")
    print(
        f"unique 15m bins      : {len(bins)} "
        f"(morning={len(morning_bins)}, afternoon={len(afternoon_bins)})"
    )
    print("head (first 10 rows): code, ts, O, H, L, C, Vol, Va")
    for r in rows[:10]:
        code, ts, o, h, l, c, vol, va = r
        va_str = "None" if va is None else f"{va:.0f}"
        print(
            f"  {code:<6} {ts:%Y-%m-%d %H:%M}  "
            f"O={o:<10.2f} H={h:<10.2f} L={l:<10.2f} C={c:<10.2f} "
            f"Vol={vol:<10} Va={va_str}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
