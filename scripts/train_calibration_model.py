"""較正専用暫定モデル (shadow) の学習。2026-07-16 owner 確定の位置づけ:

- h5s × m3.0 × τ0.70 は post-selection の刺激生成器であり、採用戦略ではない
- PnL・edge・hit・ratio・セル優劣・閾値調整には使用禁止 (G8 honest-N 非消費)
- gen1 の再採点・07-14 開封・IS-KILL 撤回は行わない
  (学習は 07-09 + 07-13 のみ。role "train+val" を assert し OOS 日には触れない)

usage: uv run python scripts/train_calibration_model.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scalp_agent import loader  # noqa: E402
from scalp_agent.config import (  # noqa: E402
    LGBM_NUM_BOOST_ROUND,
    LGBM_PARAMS,
    assert_days_role,
    assert_no_day_leakage,
    cell_key,
)
from scalp_agent.dataset import ensure_cache, load_cache, training_arrays  # noqa: E402
from scalp_agent.runtime.calibration import (  # noqa: E402
    CAL_HORIZON_S,
    CAL_MULT,
    CAL_TRAIN_DAYS,
    META_PATH,
    MODEL_DIR,
    MODEL_PATH,
    model_meta,
)


def main() -> None:
    assert_no_day_leakage()
    assert_days_role(CAL_TRAIN_DAYS, "train+val")  # OOS 日 (07-14) には触れない

    tables = {}
    for day in CAL_TRAIN_DAYS:
        source_ok = loader.db_path(day).exists()
        if source_ok:
            codes = loader.list_codes(day)
            get = ensure_cache
        else:
            # S: 不在でもローカル parquet キャッシュがあれば学習できる
            # (ensure_cache は録画 fingerprint を要求するため load_cache 直読み)
            cache_day_dir = Path("artifacts/cache/gen1") / day
            codes = sorted(p.stem for p in cache_day_dir.glob("*.parquet"))
            if not codes:
                raise SystemExit(f"{day}: 録画もキャッシュも見つからない")
            get = load_cache
        for i, code in enumerate(codes, 1):
            tables[(day, code)] = get(day, code)
            print(f"  [{day}] {i}/{len(codes)} {code}", flush=True)

    x, y = training_arrays(tables, CAL_HORIZON_S, CAL_MULT)
    if len(y) < 1000 or len(np.unique(y)) < 3:
        raise SystemExit(f"退化した学習集合 (n={len(y)}) — 学習中止")
    print(f"training: cell={cell_key(CAL_HORIZON_S, CAL_MULT)} n={len(y)} "
          f"class_counts={np.bincount(y).tolist()}", flush=True)
    ds = lgb.Dataset(x, label=y, params={"max_bin": LGBM_PARAMS["max_bin"]})
    booster = lgb.train(LGBM_PARAMS, ds, num_boost_round=LGBM_NUM_BOOST_ROUND)

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(MODEL_PATH))
    meta = model_meta()
    meta["n_train_rows"] = int(len(y))
    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"saved: {MODEL_PATH}\nmeta : {META_PATH}", flush=True)
    print("NOTE: calibration-only。この booster の成績を判定・台帳・セル選択に使わないこと。",
          flush=True)


if __name__ == "__main__":
    main()
