from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from model_cache import (
    DEFAULT_MODEL_PARAMS,
    artifact_paths,
    cache_key,
    is_hit,
    write_meta,
)

P = DEFAULT_MODEL_PARAMS
AS_OF = "2021-06-30"
TW = 80
CODES = ["7203", "6758", "9984"]


def _complete_cache(td: str, as_of=AS_OF, tw=TW, codes=CODES, params=P) -> None:
    paths = artifact_paths(td, cache_key(as_of, tw, codes, params))
    paths["up"].parent.mkdir(parents=True, exist_ok=True)
    paths["up"].write_text("up", encoding="utf-8")  # stand-in for a .keras artifact
    paths["down"].write_text("down", encoding="utf-8")
    write_meta(td, as_of, tw, codes, params)


class ModelCacheKeyTests(unittest.TestCase):
    def test_key_deterministic(self) -> None:
        self.assertEqual(cache_key(AS_OF, TW, CODES, P), cache_key(AS_OF, TW, CODES, P))

    def test_key_universe_order_independent(self) -> None:
        self.assertEqual(
            cache_key(AS_OF, TW, ["7203", "6758"], P),
            cache_key(AS_OF, TW, ["6758", "7203"], P),
        )

    def test_key_sensitive_to_identity_and_common_params(self) -> None:
        base = cache_key(AS_OF, TW, CODES, P)
        self.assertNotEqual(base, cache_key("2021-06-29", TW, CODES, P))
        self.assertNotEqual(base, cache_key(AS_OF, 60, CODES, P))
        self.assertNotEqual(base, cache_key(AS_OF, TW, CODES, {**P, "window": 20}))

    def test_key_ignores_per(self) -> None:
        # per_up/per_down must NOT affect the directory key (per the B3-3 design)
        self.assertEqual(
            cache_key(AS_OF, TW, CODES, P),
            cache_key(AS_OF, TW, CODES, {**P, "per_up": 2.0, "per_down": 0.1}),
        )


class ModelCacheHitTests(unittest.TestCase):
    def test_miss_when_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(is_hit(td, AS_OF, TW, CODES, P))

    def test_miss_when_partial(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            paths = artifact_paths(td, cache_key(AS_OF, TW, CODES, P))
            paths["up"].parent.mkdir(parents=True, exist_ok=True)
            paths["up"].write_text("up", encoding="utf-8")
            write_meta(td, AS_OF, TW, CODES, P)  # down.keras missing
            self.assertFalse(is_hit(td, AS_OF, TW, CODES, P))

    def test_hit_when_complete(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _complete_cache(td)
            self.assertTrue(is_hit(td, AS_OF, TW, CODES, P))

    def test_miss_when_meta_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _complete_cache(td)
            meta_path = artifact_paths(td, cache_key(AS_OF, TW, CODES, P))["meta"]
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["as_of"] = "2020-01-01"  # stale / corrupted meta
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            self.assertFalse(is_hit(td, AS_OF, TW, CODES, P))


if __name__ == "__main__":
    unittest.main()
