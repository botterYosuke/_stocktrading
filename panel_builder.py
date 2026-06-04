"""Cross-sectional 15-min panel builder (jpx_mlbot_15m, #8 Phase 0b / C7).

Stacks each universe instrument's per-bar features (C5) + FEP labels (C6) into a
single ``(code, timestamp)`` cross-sectional panel — the design judgement #2
pool that gives the LightGBM ranker (C8) and the p-mean test (C9) orders of
magnitude more samples than a single series.

Per-instrument processing is independent, so features stay causal within each
code (no cross-contamination at feature time). The ``timestamp`` column is kept
verbatim because C9 must split on it: a pooled panel leaks contemporaneously
across stocks (all codes at time *t* share market-wide moves), so the
pre-registered leakage guard (#8 R1) groups CV folds by ``timestamp`` — never by
stacked row. ``assemble_panel`` is pure (synthetic-bar testable); ``build_panel``
wires universe + load + resample around it (needs S:/j-quants, not unit-tested).
"""
from __future__ import annotations

import os

import pandas as pd

from features_intraday import FEATURES, calc_features
from labels import compute_labels
from minute_data_source import MinuteBar, load_minute_bars, resample_15min
from universe import select_universe

# Columns compute_labels (C6) appends, in declared order.
LABEL_COLUMNS: list[str] = [
    "buy_price", "sell_price", "buy_fep", "buy_fet", "sell_fep", "sell_fet",
    "buy_executed", "sell_executed", "y_buy", "y_sell", "buy_cost", "sell_cost",
]
_BASE_COLUMNS: list[str] = ["code", "timestamp", "open", "high", "low", "close", "volume"]
# Rows must have all features and both targets to be trainable (tutorial dropna).
_REQUIRED_NON_NULL: list[str] = FEATURES + ["y_buy", "y_sell"]


def _bars_to_frame(bars: list[MinuteBar]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": [b.timestamp for b in bars],
            "open": [float(b.open) for b in bars],
            "high": [float(b.high) for b in bars],
            "low": [float(b.low) for b in bars],
            "close": [float(b.close) for b in bars],
            "volume": [float(b.volume) for b in bars],
        }
    )


def _empty_panel() -> pd.DataFrame:
    return pd.DataFrame(columns=_BASE_COLUMNS + FEATURES + LABEL_COLUMNS)


def assemble_panel(
    panel_by_code: dict[str, list[MinuteBar]],
    cost_model,
    *,
    min_bars: int = 64,
    dropna: bool = True,
    **label_kwargs,
) -> pd.DataFrame:
    """Build the cross-sectional panel from per-code 15-min bar lists.

    Each code is featurized + labelled independently, then all are stacked and
    sorted by ``(timestamp, code)``. Codes with fewer than ``min_bars`` bars are
    skipped (features would be all-NaN warmup). With ``dropna`` (default), rows
    missing any feature or target are dropped — the trainable panel.
    """
    frames: list[pd.DataFrame] = []
    for code in sorted(panel_by_code):
        bars = panel_by_code[code]
        if len(bars) < min_bars:
            continue
        df = _bars_to_frame(bars)
        calc_features(df)
        compute_labels(df, cost_model, **label_kwargs)
        df.insert(0, "code", code)
        frames.append(df)

    if not frames:
        return _empty_panel()

    panel = pd.concat(frames, ignore_index=True)
    if dropna:
        panel = panel.dropna(subset=_REQUIRED_NON_NULL)
    # Deterministic, and groups every timestamp's cross-section together (R1).
    panel = panel.sort_values(["timestamp", "code"], kind="stable").reset_index(drop=True)
    return panel


def build_panel(
    *,
    start,
    end,
    cost_model,
    top_n: int = 100,
    universe_as_of=None,
    cache_dir=None,
    min_bars: int = 64,
    dropna: bool = True,
    panel_cache: str | None = None,
    **label_kwargs,
) -> pd.DataFrame:
    """End-to-end: select universe (PIT at ``universe_as_of``, default ``end``),
    load month-filtered minute bars over ``[start, end]``, resample to 15-min,
    and assemble the cross-sectional panel.

    ``panel_cache`` (optional parquet path) turns the slow decompress+featurize
    into a one-time cost: if the file exists it is loaded and returned verbatim;
    otherwise the panel is built and written there. The caller owns the key —
    use a distinct path per ``(start, end, top_n, as_of, label_kwargs)`` (handoff
    §5b). Requires ``pyarrow``/``fastparquet`` only when ``panel_cache`` is set."""
    if panel_cache is not None and os.path.exists(panel_cache):
        return pd.read_parquet(panel_cache)

    as_of = universe_as_of if universe_as_of is not None else end
    codes = select_universe(as_of=as_of, top_n=top_n, cache_dir=cache_dir)
    bars_by_code = load_minute_bars(
        cache_dir=cache_dir, start=start, end=end, codes=codes
    )
    panel_by_code = resample_15min(bars_by_code)
    panel = assemble_panel(
        panel_by_code, cost_model, min_bars=min_bars, dropna=dropna, **label_kwargs
    )
    if panel_cache is not None:
        panel.to_parquet(panel_cache, index=False)
    return panel
