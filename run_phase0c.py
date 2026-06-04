"""Phase 0c runner (#8 C8+C9): panel -> OOS predict -> accept/reject gate.

Thin orchestration around the additive Phase 0c modules. Does NOT touch the LSTM
``model_manager`` / ``signals_writer`` path — this only produces the GO / REJECT /
DOWNGRADE verdict for the LightGBM limit-MM strategy.

Two gates (pre-registration, handoff §8):
  * DEV   — grouped-CV OOS over the dev window (R1-safe split) + a leakage smoke
            (grouped vs timeseries vs the leaky naive_kfold baseline). If the
            honest combined return collapses vs naive_kfold, the edge was
            leakage (#8 C9 pre-reg #4) -> REJECT.
  * HELD-OUT (1-shot, pre-reg #5) — fit ONCE on the full dev panel, apply ONCE to
            a frozen held-out window never seen during dev. Universe for the
            held-out panel is selected PIT as of the dev end (``--universe-as-of``)
            so membership carries no look-ahead.

Both panels cache to parquet (``--cache-prefix``) so the slow decompress+featurize
is a one-time cost. Production (Windows) runs numba jit; this dev box uses the
pure-Python fallback.

Usage (full OOS):
  export DEV_J_QUANTS_CACHE=/Users/sasac/SynologyDrive/StockData/j-quants
  uv run --with numpy --with pandas --with scipy --with scikit-learn \\
      --with lightgbm --with TA-Lib --with pyarrow --with PyYAML \\
      python run_phase0c.py --start 2024-01-01 --end 2025-09-30 \\
      --heldout-start 2025-10-01 --heldout-end 2026-02-28 \\
      --universe-as-of 2025-09-30 --top-n 100 --cache-dir "$DEV_J_QUANTS_CACHE" \\
      --cache-prefix data/panel_phase0c
"""
from __future__ import annotations

import argparse
import os

import yaml

from backtest import evaluate_gate
from cost_model import CostModel
from model_lgbm import make_splitter, oos_predict, predict_with, train_models
from panel_builder import build_panel


def _load_cost_model(config_path: str) -> CostModel:
    if config_path and os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as fh:
            return CostModel.from_config_dict(yaml.safe_load(fh) or {})
    return CostModel.from_config_dict({})


def _build(label, *, start, end, cost_model, top_n, cache_dir, universe_as_of, cache_path):
    print(f"[panel:{label}] build {start}..{end} top_n={top_n} as_of={universe_as_of} "
          f"cache={cache_path}")
    panel = build_panel(
        start=start, end=end, cost_model=cost_model, top_n=top_n, cache_dir=cache_dir,
        universe_as_of=universe_as_of, panel_cache=cache_path,
    )
    print(f"[panel:{label}] rows={len(panel)} codes={panel['code'].nunique() if len(panel) else 0} "
          f"timestamps={panel['timestamp'].nunique() if len(panel) else 0}")
    return panel


def run(
    *,
    start: str,
    end: str,
    top_n: int,
    cache_dir: str | None,
    config_path: str = "config.yaml",
    cache_prefix: str | None = None,
    heldout_start: str | None = None,
    heldout_end: str | None = None,
    universe_as_of: str | None = None,
    n_splits: int = 5,
):
    cost_model = _load_cost_model(config_path)
    if cache_prefix:
        parent = os.path.dirname(cache_prefix)
        if parent:
            os.makedirs(parent, exist_ok=True)

    dev = _build(
        "dev", start=start, end=end, cost_model=cost_model, top_n=top_n,
        cache_dir=cache_dir, universe_as_of=None,
        cache_path=f"{cache_prefix}_dev.parquet" if cache_prefix else None,
    )
    if len(dev) == 0:
        print("[dev] empty — nothing to evaluate")
        return None

    # --- DEV gate: R1-safe grouped OOS + leakage smoke -----------------------
    dev_preds = oos_predict(dev, splitter=make_splitter("grouped", n_splits=n_splits))
    dev_result = evaluate_gate(dev_preds, cost_model)
    for kind in ("grouped", "timeseries", "naive_kfold"):
        p = oos_predict(dev, splitter=make_splitter(kind, n_splits=n_splits))
        dev_result.leakage_smoke[kind] = evaluate_gate(p, cost_model).sides["combined"].mean_ret
    print("\n===== DEV (grouped-CV OOS) =====")
    print(dev_result)

    # --- HELD-OUT 1-shot: fit once on full dev, apply once -------------------
    held_result = None
    if heldout_start and heldout_end:
        held = _build(
            "heldout", start=heldout_start, end=heldout_end, cost_model=cost_model,
            top_n=top_n, cache_dir=cache_dir, universe_as_of=universe_as_of,
            cache_path=f"{cache_prefix}_heldout.parquet" if cache_prefix else None,
        )
        if len(held) == 0:
            print("[heldout] empty — skipping 1-shot")
        else:
            models = train_models(dev)  # full-dev fit (the live model)
            held_preds = predict_with(models, held)
            held_result = evaluate_gate(held_preds, cost_model)
            print("\n===== HELD-OUT (1-shot) =====")
            print(held_result)
            dev_sign = dev_result.sides["combined"].mean_ret
            ho_sign = held_result.sides["combined"].mean_ret
            consistent = (dev_sign > 0) == (ho_sign > 0)
            print(f"\nsign-consistency dev vs held-out: "
                  f"{'OK' if consistent else 'FLIP'} "
                  f"(dev={dev_sign:+.6f}, heldout={ho_sign:+.6f})")

    return dev_result, held_result


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 0c C8+C9 accept/reject gate")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--heldout-start", default=None)
    ap.add_argument("--heldout-end", default=None)
    ap.add_argument("--universe-as-of", default=None, help="PIT universe cutoff for held-out panel")
    ap.add_argument("--top-n", type=int, default=100)
    ap.add_argument("--cache-dir", default=os.environ.get("DEV_J_QUANTS_CACHE"))
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--cache-prefix", default=None, help="parquet path prefix to cache built panels")
    ap.add_argument("--n-splits", type=int, default=5)
    args = ap.parse_args()
    run(
        start=args.start, end=args.end, top_n=args.top_n, cache_dir=args.cache_dir,
        config_path=args.config, cache_prefix=args.cache_prefix,
        heldout_start=args.heldout_start, heldout_end=args.heldout_end,
        universe_as_of=args.universe_as_of, n_splits=args.n_splits,
    )


if __name__ == "__main__":
    main()
