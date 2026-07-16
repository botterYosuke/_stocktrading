"""キャッシュ整合性: sidecar が date・code・feature schema・label spec・
source fingerprint を持ち、いずれかの不一致で stale になること。"""
import json

import numpy as np
import pytest

from scalp_agent import dataset


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(dataset, "CACHE_DIR", tmp_path)
    fp = {"path": "S:/fake/2026-07-09.duckdb", "size": 12345, "mtime": 1.0}
    monkeypatch.setattr(dataset, "_source_fingerprint", lambda day: dict(fp))
    return tmp_path, fp


def _tiny_table():
    return {"b_ts": np.array([1.0, 2.0]), "f_spread_bps": np.array([3.0, 4.0])}


def test_sidecar_contains_all_required_fields(tmp_cache):
    dataset.write_cache("2026-07-09", "7203", _tiny_table())
    _, meta_path = dataset.cache_paths("2026-07-09", "7203")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["day"] == "2026-07-09"
    assert meta["code"] == "7203"
    assert meta["feature_schema_hash"]
    assert meta["label_spec"]["kind"] == "triple_barrier_v1"
    assert meta["label_spec"]["horizons_s"] and meta["label_spec"]["mults"]
    assert set(meta["source"]) == {"path", "size", "mtime"}
    assert meta["n_decisions"] == 2


def test_cache_valid_roundtrip_and_load(tmp_cache):
    dataset.write_cache("2026-07-09", "7203", _tiny_table())
    assert dataset.is_cache_valid("2026-07-09", "7203")
    loaded = dataset.load_cache("2026-07-09", "7203")
    assert loaded["b_ts"].tolist() == [1.0, 2.0]
    assert loaded["f_spread_bps"].tolist() == [3.0, 4.0]


def test_cache_stale_on_source_fingerprint_change(tmp_cache, monkeypatch):
    dataset.write_cache("2026-07-09", "7203", _tiny_table())
    monkeypatch.setattr(
        dataset, "_source_fingerprint",
        lambda day: {"path": "S:/fake/2026-07-09.duckdb", "size": 99999, "mtime": 2.0},
    )
    assert not dataset.is_cache_valid("2026-07-09", "7203")


def test_cache_stale_on_schema_hash_tamper(tmp_cache):
    dataset.write_cache("2026-07-09", "7203", _tiny_table())
    _, meta_path = dataset.cache_paths("2026-07-09", "7203")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["feature_schema_hash"] = "0" * 64
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    assert not dataset.is_cache_valid("2026-07-09", "7203")


def test_cache_invalid_when_files_missing(tmp_cache):
    assert not dataset.is_cache_valid("2026-07-09", "9999")
