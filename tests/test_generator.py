from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

# daily_generator -> misc -> config_manager pulls jpholiday/yaml/dotenv (the
# calendar stack). Absent in the stdlib-only tooling env, so these self-skip
# there and run in the brain venv (BaseDir must be set for config_manager).
_HAS_CAL = all(
    importlib.util.find_spec(m) is not None for m in ("jpholiday", "yaml", "dotenv")
)


@unittest.skipUnless(_HAS_CAL, "calendar deps (jpholiday/yaml/dotenv) not installed")
class GeneratorTests(unittest.TestCase):
    def test_enumerate_excludes_weekend(self) -> None:
        from daily_generator import enumerate_business_days

        days = enumerate_business_days("2021-06-04", "2021-06-08")  # Fri..Tue
        self.assertEqual(days, [date(2021, 6, 4), date(2021, 6, 7), date(2021, 6, 8)])
        self.assertNotIn(date(2021, 6, 5), days)  # Sat
        self.assertNotIn(date(2021, 6, 6), days)  # Sun

    def test_enumerate_excludes_holiday(self) -> None:
        from daily_generator import enumerate_business_days

        # 2021-05-03/04/05 are JP holidays (GW); only 05-06, 05-07 are business days.
        days = enumerate_business_days("2021-05-03", "2021-05-07")
        self.assertEqual(days, [date(2021, 5, 6), date(2021, 5, 7)])

    def test_enumerate_start_after_end_is_empty(self) -> None:
        from daily_generator import enumerate_business_days

        self.assertEqual(enumerate_business_days("2021-06-08", "2021-06-04"), [])

    def test_prev_business_day_skips_weekend(self) -> None:
        from misc import Misc

        # Monday -> previous Friday
        self.assertEqual(Misc.prev_business_day(date(2021, 6, 7)), date(2021, 6, 4))

    def test_prev_business_day_skips_holiday(self) -> None:
        from misc import Misc

        # 2021-05-06 (Thu): skip 05-05/04/03 (holidays) + 05-02/01 (weekend) -> 04-30 (Fri)
        self.assertEqual(Misc.prev_business_day(date(2021, 5, 6)), date(2021, 4, 30))

    def test_cli_dry_run_mapping(self) -> None:
        from daily_generator import main

        with tempfile.TemporaryDirectory() as td:  # empty out -> all "generate"
            plan = main(["--start", "2021-06-04", "--end", "2021-06-08", "--out", td, "--dry-run"])
        self.assertEqual(
            plan,
            [
                {"target_date": "2021-06-04", "as_of": "2021-06-03", "action": "generate"},
                {"target_date": "2021-06-07", "as_of": "2021-06-04", "action": "generate"},
                {"target_date": "2021-06-08", "as_of": "2021-06-07", "action": "generate"},
            ],
        )

    def test_should_generate_resume(self) -> None:
        from daily_generator import should_generate, signals_path_for
        from signals_writer import write_daily_signals

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            # missing -> generate
            self.assertTrue(should_generate(td_path, "2021-07-01"))
            # valid existing -> skip
            write_daily_signals(
                output_dir=td_path, target_date="2021-07-01", as_of="2021-06-30",
                rows=[{"code": "7203", "pred": 0.83, "side": 2}],
            )
            self.assertFalse(should_generate(td_path, "2021-07-01"))
            # --force -> regenerate even if valid
            self.assertTrue(should_generate(td_path, "2021-07-01", force=True))
            # invalid JSON -> generate
            signals_path_for(td_path, "2021-07-02").write_text("{ broken", encoding="utf-8")
            self.assertTrue(should_generate(td_path, "2021-07-02"))

    def test_aggregate_manifest_union_and_order(self) -> None:
        from daily_generator import aggregate_manifest
        from signals_writer import write_daily_signals

        with tempfile.TemporaryDirectory() as td:
            # create out of order; symbols overlap across days
            write_daily_signals(output_dir=td, target_date="2021-06-08", as_of="2021-06-07",
                                rows=[{"code": "9984", "pred": 0.8, "side": 2}])
            write_daily_signals(output_dir=td, target_date="2021-06-04", as_of="2021-06-03",
                                rows=[{"code": "7203", "pred": 0.8, "side": 2}])
            write_daily_signals(output_dir=td, target_date="2021-06-07", as_of="2021-06-04",
                                rows=[{"code": "6758", "pred": 0.8, "side": 1},
                                      {"code": "7203", "pred": 0.75, "side": 2}])
            mp = aggregate_manifest(td, "2021-06-04", "2021-06-08")
            m = json.loads(Path(mp).read_text(encoding="utf-8"))
            self.assertEqual(
                m["files"],
                ["signals_2021-06-04.json", "signals_2021-06-07.json", "signals_2021-06-08.json"],
            )
            self.assertEqual(m["instruments"], ["6758.TSE", "7203.TSE", "9984.TSE"])
            self.assertEqual((m["start"], m["end"]), ("2021-06-04", "2021-06-08"))

    def test_aggregate_manifest_skips_invalid(self) -> None:
        from daily_generator import aggregate_manifest, signals_path_for
        from signals_writer import write_daily_signals

        with tempfile.TemporaryDirectory() as td:
            write_daily_signals(output_dir=td, target_date="2021-06-04", as_of="2021-06-03",
                                rows=[{"code": "7203", "pred": 0.8, "side": 2}])
            signals_path_for(Path(td), "2021-06-07").write_text("{bad", encoding="utf-8")
            write_daily_signals(output_dir=td, target_date="2021-06-08", as_of="2021-06-07",
                                rows=[{"code": "9984", "pred": 0.8, "side": 2}])
            mp = aggregate_manifest(td, "2021-06-04", "2021-06-08")
            m = json.loads(Path(mp).read_text(encoding="utf-8"))
            self.assertEqual(m["files"], ["signals_2021-06-04.json", "signals_2021-06-08.json"])
            self.assertEqual(m["instruments"], ["7203.TSE", "9984.TSE"])

    def test_run_range_orchestration_resume_force_aggregate(self) -> None:
        from daily_generator import run_range
        from signals_writer import write_daily_signals

        calls = []

        def fake_gen(*, model_manager, as_of, target_date, out_dir, models_root, model_params):
            calls.append((target_date, as_of))
            write_daily_signals(
                output_dir=out_dir, target_date=target_date, as_of=as_of,
                rows=[{"code": "7203", "pred": 0.8, "side": 2}],
            )

        with tempfile.TemporaryDirectory() as td:
            mp = run_range("2021-06-04", "2021-06-08", td, generate_one=fake_gen)
            # loop covers the 3 business days, as_of = prev business day
            self.assertEqual([c[0] for c in calls],
                             ["2021-06-04", "2021-06-07", "2021-06-08"])
            self.assertEqual([c[1] for c in calls],
                             ["2021-06-03", "2021-06-04", "2021-06-07"])
            m = json.loads(Path(mp).read_text(encoding="utf-8"))
            self.assertEqual(
                m["files"],
                ["signals_2021-06-04.json", "signals_2021-06-07.json", "signals_2021-06-08.json"],
            )
            # 2nd run -> resume skips everything (valid signals already present)
            calls.clear()
            run_range("2021-06-04", "2021-06-08", td, generate_one=fake_gen)
            self.assertEqual(calls, [])
            # --force -> regenerate all
            run_range("2021-06-04", "2021-06-08", td, force=True, generate_one=fake_gen)
            self.assertEqual([c[0] for c in calls],
                             ["2021-06-04", "2021-06-07", "2021-06-08"])


if __name__ == "__main__":
    unittest.main()
