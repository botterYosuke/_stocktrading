"""足読み gen2 (stocks_minute) の凍結設定。

新 family `gen2_minute_v1`。IS (train+val) で P1/P2/P3 を探索し、val の候補条件を
満たした構成だけを凍結して sealed OOS へ 1 回適用する。val 結果を見た後の
構成変更は次の新 family。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import orjson

from scalp_agent.config import (  # noqa: F401
    FORCE_CLOSE_TOD,
    LGBM_NUM_BOOST_ROUND,
    LGBM_PARAMS,
    SESSION_AFTERNOON,
    SESSION_MORNING,
)

MINUTE_DIR = Path(r"S:/jp/stocks_minute")

# ---- ユニバース (フル履歴 2024-01-04〜2026-02-18 の 17 銘柄・事前固定) ----------
# stocks_minute のメタデータ検分 (2026-07-16) で to_date=2026-02-18 の銘柄のみ。
# 全て板録画ユニバースに含まれ、スプレッド実測による friction 較正が可能。

UNIVERSE: tuple[str, ...] = (
    "4063", "6098", "6501", "6503", "6758", "6861", "7011", "7203", "7974",
    "8035", "8058", "8306", "8316", "8411", "8766", "9983", "9984",
)

# ---- 日割り (G5: IS/OOS 凍結) --------------------------------------------------

TRAIN_RANGE = ("2024-01-04", "2025-06-30")
VAL_RANGE = ("2025-07-01", "2025-09-30")
OOS_RANGE = ("2025-10-01", "2026-02-18")   # sealed — 凍結完了まで読み込み禁止


def split_of(day: str) -> str:
    """date 文字列 (YYYY-MM-DD) → 'train' | 'val' | 'oos'。範囲外はエラー。"""
    if TRAIN_RANGE[0] <= day <= TRAIN_RANGE[1]:
        return "train"
    if VAL_RANGE[0] <= day <= VAL_RANGE[1]:
        return "val"
    if OOS_RANGE[0] <= day <= OOS_RANGE[1]:
        return "oos"
    raise ValueError(f"day {day} は宣言された分割範囲の外")


# ---- 執行モデル (保守的バー執行) ------------------------------------------------

# friction: 板録画 07-09 + 07-13 の実測 median spread_bps × 安全係数 (銘柄別・凍結)。
# 2024〜25 年へ 2026-07 の実測を適用する近似の保険として 1.25 を掛ける。
FRICTION_CALIBRATION_PATH = Path("artifacts/gen2_minute/friction_spread_bps.json")
FRICTION_SAFETY = 1.25

ENTRY_END_TOD = FORCE_CLOSE_TOD          # 14:55 より前の決定のみ新規
ATR_BARS = 20                            # バリア錨: 直近 20 本 (現在バー含む) の TR 平均

# ---- 凍結格子 -------------------------------------------------------------------

HORIZON_BARS: tuple[int, ...] = (5, 15, 30)      # timeout までのバー数
ATR_MULTS: tuple[float, ...] = (1.0, 2.0)        # Δ = mult × ATR20
TAUS: tuple[float, ...] = (0.45, 0.55, 0.65)

# val 候補条件 (D>=20 が正式に効く構成)
CANDIDATE_MIN_N = 300
CANDIDATE_MIN_D = 20
CANDIDATE_MIN_RATIO = 3.0
CANDIDATE_MAX_CODE_SHARE = 0.5           # G3: 単一銘柄への利益集中

# P3 (クロスセクション): 各分境界で score 最大の 1 銘柄のみエントリー
CROSS_SECTION_TOP_K = 1


def cell_key(horizon_bars: int, atr_mult: float) -> str:
    return f"hb{horizon_bars}_a{int(round(atr_mult * 10))}"


def grid_cells() -> list[tuple[int, float]]:
    return [(h, m) for h in HORIZON_BARS for m in ATR_MULTS]


def grid_cells_full() -> list[tuple[int, float, float]]:
    return [(h, m, t) for h in HORIZON_BARS for m in ATR_MULTS for t in TAUS]


PATTERNS: tuple[str, ...] = ("P1_single", "P2_cross_features", "P3_pooled_topk")


def config_hash() -> str:
    payload = {
        "family": "gen2_minute_v1",
        "universe": UNIVERSE,
        "splits": {"train": TRAIN_RANGE, "val": VAL_RANGE, "oos": OOS_RANGE},
        "friction_safety": FRICTION_SAFETY,
        "atr_bars": ATR_BARS,
        "horizon_bars": HORIZON_BARS,
        "atr_mults": ATR_MULTS,
        "taus": TAUS,
        "candidate": {
            "min_n": CANDIDATE_MIN_N,
            "min_d": CANDIDATE_MIN_D,
            "min_ratio": CANDIDATE_MIN_RATIO,
            "max_code_share": CANDIDATE_MAX_CODE_SHARE,
        },
        "cross_section_top_k": CROSS_SECTION_TOP_K,
        "patterns": PATTERNS,
        "lgbm": LGBM_PARAMS,
        "rounds": LGBM_NUM_BOOST_ROUND,
        "force_close_tod": FORCE_CLOSE_TOD,
    }
    return hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()
