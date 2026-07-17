"""足読み gen8 (cost-aware complete-transaction selector) の凍結設定。

新 family `gen8_net_selector_v1`。事前凍結の正本は
docs/analysis/gen8-net-selector-preregistration-2026-07-17.md。
データ・特徴・friction・執行・セル格子は gen2 と同一 (キャッシュ読み取り再利用)。
新規部分は教師 (実現 net_bps 回帰 + 下側分位) と売買規則 (q̂_α > 0) のみ。
"""
from __future__ import annotations

import hashlib

import orjson

from scalp_agent.config import LGBM_NUM_BOOST_ROUND, LGBM_PARAMS
from scalp_agent_bars.minute.config import (  # noqa: F401
    ATR_BARS,
    ATR_MULTS,
    CANDIDATE_MAX_CODE_SHARE,
    CANDIDATE_MIN_D,
    CANDIDATE_MIN_N,
    CANDIDATE_MIN_RATIO,
    FRICTION_SAFETY,
    HORIZON_BARS,
    UNIVERSE,
    cell_key,
)
from scalp_agent_bars.minute.config import config_hash as gen2_config_hash

# ---- 日割り (事前凍結 §2) --------------------------------------------------------
# 2025-07〜09 は gen2/gen3 の選定済み val — 選定に再利用しない (FINAL_FIT 学習行のみ)。
# 2025-04 は gen4/5/6 の sealed OOS (封印維持) — 選定メトリクスを計算しない。

TRAIN_RANGE = ("2024-01-04", "2025-04-30")
VAL_RANGE = ("2025-05-01", "2025-06-30")
FINAL_FIT_RANGE = ("2024-01-04", "2025-09-30")
OOS_RANGE = ("2025-10-01", "2026-02-18")   # sealed — 凍結完了まで読み込み禁止

# ---- 探索格子 (これが探索の全て) ---------------------------------------------------

ALPHAS: tuple[float, ...] = (0.05, 0.10, 0.20)

# ---- LightGBM (gen2 と objective 以外同一) ----------------------------------------

_BASE = {k: v for k, v in LGBM_PARAMS.items() if k not in ("objective", "num_class")}
LGBM_MEAN_PARAMS: dict = {**_BASE, "objective": "regression"}


def lgbm_quantile_params(alpha: float) -> dict:
    return {**_BASE, "objective": "quantile", "alpha": alpha}


def config_hash() -> str:
    payload = {
        "family": "gen8_net_selector_v1",
        "gen2_config_hash": gen2_config_hash(),
        "universe": UNIVERSE,
        "splits": {
            "train": TRAIN_RANGE,
            "val": VAL_RANGE,
            "final_fit": FINAL_FIT_RANGE,
            "oos": OOS_RANGE,
        },
        "alphas": ALPHAS,
        "lgbm_mean": LGBM_MEAN_PARAMS,
        "lgbm_quantile_base": lgbm_quantile_params(0.0),
        "rounds": LGBM_NUM_BOOST_ROUND,
        "candidate": {
            "min_n": CANDIDATE_MIN_N,
            "min_d": CANDIDATE_MIN_D,
            "min_ratio": CANDIDATE_MIN_RATIO,
            "max_code_share": CANDIDATE_MAX_CODE_SHARE,
        },
        "entry_rule": "q_alpha(net_side)>0; both->larger mean; tie->skip",
        "null_gate": {"time_shuffle_p_upper_max": 0.05},
    }
    return hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()
