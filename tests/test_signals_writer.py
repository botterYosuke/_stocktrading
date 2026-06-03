from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from signals_writer import is_valid_signals_file, write_daily_signals, write_manifest


class SignalsWriterTests(unittest.TestCase):
    def test_daily_signals_and_manifest_contract(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "signals"
            p1 = write_daily_signals(
                output_dir=out,
                target_date="2026-06-04",
                as_of="2026-06-03",
                generated_at="2026-06-03T00:00:00+00:00",
                rows=[
                    {"code": "72030", "brand": "Toyota", "pred": 0.83, "side": 2},
                    {"code": "67580", "brand": "Sony", "pred": 0.79, "side": 1},
                ],
            )
            p2 = write_daily_signals(
                output_dir=out,
                target_date="2026-06-05",
                as_of="2026-06-04",
                generated_at="2026-06-04T00:00:00+00:00",
                rows=[
                    {"code": "72030", "brand": "Toyota", "pred": 0.81, "side": 2},
                    {"code": "99840", "brand": "SoftBank Group", "pred": 0.77, "side": 1},
                ],
            )

            manifest = write_manifest(
                output_dir=out,
                start="2026-06-04",
                end="2026-06-05",
                signal_files=[p1, p2],
            )

            daily = json.loads(p1.read_text(encoding="utf-8"))
            self.assertEqual(daily["schema_version"], 1)
            self.assertEqual(daily["target_date"], "2026-06-04")
            self.assertEqual(daily["as_of"], "2026-06-03")
            self.assertEqual(daily["signals"][0]["symbol"], "7203.TSE")
            self.assertEqual(daily["signals"][0]["side"], "LONG")
            self.assertEqual(daily["signals"][1]["side"], "SHORT")
            self.assertEqual(daily["regulation_filter"]["replay"], "not_available")

            doc = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(doc["files"], ["signals_2026-06-04.json", "signals_2026-06-05.json"])
            self.assertEqual(doc["instruments"], ["6758.TSE", "7203.TSE", "9984.TSE"])
            self.assertEqual(doc["retrain_policy"], "daily")


class ValidSignalsFileTests(unittest.TestCase):
    def _write_valid(self, td, rows, target_date="2021-07-01"):
        return write_daily_signals(
            output_dir=td, target_date=target_date, as_of="2021-06-30", rows=rows
        )

    def test_valid_with_signals(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = self._write_valid(td, [{"code": "7203", "pred": 0.83, "side": 2}])
            self.assertTrue(is_valid_signals_file(p))
            self.assertTrue(is_valid_signals_file(p, expected_target_date="2021-07-01"))

    def test_empty_signals_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = self._write_valid(td, [])
            self.assertTrue(is_valid_signals_file(p))

    def test_missing_and_broken(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(is_valid_signals_file(Path(td) / "nope.json"))
            broken = Path(td) / "b.json"
            broken.write_text("{ not json", encoding="utf-8")
            self.assertFalse(is_valid_signals_file(broken))

    def test_wrong_expected_target_date(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = self._write_valid(td, [{"code": "7203", "pred": 0.83, "side": 2}])
            self.assertFalse(is_valid_signals_file(p, expected_target_date="2021-07-02"))

    def test_malformed_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = {
                "schema_version": 1, "target_date": "2021-07-01", "as_of": "2021-06-30",
                "signals": [{"symbol": "7203.TSE", "side": "LONG", "confidence": 0.8}],
            }
            cases = [
                {**base, "schema_version": 2},
                {**base, "signals": [{"symbol": "7203.TSE", "side": "BUY", "confidence": 0.8}]},
                {**base, "signals": [{"symbol": "7203.TSE", "side": "LONG", "confidence": 1.5}]},
                {**base, "signals": [{"symbol": "7203.TSE", "side": "LONG", "confidence": 0}]},
                {**base, "signals": [{"symbol": "", "side": "LONG", "confidence": 0.8}]},
                {**base, "signals": "notalist"},
            ]
            for i, doc in enumerate(cases):
                p = Path(td) / f"c{i}.json"
                p.write_text(json.dumps(doc), encoding="utf-8")
                self.assertFalse(is_valid_signals_file(p), f"case {i} should be invalid")


if __name__ == "__main__":
    unittest.main()
