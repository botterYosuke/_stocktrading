"""足読み gen5 (`gen5_tod_lag_v1`) — 同時刻 lag 横断ランキング。pure・numpy のみ。

gen4 との違いは signal のみ: 直近 15〜60 分の値動きではなく「過去営業日の同一
30 分枠」の市場・業種控除後リターン (gen4 dataset の h30_adj_bps) の lag 1〜40
営業日系列を EWMA (half-life 5 日 / 20 日) したものを score にする。
根拠: 30 分リターンの横断的継続が 1 営業日の整数倍 lag (同時刻帯) で発生し
40 営業日以上持続する (arXiv:1005.3535)。

継承 (gen4 = `gen4_xsec_v1` から): universe / train・val 分割 / friction モデル /
判断時刻 5 枠 (09:30, 10:00, 10:30, 13:00, 14:00) / sealed OOS (2025-04 開けない)。
入力は gen4 の凍結キャッシュ dataset_isval.parquet — 新しい分足走査はしない。

因果性: day d・tod T の h30_adj_bps は d 当日 T+30 分に確定するため、lag >= 1
営業日の値はすべて day d の判断時刻 T までに既知。

「同時刻だから効く」の証明が本体 — 対照 3 種:
1. 別時刻 lag (rotation): 各 tod の score を別 tod の lag 系列で計算
2. 時刻 permutation: tod 対応の全 non-identity 置換 (5! - 1 = 119) をヌル分布に
3. ランダム銘柄: gen4 evaluate.null_gap を流用 (G2)

G8 (honest-N): 格子は {hl5, hl20} × {K5, K10} の 4 セル・horizon 30 分のみに
固定し、config_hash で凍結してから val を評価する。
"""
from __future__ import annotations

import hashlib
from itertools import permutations
from pathlib import Path

import numpy as np
import orjson

from scalp_agent_bars.xsec.config import (
    CANDIDATE_MAX_CODE_SHARE,
    CANDIDATE_MAX_DAY_SHARE,
    CANDIDATE_MIN_D,
    CANDIDATE_MIN_DECILE_SPEARMAN,
    CANDIDATE_MIN_N,
    CANDIDATE_MIN_RATIO,
)
from scalp_agent_bars.xsec.config import config_hash as gen4_config_hash

FAMILY = "gen5_tod_lag_v1"

ART = Path("artifacts/gen5_tod_lag")

# ---- signal (これが全探索。増やしたら G8 で N をインクリメント) -------------------

LAG_MAX = 40                   # 論文の持続レンジ (>= 40 営業日) をそのまま上限に
HALF_LIVES: tuple[float, ...] = (5.0, 20.0)
TOP_KS: tuple[int, ...] = (5, 10)
HORIZON = 30                   # 次バー open 入り → 30 分後 open 決済 (owner 指定)
MIN_VALID_LAGS = 20            # lag 40 本中 20 本未満しか観測が無い行は signal なし

# ---- 対照の候補条件 (val を見る前に凍結) -----------------------------------------

PERM_P_MAX = 0.05              # 時刻 permutation ヌルに対する net/entry の p 値
# 別時刻 lag 対照: 実測 gross/entry が rotation 4 種の gross/entry 平均を上回ること


