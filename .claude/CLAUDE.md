# stocktrading Claude Guide

Python workspace for validating Japanese stock day-trading strategies against
order-book data.

## External command center vault (`note`) -- check it if it exists

`D:\Documents\note` (Obsidian vault "note") is the cross-repo command center for all four
strategy repos (bellwether / blacksheep / sacrificial-lamb / stocktrading). If it exists on
this machine, check it before starting non-trivial work:

- `Projects/株価シュミレーション/株価シュミレーション.md` -- overall hypothesis, signal log, current bet
- `Projects/株価シュミレーション/リポジトリ変遷史.md` -- why the work split into 4 repos, when to use which
- `Projects/株価シュミレーション/戦略台帳.md` -- a cross-repo index of ~372 verdicts aggregated from
  the other three repos' logs and ledgers (heuristically extracted, v1 -- treat as a pointer,
  not ground truth). Useful for checking whether an idea was already tried and killed elsewhere
  before implementing it here.

## Project Facts

- Source in `src/stocktrading`, tests in `tests`.
- Medallion pipeline: `bronze` (raw, minimal interpretation) -> `silver`
  (normalized board events, mid/spread/imbalance) -> `gold` (per-tick signal
  output the strategy reads).
- `C:\Users\sasai\Documents\backcast` is external infrastructure. Do not edit it
  from this repository.
- Live execution currently offers only `submit_market`. Any strategy that needs
  limit orders cannot be run live yet.

## Development Rules

- Run `uv run pytest` before claiming an implementation is complete.
- `signals.py` must stay pure and I/O-free: the backtest and the future live cell
  share it. State is passed in and returned, never held in the module.
- The backtest and the gold writer must both derive targets from
  `signals.fold_states`. Never re-express the signal rule in SQL.
- Backtests reset position, signal state, fill-delay window and clock at every
  session boundary. Nothing survives the overnight gap.
- Inline SQL literals go through `sql.sql_str`. DuckDB cannot bind parameters
  inside `CREATE`/`COPY`.
- Never hand DuckDB bulk Python values (`executemany`, list parameters). The
  conversion is quadratic -- a 93k-row day costs ~10 minutes. Stage through its
  CSV reader instead.
- Be conservative in simulation. Where fill logic is uncertain, bias against the
  strategy.

## Reading Results

- Ranking configurations by net PnL rewards not trading: net approaches 0 from
  below as turnover falls. Judge a signal by **net per round trip**, and only
  over a defensible sample (`sweep --min-round-trips`).
- Low turnover is not evidence of alpha.
- **The symmetric trap**: ranking by *net per entry* rewards trading only **once**
  (on the one lucky day). A day-selector can "pick winning days" or "point at days
  that happened to win" -- the numbers look identical.
- Strategies are no longer scored alone. The unit is **(G, S) = day-selector x
  strategy**, scored on **net return per entry**, and it is only admissible after
  passing the G1-G8 guardrails (causality / matched null on the *selector* /
  concentration cap / min firings / IS-OOS freeze / friction ratio >= 3 /
  executability / honest-N). See **`docs/adr/ADR-0001-evaluation-standard.md`**
  (canonical; mirrored as pointers in the other three repos).

The standing conclusion about the imbalance signal's viability lives in
`docs/architecture.md`. Read it there rather than restating it here, and update
it there when the evidence changes.

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
