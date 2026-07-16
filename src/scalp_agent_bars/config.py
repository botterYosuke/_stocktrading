"""gen1b (兄弟 family: bars) の凍結設定。

gen1 との差分だけをここに置き、共通の凍結値 (mult / τ / 日割り / LightGBM /
執行定数) は scalp_agent.config から再輸出する。gen1b は **新 family** であり、
台帳の honest-N を 1 消費する (G8)。

- 特徴量: 板 (bid_*/ask_*) を一切使わない。last_px/volume 由来の1分足 +
  前日日足コンテキストのみ
- 決定グリッド: 1分足の確定境界 (取引のあったバーのみ)。gen1 の 1Hz に対応
- 凍結格子: horizon 6 × mult 4 × τ 5 = 120 セル (gen1 と同数・horizon のみ分スケール)
- ラベル/執行/候補条件/タイブレークは gen1 の凍結仕様をそのまま共有
"""
from __future__ import annotations

import hashlib

import orjson

from scalp_agent.config import (  # noqa: F401  (gen1b の正本として再輸出)
    CANDIDATE_MIN_N,
    CANDIDATE_MIN_RATIO,
    ENTRY_END_TOD,
    ENTRY_MAX_LATENCY_S,
    FORCE_CLOSE_TOD,
    IS_TRAIN_DAYS,
    IS_VAL_DAYS,
    LGBM_NUM_BOOST_ROUND,
    LGBM_PARAMS,
    MULTS,
    OOS_DAYS,
    SESSION_AFTERNOON,
    SESSION_MORNING,
    TAUS,
    assert_days_role,
    assert_no_day_leakage,
    cell_key,
)

# ---- gen1b 固有の凍結値 -------------------------------------------------------

BAR_S = 60.0  # 1分足

# 分足スケールのホライズン (秒)。gen1 と同じく 6 点・事前固定・掃引しない。
HORIZONS_S: tuple[float, ...] = (60.0, 120.0, 180.0, 300.0, 600.0, 900.0)

# 前営業日 (録画が存在する直前日) の凍結マップ。日足特徴量の参照先。
# 未録画日や整合性未検分の日 (例: 台帳外の 07-10) を暗黙に拾わないため明示する。
PREV_DAY: dict[str, str | None] = {
    "2026-07-09": None,          # 最初の録画日 — 日足特徴量は欠損 (NaN)
    "2026-07-13": "2026-07-09",
    "2026-07-14": "2026-07-13",
}

# 出来高正規化: 直前 VOL_MED_BARS 本 (現在バー除く) の trailing median。
# 有効な過去バーが VOL_MED_MIN_BARS 未満なら欠損 (NaN)。
VOL_MED_BARS = 30
VOL_MED_MIN_BARS = 10


def grid_cells() -> list[tuple[float, float]]:
    """(horizon_s, mult) の 24 組。"""
    return [(h, m) for h in HORIZONS_S for m in MULTS]


def grid_cells_full() -> list[tuple[float, float, float]]:
    """(horizon_s, mult, tau) 120 セル。"""
    return [(h, m, t) for h in HORIZONS_S for m in MULTS for t in TAUS]


def config_hash() -> str:
    """gen1b 設定一式の sha256。テストで値を固定する。"""
    payload = {
        "family": "gen1b_bars_v1",
        "bar_s": BAR_S,
        "lgbm": LGBM_PARAMS,
        "rounds": LGBM_NUM_BOOST_ROUND,
        "horizons": HORIZONS_S,
        "mults": MULTS,
        "taus": TAUS,
        "days": {"train": IS_TRAIN_DAYS, "val": IS_VAL_DAYS, "oos": OOS_DAYS},
        "prev_day": PREV_DAY,
        "vol_med_bars": VOL_MED_BARS,
        "vol_med_min_bars": VOL_MED_MIN_BARS,
        "entry_max_latency_s": ENTRY_MAX_LATENCY_S,
        "force_close_tod": FORCE_CLOSE_TOD,
    }
    return hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()
