"""較正専用暫定モデル (shadow) の定義。2026-07-16 owner 確定。

位置づけ (G8 honest-N 非消費の条件 — 全出力にこのタグを付ける):
- h5s × m3.0 × τ0.70 は post-selection の刺激生成器であり、採用戦略ではない
- PnL・edge・hit・ratio・セル優劣・閾値調整には使用禁止
- gen1 再採点・07-14 開封・IS-KILL 撤回は禁止
- fill 較正値を採用する将来サイクルは fresh sealed days で評価する
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scalp_agent.config import config_hash
from scalp_agent.features import feature_schema_hash

CAL_HORIZON_S = 5.0
CAL_MULT = 3.0
CAL_TAU = 0.70
CAL_TRAIN_DAYS = ("2026-07-09", "2026-07-13")  # role "train+val"

MODEL_DIR = Path("artifacts/calibration/shadow_h5_m30")
MODEL_PATH = MODEL_DIR / "model.txt"
META_PATH = MODEL_DIR / "meta.json"

CALIBRATION_TAGS = {
    "calibration_only": True,
    "policy": "shadow_h5_m30_tau070",
    "purpose": "fill-model calibration stimulus (DESIGN 決定 2)。判定・台帳・セル選択に使用禁止",
}


def model_meta() -> dict:
    return {
        **CALIBRATION_TAGS,
        "horizon_s": CAL_HORIZON_S,
        "mult": CAL_MULT,
        "tau": CAL_TAU,
        "train_days": list(CAL_TRAIN_DAYS),
        "config_hash": config_hash(),
        "feature_schema_hash": feature_schema_hash(),
    }


def model_version() -> str:
    """モデルファイルの sha256 先頭 12 桁 (出力レコードのタグ用)。"""
    if not MODEL_PATH.exists():
        return "missing"
    return hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest()[:12]


def load_booster():
    """LightGBM booster をロードし、メタ整合を検査する。"""
    import lightgbm as lgb

    if not MODEL_PATH.exists():
        raise FileNotFoundError(
            f"{MODEL_PATH} が無い。scripts/train_calibration_model.py を先に実行する")
    meta = json.loads(META_PATH.read_text(encoding="utf-8"))
    expected = model_meta()
    for k in ("horizon_s", "mult", "tau", "config_hash", "feature_schema_hash"):
        if meta.get(k) != expected[k]:
            raise RuntimeError(f"calibration model meta 不一致: {k}: "
                               f"saved={meta.get(k)} expected={expected[k]}")
    return lgb.Booster(model_file=str(MODEL_PATH))
