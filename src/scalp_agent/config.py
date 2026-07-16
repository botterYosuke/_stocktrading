"""第一世代 (gen1) の凍結設定。2026-07-16 グリル確定仕様の正本コード。

- 凍結格子: horizon 6 × mult 4 × τ 5 = 120 セル (τ は離散 5 点、連続範囲ではない)
- 日割り: IS-train=07-09 / IS-val=07-13 / OOS=07-14。役割の混同はコードで拒否する
- LightGBM は固定パラメータ・掃引しない。config_hash がテストで固定される
- softmax 出力は較正済み確率ではなく未較正 score として扱う (変数名も score)
"""
from __future__ import annotations

import hashlib

import orjson

# ---- 凍結格子 -------------------------------------------------------------

HORIZONS_S: tuple[float, ...] = (5.0, 15.0, 30.0, 60.0, 120.0, 300.0)
MULTS: tuple[float, ...] = (1.5, 2.0, 2.5, 3.0)
TAUS: tuple[float, ...] = (0.40, 0.50, 0.60, 0.70, 0.80)


def grid_cells() -> list[tuple[float, float]]:
    """(horizon_s, mult) の 24 組。ラベル/モデルはこの単位、τ は執行側。"""
    return [(h, m) for h in HORIZONS_S for m in MULTS]


def grid_cells_full() -> list[tuple[float, float, float]]:
    """(horizon_s, mult, tau) 120 セル。"""
    return [(h, m, t) for h in HORIZONS_S for m in MULTS for t in TAUS]


def cell_key(horizon_s: float, mult: float) -> str:
    """列名に使うセルキー。mult は 10 倍整数表記 (1.5 → m15)。"""
    return f"h{int(horizon_s)}_m{int(round(mult * 10))}"


# ---- 日割り (G5: IS/OOS 凍結) ---------------------------------------------

IS_TRAIN_DAYS: tuple[str, ...] = ("2026-07-09",)
IS_VAL_DAYS: tuple[str, ...] = ("2026-07-13",)
OOS_DAYS: tuple[str, ...] = ("2026-07-14",)


def assert_no_day_leakage() -> None:
    """train/val/OOS の日付集合が互いに素であることを保証する。"""
    tr, va, oo = set(IS_TRAIN_DAYS), set(IS_VAL_DAYS), set(OOS_DAYS)
    if tr & va or tr & oo or va & oo:
        raise AssertionError(f"day leakage: train={tr} val={va} oos={oo}")


def assert_days_role(days: list[str] | tuple[str, ...], role: str) -> None:
    """データ組立時に日付が宣言された役割と一致するかを検査する。"""
    allowed = {
        "train": set(IS_TRAIN_DAYS),
        "val": set(IS_VAL_DAYS),
        "train+val": set(IS_TRAIN_DAYS) | set(IS_VAL_DAYS),
        "oos": set(OOS_DAYS),
    }[role]
    bad = set(days) - allowed
    if bad:
        raise AssertionError(f"days {sorted(bad)} not allowed for role '{role}'")


# ---- 候補条件と凍結タイブレーク --------------------------------------------

CANDIDATE_MIN_N = 100
CANDIDATE_MIN_RATIO = 3.0
# タイブレーク: n 多 → horizon 短 → mult 小 → τ 高 (net/entry 同値のとき)


# ---- 執行規則の定数 ---------------------------------------------------------

ENTRY_MAX_LATENCY_S = 2.0   # 決定境界からエントリ fill PUSH までの最大許容遅延
SESSION_MORNING = (9 * 3600.0, 11 * 3600.0 + 30 * 60.0)      # [09:00, 11:30)
SESSION_AFTERNOON = (12 * 3600.0 + 30 * 60.0, 15 * 3600.0 + 20 * 60.0)  # [12:30, 15:20]
FORCE_CLOSE_TOD = 14 * 3600.0 + 55 * 60.0                     # 14:55 強制決済
ENTRY_END_TOD = FORCE_CLOSE_TOD                               # 新規判定は 14:55 より前


# ---- 特徴量正規化の定数 ------------------------------------------------------

MEDIAN_WINDOW_S = 300.0   # trailing median depth の窓 (直前の完了秒まで・日初リセット)
MEDIAN_MIN_S = 60         # これ未満の経過秒では欠損 (NaN)


# ---- LightGBM 固定設定 (掃引禁止) -------------------------------------------

LGBM_PARAMS: dict = {
    "objective": "multiclass",
    "num_class": 3,
    "learning_rate": 0.05,
    "num_leaves": 63,
    "max_depth": -1,
    "min_data_in_leaf": 200,
    "feature_fraction": 0.9,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l1": 0.0,
    "lambda_l2": 1.0,
    "max_bin": 255,
    "seed": 20260716,
    "deterministic": True,
    "force_row_wise": True,
    "verbosity": -1,
}
LGBM_NUM_BOOST_ROUND = 300


def config_hash() -> str:
    """LightGBM 固定設定 + 格子 + 日割りの sha256。テストで値を固定する。"""
    payload = {
        "lgbm": LGBM_PARAMS,
        "rounds": LGBM_NUM_BOOST_ROUND,
        "horizons": HORIZONS_S,
        "mults": MULTS,
        "taus": TAUS,
        "days": {"train": IS_TRAIN_DAYS, "val": IS_VAL_DAYS, "oos": OOS_DAYS},
        "entry_max_latency_s": ENTRY_MAX_LATENCY_S,
        "force_close_tod": FORCE_CLOSE_TOD,
    }
    return hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()


# ---- ヌル検定 ---------------------------------------------------------------

NULL_SEED = 20260714
NULL_N_SAMPLES = 200
NULL_BAND_S = 1800.0  # 同 30 分帯
