"""Phase 0c runner (#8 C8+C9): panel -> OOS predict -> accept/reject gate.

Thin orchestration around the additive Phase 0c modules. Does NOT touch the LSTM
``model_manager`` / ``signals_writer`` path — this only produces the GO / REJECT /
DOWNGRADE verdict for the LightGBM limit-MM strategy.

  1. ``build_panel`` (cross-sectional 15-min panel; optional parquet cache),
  2. ``oos_predict`` with the R1-safe ``grouped`` split -> the headline gate,
  3. a leakage smoke: re-run the gate under ``timeseries`` and the leaky
     ``naive_kfold`` baseline. If the honest (grouped/timeseries) combined return
     collapses vs naive_kfold, the edge was leakage (#8 C9 pre-reg #4) -> REJECT.

Usage (small smoke):
  export DEV_J_QUANTS_CACHE=/Users/sasac/SynologyDrive/StockData/j-quants
  uv run --with numpy --with pandas --with scikit-learn --with lightgbm --with TA-Lib \\
      python run_phase0c.py --start 2024-01-01 --end 2024-01-31 --top-n 10 \\
      --cache-dir "$DEV_J_QUANTS_CACHE"
"""
from __future__ import annotations

import argparse
import os

import yaml

from backtest import evaluate_gate
from cost_model import CostModel
from model_lgbm import make_splitter, oos_predict
from panel_builder import build_panel


def _load_cost_model(config_path: str) -> CostModel:
    if config_path and os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as fh:
            return CostModel.from_config_dict(yaml.safe_load(fh) or {})
    return CostModel.from_config_dict({})


def run(
    *,
    start: str,
    end: str,
    top_n: int,
    cache_dir: str | None,
    config_path: str = "config.yaml",
    panel_cache: str | None = None,
    n_splits: int = 5,
):
    cost_model = _load_cost_model(config_path)
    print(f"[panel] build {start}..{end} top_n={top_n} cache_dir={cache_dir}")
    panel = build_panel(
        start=start, end=end, cost_model=cost_model, top_n=top_n,
        cache_dir=cache_dir, panel_cache=panel_cache,
    )
    print(f"[panel] rows={len(panel)} codes={panel['code'].nunique()} "
          f"timestamps={panel['timestamp'].nunique()}")
    if len(panel) == 0:
        print("[panel] empty — nothing to evaluate")
        return None

    # Headline gate: R1-safe grouped split.
    preds = oos_predict(panel, splitter=make_splitter("grouped", n_splits=n_splits))
    result = evaluate_gate(preds, cost_model)

    # Leakage smoke: honest vs leaky. Combined selected-return mean per split.
    for kind in ("grouped", "timeseries", "naive_kfold"):
        p = oos_predict(panel, splitter=make_splitter(kind, n_splits=n_splits))
        result.leakage_smoke[kind] = evaluate_gate(p, cost_model).sides["combined"].mean_ret

    print(result)
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 0c C8+C9 accept/reject gate")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--top-n", type=int, default=100)
    ap.add_argument("--cache-dir", default=os.environ.get("DEV_J_QUANTS_CACHE"))
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--panel-cache", default=None, help="parquet path to cache the built panel")
    ap.add_argument("--n-splits", type=int, default=5)
    args = ap.parse_args()
    run(
        start=args.start, end=args.end, top_n=args.top_n, cache_dir=args.cache_dir,
        config_path=args.config, panel_cache=args.panel_cache, n_splits=args.n_splits,
    )


if __name__ == "__main__":
    main()