def config_hash() -> str:
    payload = {
        "family": FAMILY,
        "inherits": gen4_config_hash(),
        "lag_max": LAG_MAX,
        "half_lives": HALF_LIVES,
        "top_ks": TOP_KS,
        "horizon": HORIZON,
        "min_valid_lags": MIN_VALID_LAGS,
        "controls": {
            "rotation": "all nonzero shifts",
            "permutation": "all non-identity tod permutations",
            "perm_p_max": PERM_P_MAX,
            "rotation_rule": "gross_per_entry > mean(rotation gross_per_entry)",
        },
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


# ---- pivot ----------------------------------------------------------------------

def pivot_series(
    code: np.ndarray, tod: np.ndarray, day: np.ndarray, values: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]:
    """(day, tod, code) 行 → (code × tod) 系列 × 営業日 の行列。

    戻り値: (mat, code_idx, tod_idx, day_idx, n_tod)。
    mat[code_i * n_tod + tod_i, day_i] = values の該当行 (欠測は NaN)。
    day 軸は dataset に現れる営業日の昇順 (= グローバル営業日インデックス)。
    """
    days_u, day_idx = np.unique(day.astype(str), return_inverse=True)
    tods_u, tod_idx = np.unique(tod.astype(np.float64), return_inverse=True)
    codes_u, code_idx = np.unique(code.astype(str), return_inverse=True)
    n_tod = len(tods_u)
    mat = np.full((len(codes_u) * n_tod, len(days_u)), np.nan)
    mat[code_idx * n_tod + tod_idx, day_idx] = np.asarray(values, dtype=np.float64)
    return mat, code_idx, tod_idx, day_idx, n_tod


# ---- EWMA lag signal --------------------------------------------------------------

def ewma_lag_signal(
    mat: np.ndarray, half_life: float,
    lag_max: int = LAG_MAX, min_valid: int = MIN_VALID_LAGS,
) -> np.ndarray:
    """同時刻 lag 系列の EWMA。sig[:, d] は day d の判断時点で既知の値のみ使う。

    sig[s, d] = Σ_{L=1..lag_max} w_L·mat[s, d-L] / Σ w_L  (w_L = 0.5^(L/half_life)、
    NaN の lag は分子分母から除外)。有効 lag が min_valid 本未満なら NaN。
    """
    n_s, n_d = mat.shape
    num = np.zeros((n_s, n_d))
    den = np.zeros((n_s, n_d))
    cnt = np.zeros((n_s, n_d), dtype=np.int32)
    for lag in range(1, lag_max + 1):
        if lag >= n_d:
            break
        w = 0.5 ** (lag / half_life)
        src = mat[:, : n_d - lag]
        fin = np.isfinite(src)
        num[:, lag:] += np.where(fin, src, 0.0) * w
        den[:, lag:] += fin * w
        cnt[:, lag:] += fin
    with np.errstate(invalid="ignore", divide="ignore"):
        sig = num / den
    sig[(cnt < min_valid) | (den <= 0.0)] = np.nan
    return sig


# ---- score gather (対照は tod の置換で表現) ---------------------------------------

def identity_perm(n_tod: int) -> np.ndarray:
    return np.arange(n_tod)


def rotation_perm(n_tod: int, shift: int) -> np.ndarray:
    """別時刻 lag 対照: tod_i の score を tod_{(i+shift) mod n} の lag 系列で計算。"""
    return (np.arange(n_tod) + shift) % n_tod


def all_tod_permutations(n_tod: int) -> list[tuple[int, ...]]:
    """時刻 permutation 対照: identity を除く全置換 (n_tod=5 で 119 通り)。"""
    ident = tuple(range(n_tod))
    return [p for p in permutations(range(n_tod)) if p != ident]


def perm_scores(
    sig: np.ndarray, code_idx: np.ndarray, tod_idx: np.ndarray,
    day_idx: np.ndarray, n_tod: int, perm: np.ndarray,
) -> np.ndarray:
    """各行の score = sig[(code, perm[tod]), day]。identity なら本命 signal。"""
    perm = np.asarray(perm)
    return sig[code_idx * n_tod + perm[tod_idx], day_idx]


# ---- 候補判定 (val を見る前に凍結した条件) ----------------------------------------

def is_candidate(m: dict, deciles: dict, controls: dict) -> bool:
    """gen4 の val 候補条件 + gen5 の対照 2 条件。1 つでも落ちたら候補外。"""
    sp = deciles.get("spearman")
    nul = m.get("null", {})
    rot_gross = controls.get("rotation_gross_mean")
    perm_p = controls.get("perm_p_net")
    return (
        m["n"] >= CANDIDATE_MIN_N
        and m["D"] >= CANDIDATE_MIN_D
        and m["net_per_entry"] is not None and m["net_per_entry"] > 0
        and m["ratio"] is not None and m["ratio"] >= CANDIDATE_MIN_RATIO
        and (m["max_day_share"] is None or m["max_day_share"] <= CANDIDATE_MAX_DAY_SHARE)
        and (m["max_code_share"] is None or m["max_code_share"] <= CANDIDATE_MAX_CODE_SHARE)
        and sp is not None and sp >= CANDIDATE_MIN_DECILE_SPEARMAN
        and nul.get("gap_z") is not None and nul["gap_z"] >= 2.0
        and rot_gross is not None and m["gross_per_entry"] is not None
        and m["gross_per_entry"] > rot_gross
        and perm_p is not None and perm_p < PERM_P_MAX
    )
