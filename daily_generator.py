"""Daily point-in-time signal generator (B3-2 skeleton).

This module enumerates business-day ``target_date``s over a range and maps each
to its ``as_of = prev_business_day(target_date)``. B3-2 is a SKELETON: the CLI
prints/returns that plan only. The heavy per-date train/predict/emit loop, model
cache, and resume are wired in B3-3+.
"""
from __future__ import annotations

import argparse
import datetime
from pathlib import Path

from data_source import parse_date
from misc import Misc
from model_cache import DEFAULT_MODEL_PARAMS
from signals_writer import is_valid_signals_file, write_manifest


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


def signals_path_for(out_dir, target_date) -> Path:
    return Path(out_dir) / f"signals_{target_date}.json"


def should_generate(out_dir, target_date, force=False) -> bool:
    """Resume decision (B3-4): (re)generate unless a *valid* signals file already
    exists for ``target_date``. ``--force`` always regenerates."""
    if force:
        return True
    return not is_valid_signals_file(
        signals_path_for(out_dir, target_date), expected_target_date=target_date
    )


def plan_runs(start, end, out_dir=None, force=False) -> list[dict]:
    """Each business ``target_date`` -> ``as_of`` (prev business day). When
    ``out_dir`` is given, also tag a resume ``action`` of ``generate``/``skip``."""
    rows = []
    for d in enumerate_business_days(start, end):
        target_date = d.isoformat()
        row = {"target_date": target_date, "as_of": Misc.prev_business_day(d).isoformat()}
        if out_dir is not None:
            row["action"] = "generate" if should_generate(out_dir, target_date, force) else "skip"
        rows.append(row)
    return rows


def aggregate_manifest(out_dir, start, end) -> Path:
    """Aggregate the valid daily signals files in ``[start, end]`` into one range
    ``manifest.json`` (B3-5): ``files`` date-ascending, ``instruments`` = union of
    every day's signal symbols, ``start``/``end`` = first/last covered date.

    Invalid/missing daily files are skipped. With no valid files the manifest is
    written with ``files=[]`` and ``instruments=[]`` over the requested range.
    """
    out = Path(out_dir)
    pairs = []
    for d in enumerate_business_days(start, end):
        target_date = d.isoformat()
        path = signals_path_for(out, target_date)
        if is_valid_signals_file(path, expected_target_date=target_date):
            pairs.append((target_date, path))
    pairs.sort(key=lambda x: x[0])  # date ascending

    if pairs:
        m_start, m_end = pairs[0][0], pairs[-1][0]
    else:
        m_start, m_end = parse_date(start).isoformat(), parse_date(end).isoformat()
    return write_manifest(
        output_dir=out, start=m_start, end=m_end, signal_files=[p for _, p in pairs]
    )


def _generate_one_date(*, model_manager, as_of, target_date, out_dir, models_root, model_params):
    """Real one-date generation (B3-6): prepare_data(as_of) -> model cache
    hit(load)/miss(fit+save) -> predict UP/DOWN -> price-band -> emit_daily_signals.
    Heavy (pandas/TF); imported lazily so the orchestration stays importable/testable.
    """
    import pandas as pd

    import model_cache
    from data_source import newest_close_as_of
    from model_manager import ModelManager, emit_daily_signals

    mm = model_manager if model_manager is not None else ModelManager()
    dict_df, dict_close = mm.prepare_data(as_of)
    codes, tw = mm.codes, mm.train_window_business_days

    if model_cache.is_hit(models_root, as_of, tw, codes, model_params):
        key = model_cache.cache_key(as_of, tw, codes, model_params)
        up_model, down_model = model_cache.load_models(models_root, key)
    else:
        up_model = mm.fit(dict_df, dict_close, per=model_params["per_up"], opt_model=model_params["layer"])
        down_model = mm.fit(dict_df, dict_close, per=model_params["per_down"], opt_model=model_params["layer"])
        model_cache.save_models(models_root, as_of, tw, codes, model_params, up_model, down_model)

    df_up = mm.predict(up_model, dict_df, as_of)
    df_up["side"] = 2
    df_down = mm.predict(down_model, dict_df, as_of)
    df_down["side"] = 1
    df = pd.concat([df_up, df_down]).sort_values("pred", ascending=False).drop_duplicates(
        subset=["code"], keep="first"
    )
    sel = [i for i, r in df.iterrows() if 700 < newest_close_as_of(mm.bars_by_code, r["code"], as_of) < 6000]
    df = df.loc[sel, ["date", "code", "pred", "side"]]
    emit_daily_signals(df, as_of, output_dir=out_dir)


def run_range(start, end, out_dir, force=False, *, model_manager=None,
              models_root="models", model_params=None, generate_one=None) -> Path:
    """Orchestrate point-in-time daily generation over ``[start, end]`` (B3-6).

    For each business ``target_date`` that needs (re)generation (resume / --force),
    map ``as_of = prev_business_day`` and run the one-date generation, then write
    the aggregated range manifest. ``generate_one`` is injectable so the loop /
    resume / aggregation can be tested without TF; the default trains real models.
    """
    params = model_params or DEFAULT_MODEL_PARAMS
    gen = generate_one if generate_one is not None else _generate_one_date
    for d in enumerate_business_days(start, end):
        target_date = d.isoformat()
        if not should_generate(out_dir, target_date, force):
            continue
        as_of = Misc.prev_business_day(d).isoformat()
        gen(
            model_manager=model_manager,
            as_of=as_of,
            target_date=target_date,
            out_dir=out_dir,
            models_root=models_root,
            model_params=params,
        )
    return aggregate_manifest(out_dir, start, end)


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
        "--force",
        action="store_true",
        help="regenerate even when a valid signals file already exists (B3-4)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan only (the skeleton always dry-runs)",
    )
    args = parser.parse_args(argv)

    plan = plan_runs(args.start, args.end, out_dir=args.out, force=args.force)
    for row in plan:
        print(f"{row['target_date']} <- as_of {row['as_of']} [{row['action']}]")
    if not args.dry_run:
        run_range(args.start, args.end, args.out, force=args.force)
    return plan


if __name__ == "__main__":
    main()
