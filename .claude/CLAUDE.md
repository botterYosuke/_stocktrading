# stocktrading Claude Guide

This repository is a Python workspace for testing Japanese stock day-trading
strategies from order-book data. Keep Claude instructions short and tied to this
codebase; do not import rules from other strategy projects unless they are
actually used here.

## Project Facts

- Source code lives in `src/stocktrading`.
- Tests live in `tests`.
- The data pipeline is medallion-style:
  - `bronze`: copy/normalize raw source files with minimal interpretation.
  - `silver`: normalized board events and derived market columns.
  - `gold`: strategy-readable features and backtest inputs.
- `C:\Users\sasai\Documents\backcast` is external infrastructure. Do not edit it
  from this repository.
- Current live execution is constrained by `submit_market`; treat this as a
  strategy bottleneck, not as a permanent design assumption.

## Current Strategy Context

- Churn suppression is useful because it reduces taker losses, but it is not an
  edge by itself.
- The current imbalance taker strategy is structurally unprofitable after spread
  and commission. Do not present lower turnover as proof of alpha.
- The next important research direction is passive/maker execution: model limit
  orders, cancellations, conservative fills, queue disadvantage, latency,
  adverse selection, and spread capture.
- Any profitable-looking configuration with a tiny sample is suspect. Prefer
  minimum sample floors, cross-symbol checks, and explicit `net/trip` reporting.

## Development Rules

- Run `uv run pytest` before claiming implementation is complete.
- Keep signal functions pure where possible so backtest and live code can share
  them.
- Use shared SQL escaping helpers for dynamic SQL literals.
- Backtests must reset position, signal state, fill-delay window, and clock at
  each trading day/session boundary.
- Be conservative in simulation. If fill logic is uncertain, bias against the
  strategy.

## Useful Commands

```powershell
uv sync
uv run pytest
uv run python -m stocktrading.cli doctor
uv run python -m stocktrading.cli ingest-bronze --limit 1
uv run python -m stocktrading.cli build-silver --date 2026-07-09
uv run python -m stocktrading.cli backtest --symbol 9984 --date 2026-07-09
uv run python -m stocktrading.cli sweep --symbols 9984,285A,5803 --date 2026-07-09
```

