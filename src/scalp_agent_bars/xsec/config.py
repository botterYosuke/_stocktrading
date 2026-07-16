"""足読み gen4 (`gen4_xsec_v1`) の凍結設定。

IS (train+val) で cheap gate (線形 / LightGBM ranker × horizon × K) を探索し、
val の候補条件を満たした構成だけを凍結して sealed OOS へ 1 回適用する。
val 結果を見た後の構成変更は次の新 family (ADR-0001 G8: honest-N)。

既知の近似 (文書化済みの限界):
- 業種 (Sector33) は listed_info の 2025-12 静的スナップショット。過去時点の
  業種変更・市場区分変更を反映しない (業種は slow-moving、控除のみに使用)。
- stocks_minute は 2026-07 取得のスナップショットのため、期間中に上場廃止した
  銘柄が欠ける可能性 (survivorship)。流動性上位ユニバースでは影響は限定的だが
  net を過大評価しうる — 判定時に注記する。
- friction は板実測ではなく呼値ラダー由来の保守モデル (friction.py)。
  gen2 の 17 銘柄実測と照合し、モデル >= 実測 であることを確認して使う。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import orjson

FAMILY = "gen4_xsec_v1"

MINUTE_DIR = Path(r"S:/jp/stocks_minute")
DAILY_DIR = Path(r"S:/jp/stocks_daily")
LISTED_INFO_DB = Path(r"S:/jp/listed_info.duckdb")

ART = Path("artifacts/gen4_xsec")

# ---- 日割り (G5: 凍結分割。OOS は sealed) ---------------------------------------
#
# gen2 の分割 (2024-01〜2025-09 IS) は使えない: stocks_minute の全銘柄カバレッジ
# 走査 (2026-07-16、artifacts/gen4_xsec/minute_coverage.json) で、300+ 銘柄が
# 揃うのは 2024-04-01〜2025-04-30 のみ (3,647 銘柄がフルカバー、median 265 営業日)。
# フル期間 2024-01〜2026-02 を持つのは 17〜35 銘柄で横断ランキングが成立しない。
# 本分割は**データ到達性のみ**から評価前に決定した (結果を見た変更ではない)。
# OOS が 1 ヶ月 (D≈21) と薄い点は判定時に注記する (G4 の D>=20 は形式的に満たす)。

TRAIN_RANGE = ("2024-04-01", "2024-11-29")
VAL_RANGE = ("2024-12-02", "2025-03-31")
OOS_RANGE = ("2025-04-01", "2025-04-30")   # sealed — 凍結完了まで読み込み禁止

# 分足カバレッジ要件 (ユニバース参加の前提。この窓を持たない銘柄は除外 =
# 窓内 survivorship を含む近似 — 判定時に注記)
MINUTE_COVERAGE_REQ = ("2024-04-01", "2025-04-30")


def split_of(day: str) -> str:
    if TRAIN_RANGE[0] <= day <= TRAIN_RANGE[1]:
        return "train"
    if VAL_RANGE[0] <= day <= VAL_RANGE[1]:
        return "val"
    if OOS_RANGE[0] <= day <= OOS_RANGE[1]:
        return "oos"
    raise ValueError(f"day {day} は宣言された分割範囲の外")


# ---- ユニバース (point-in-time・月次) -------------------------------------------

UNIVERSE_SIZE = 400            # 月ごとの上位 N (>= 300 の要件に余裕を持たせる)
LIQUIDITY_WINDOW_DAYS = 60     # 前月末までの trailing 営業日数 (median TurnoverValue)
MIN_MEDIAN_CLOSE = 200.0       # 低位株除外 (tick 比スプレッドが支配的になる帯)
PRIME_MARKET_CODE = "0111"     # listed_info 静的スナップショット近似 (ETF も落ちる)

# ---- 判断時刻・horizon ----------------------------------------------------------

# 固定判断時刻 (JST tod 秒)。毎分評価しない。昼休みと大引け間際を避ける。
DECISION_TODS: tuple[float, ...] = (
    9 * 3600 + 30 * 60,    # 09:30
    10 * 3600,             # 10:00
    10 * 3600 + 30 * 60,   # 10:30
    13 * 3600,             # 13:00
    14 * 3600,             # 14:00
)
HORIZON_MIN: tuple[int, ...] = (15, 30, 60)   # 14:00+60m=15:00 は 2024 年制度でも存在

ENTRY_MAX_DELAY_S = 180.0      # T から 3 分以内に始まるバーが無ければ不成立 (halt 等)

# ---- ラベル ---------------------------------------------------------------------

SECTOR_MIN_MEMBERS = 5         # Sector33 控除に必要な同時観測数。未満は市場控除のみ

# ---- 執行・friction -------------------------------------------------------------

FRICTION_SAFETY = 1.25         # スプレッドモデルへの安全係数
FRICTION_FLAT_BPS = 1.0        # 一日信用の金利・貸株料等の丸め (往復)
SPREAD_FLOOR_BPS = 2.0         # メガキャップ実測 (1.5-5.3bps) 下限側の床
LIMIT_PROXIMITY = 0.03         # 日次値幅制限まで 3% 未満なら執行不能扱い (G7)

# ---- cheap gate 格子 (これが全探索。増やしたら G8 で N をインクリメント) ----------

MODELS: tuple[str, ...] = ("linear", "lgbm_rank")
TOP_KS: tuple[int, ...] = (5, 10)   # long 上位 K + short 下位 K

LGBM_RANK_PARAMS = {
    "objective": "lambdarank",
    "metric": "ndcg",
    "ndcg_eval_at": [10],
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_data_in_leaf": 200,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "max_bin": 63,
    "label_gain": [0, 1, 3, 7, 15],   # 五分位ラベル 0..4
    "verbosity": -1,
    "seed": 20260716,
    "deterministic": True,
}
LGBM_RANK_ROUNDS = 300
RIDGE_LAMBDA = 10.0
RANK_LABEL_BINS = 5

# ---- val 候補条件 (1 つでも落ちたら候補外) ---------------------------------------

CANDIDATE_MIN_N = 300
CANDIDATE_MIN_D = 60           # owner 指定: D >= 60
CANDIDATE_MIN_RATIO = 3.0      # G6
CANDIDATE_MAX_DAY_SHARE = 0.30    # G3
CANDIDATE_MAX_CODE_SHARE = 0.30   # G3
CANDIDATE_MIN_DECILE_SPEARMAN = 0.8   # 予測 decile vs 実現 adj gross の単調性
NULL_SHUFFLES = 200            # G2: 同日・同時刻・ランダム銘柄ヌル


def config_hash() -> str:
    payload = {
        "family": FAMILY,
        "splits": {"train": TRAIN_RANGE, "val": VAL_RANGE, "oos": OOS_RANGE},
        "minute_coverage_req": MINUTE_COVERAGE_REQ,
        "universe": {
            "size": UNIVERSE_SIZE,
            "window": LIQUIDITY_WINDOW_DAYS,
            "min_close": MIN_MEDIAN_CLOSE,
            "market": PRIME_MARKET_CODE,
        },
        "decision_tods": DECISION_TODS,
        "horizon_min": HORIZON_MIN,
        "entry_max_delay_s": ENTRY_MAX_DELAY_S,
        "sector_min_members": SECTOR_MIN_MEMBERS,
        "friction": {
            "safety": FRICTION_SAFETY,
            "flat_bps": FRICTION_FLAT_BPS,
            "floor_bps": SPREAD_FLOOR_BPS,
            "limit_proximity": LIMIT_PROXIMITY,
        },
        "grid": {"models": MODELS, "top_ks": TOP_KS},
        "lgbm": LGBM_RANK_PARAMS,
        "rounds": LGBM_RANK_ROUNDS,
        "ridge_lambda": RIDGE_LAMBDA,
        "rank_label_bins": RANK_LABEL_BINS,
        "candidate": {
            "min_n": CANDIDATE_MIN_N,
            "min_d": CANDIDATE_MIN_D,
            "min_ratio": CANDIDATE_MIN_RATIO,
            "max_day_share": CANDIDATE_MAX_DAY_SHARE,
            "max_code_share": CANDIDATE_MAX_CODE_SHARE,
            "min_decile_spearman": CANDIDATE_MIN_DECILE_SPEARMAN,
        },
    }
    return hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()
