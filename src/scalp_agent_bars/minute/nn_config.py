"""足読み gen3 (系列 NN) の凍結設定。新 family `gen3_minute_nn_v1` (honest-N +1)。

gen2 との対照実験: **変えるのは学習器と入力表現のみ**。
- 入力: 手作り特徴 26 個 → 当日直近 K=32 本の生分足系列 (8ch) + 静的 4 特徴
- 学習器: LightGBM → GRU エンコーダ + 6 セル・マルチタスク分類ヘッド
- ラベル・保守的バー執行・friction 較正・分割・候補条件・sealed OOS は
  gen2 (minute.config) と完全同一 — config hash に gen2 hash を含めて束縛する。

early stopping は train 末尾窓 (ESTOP_RANGE) の loss のみで行い、公式 val は
候補選択まで一切見ない。val を見た後の構成変更は次の新 family。
"""
from __future__ import annotations

import hashlib

import orjson

from scalp_agent_bars.minute.config import (  # noqa: F401  (gen3 でも同一の凍結値)
    ATR_MULTS,
    CANDIDATE_MAX_CODE_SHARE,
    CANDIDATE_MIN_D,
    CANDIDATE_MIN_N,
    CANDIDATE_MIN_RATIO,
    CROSS_SECTION_TOP_K,
    HORIZON_BARS,
    OOS_RANGE,
    TAUS,
    TRAIN_RANGE,
    UNIVERSE,
    VAL_RANGE,
    cell_key,
    grid_cells,
)
from scalp_agent_bars.minute.config import config_hash as gen2_config_hash

# ---- 入力表現 (凍結) -------------------------------------------------------------

SEQ_LEN = 32  # 当日内の直近バー本数。日初は左ゼロパディング + mask ch

# 系列チャネル (順序も凍結)。すべて因果・当日内・固定スケールで正規化。
SEQ_CHANNELS: tuple[str, ...] = (
    "ret1",    # (close_t/close_{t-1}-1)*1e4, clip±50, /50。日初バーは 0
    "range",   # (high-low)/close*1e4, clip[0,100], /100
    "body",    # (close-open)/close*1e4, clip±50, /50
    "upper",   # (high-max(o,c))/close*1e4, clip[0,50], /50
    "lower",   # (min(o,c)-low)/close*1e4, clip[0,50], /50
    "volr",    # log1p(vol/trailing-median30(現在バー除く,min10)), clip[0,5], /5。無効=0
    "tod",     # (start_tod-9h)/60/390
    "mask",    # 実バー=1 / パディング=0
)

STATIC_FEATURES: tuple[str, ...] = (
    "gap",        # (寄り - 前日終値)/前日終値*1e4, clip±100, /100。前日なし=0
    "prev_ret",   # 前日 (close-open)/open*1e4, clip±300, /300
    "prev_range", # 前日 (high-low)/close*1e4, clip[0,500], /500
    "atr",        # 決定バー ATR20/close*1e4, clip[0,100], /100
)

# ---- モデル・学習 (凍結・掃引しない) ------------------------------------------------

MODEL = {
    "kind": "gru_multitask",
    "hidden": 64,
    "layers": 2,
    "mlp_hidden": 64,
    "dropout": 0.1,
    "heads": 6,          # (horizon_bars × atr_mult) の 6 セル
    "classes": 3,
}
TRAIN = {
    "batch_size": 4096,
    "lr": 1e-3,
    "max_epochs": 20,
    "estop_patience": 3,
    "seed": 20260716,
}

# early stopping 専用窓 (train の末尾)。公式 val は使わない。
FIT_RANGE = ("2024-01-04", "2025-04-30")
ESTOP_RANGE = ("2025-05-01", "2025-06-30")

PATTERNS: tuple[str, ...] = ("NN_pooled", "NN_pooled_topk")


def config_hash() -> str:
    payload = {
        "family": "gen3_minute_nn_v1",
        "gen2_config_hash": gen2_config_hash(),
        "seq_len": SEQ_LEN,
        "seq_channels": SEQ_CHANNELS,
        "static_features": STATIC_FEATURES,
        "model": MODEL,
        "train": TRAIN,
        "fit_range": FIT_RANGE,
        "estop_range": ESTOP_RANGE,
        "patterns": PATTERNS,
    }
    return hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()
