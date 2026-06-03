"""Point-in-time model cache for the daily generator (B3-3).

Layout: ``models/<key>/{up.keras, down.keras, meta.json}``.

The cache **key** (directory name) is derived from the *common* inputs only:
``as_of`` + ``train_window_business_days`` + order-independent ``universe_hash`` +
common ``model_params`` (window/epochs/batch_size/layer/threshold). ``per`` is
NOT in the key (it differs UP vs DOWN); ``per_up``/``per_down`` live in meta.

Key/path/hit-miss are pure (stdlib only, no TensorFlow). ``save_models`` /
``load_models`` import Keras lazily and are exercised in the brain venv.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

CACHE_SCHEMA_VERSION = 1

# Canonical training params (kept in sync with model_manager / __main__).
DEFAULT_MODEL_PARAMS = {
    "window": 30,
    "epochs": 30,
    "batch_size": 128,
    "layer": "lstm",
    "threshold": 0.7,
    "per_up": 1.005,
    "per_down": 0.995,
}

# Fields of model_params that participate in the directory key (common to UP/DOWN).
_KEY_PARAM_FIELDS = ("window", "epochs", "batch_size", "layer", "threshold")


def universe_hash(codes) -> str:
    """Order-independent hash of the universe code set."""
    joined = ",".join(sorted(str(c) for c in codes))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def cache_key(as_of, train_window_business_days, codes, model_params) -> str:
    payload = json.dumps(
        {
            "as_of": str(as_of),
            "train_window_business_days": int(train_window_business_days),
            "universe_hash": universe_hash(codes),
            "model_params": {k: model_params[k] for k in _KEY_PARAM_FIELDS},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def cache_dir(models_root, key) -> Path:
    return Path(models_root) / key


def artifact_paths(models_root, key) -> dict:
    d = cache_dir(models_root, key)
    return {"up": d / "up.keras", "down": d / "down.keras", "meta": d / "meta.json"}


def build_meta(as_of, train_window_business_days, codes, model_params) -> dict:
    codes = list(codes)
    key = cache_key(as_of, train_window_business_days, codes, model_params)
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "key": key,
        "as_of": str(as_of),
        "train_window_business_days": int(train_window_business_days),
        "universe_hash": universe_hash(codes),
        "universe_count": len(codes),
        "model_params": dict(model_params),
        "artifacts": {"up": "up.keras", "down": "down.keras"},
    }


def write_meta(models_root, as_of, train_window_business_days, codes, model_params) -> Path:
    meta = build_meta(as_of, train_window_business_days, codes, model_params)
    path = artifact_paths(models_root, meta["key"])["meta"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def is_hit(models_root, as_of, train_window_business_days, codes, model_params) -> bool:
    """Hit iff up/down/meta all exist AND meta agrees with the key inputs."""
    key = cache_key(as_of, train_window_business_days, codes, model_params)
    paths = artifact_paths(models_root, key)
    if not all(paths[k].exists() for k in ("up", "down", "meta")):
        return False
    try:
        meta = json.loads(paths["meta"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    expected = build_meta(as_of, train_window_business_days, codes, model_params)
    for f in ("key", "as_of", "train_window_business_days", "universe_hash"):
        if meta.get(f) != expected[f]:
            return False
    mp, emp = meta.get("model_params", {}), expected["model_params"]
    return all(mp.get(f) == emp.get(f) for f in _KEY_PARAM_FIELDS)


def save_models(models_root, as_of, train_window_business_days, codes, model_params,
                up_model, down_model) -> str:
    """Persist UP/DOWN Keras models + meta; returns the cache key."""
    key = cache_key(as_of, train_window_business_days, codes, model_params)
    paths = artifact_paths(models_root, key)
    paths["up"].parent.mkdir(parents=True, exist_ok=True)
    up_model.save(paths["up"])
    down_model.save(paths["down"])
    write_meta(models_root, as_of, train_window_business_days, codes, model_params)
    return key


def load_models(models_root, key):
    """Load (up_model, down_model) for an existing cache key (lazy Keras import)."""
    from tensorflow.keras.models import load_model

    paths = artifact_paths(models_root, key)
    return load_model(paths["up"]), load_model(paths["down"])
