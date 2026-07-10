---
name: sacrificial-lamb-replay
description: Workflow for running, ingesting, and reporting on strategy baseline replays in the _sacrificial-lamb / The-Trader-Was-Replaced project. Use this skill whenever the user wants to run a strategy replay, switch between SCENARIO windows, ingest a run into Bronze/Silver, compute basket fill rate, exit reason breakdown, PnL concentration, or compile the §6-format baseline report. Also use when debugging AccountBalanceNegative errors, trade log buffering issues, or PnL discrepancies between engine output and trade log. Also use when extending the TTWR catalog (adding new date ranges to jquants-catalog), running multi-window re-evaluation sweeps, or when a strategy produces 0 trades unexpectedly after a --start/--end override. Also use when: setting up replay after a TTWR submodule update, debugging "SCENARIO has unknown keys" errors, creating or updating sidecar JSON files (strategies/*.json), or when instruments_ref is not being resolved (bars stream but trade_count=0). Also covers parameter sweeps and cross-run optimization (絶対に: 「戦略をスイープして最適化して」「最も利益が出るパラメータを探して」「どのパラメータが一番儲かるか」「既存ランの結果を比較して」「win_rate や PnL が最高の設定は」と言われたら起動): running sweeps via sweep_runner.py / run_<strategy>_sweep.ps1, comparing Silver runs with compare_runs.py, W&B publish + parallel-coordinates comparison, adoption criteria, and recording results to wiki (B-5) and docs/plan/<strategy>-proposals.md (B-6 format). 実装指示書 (task ファイル) に「B-6 形式で proposals に追記」「compare_runs.py で sweep レポート」と書かれている場合も起動する。 また **offline 解析ラボ（Phase -1, strategy コードゼロ・replay なし）** にも使う: 「base rate を測って」「separability probe」「offline 解析 script を実行して」「cross-sectional ranker を学習して」「機械判定だけ返して」「friction 壁を超えるか測って」「exit-timing/利確 sweep」「既存 harness に最小追記して」「EVLAB_MODE で A/B」「EVLAB_PHASE で phase ゲート」「risk 層 / 資産管理 / pick-weighting / no-trade を Sharpe・CAGR で A/B」「日次ポートフォリオ複利層」「baseline cell を EXACT 再現して regression 確認」「OOS / fresh forward / cross-period で edge が持続するか検証して」「per-dt refit を train≤YYYYMM で fit し OOS に 1-shot apply」「leak assert を仕込んで」「era-overfit か判定して」「再ランク / rerank を OOS で回して」と言われたら起動する。**practical track（gh #9 down-thrust-scalp / #11 rejection-scalp、strategy コードゼロ・replay engine なしの純 offline sim）** も同じ規範で本スキル参照: 「凍結 setup を cheap-gate に通して」「friction 3段で backtest して」「day-clustered t / 集中度 / matched-firing NULL-control（random・hold-to-close）を出して」「bucket × phenotype のセル別 OOS」「回帰アンカー（9107 2022-10-26）を再現して」と言われたら起動。実装資産は `scripts/practical_{rule_backtest,situation_search,expectancy_report,setup_backtest,setup_analyze}.py` ＋ `data/universe/*` ＋ jquants-catalog parquet（`S:\artifacts\artifacts\jquants-catalog\data\bar`、canonical loader=`_load_day_bars`/`_decode_i64` を verbatim import・検出器/proxy 改変禁止）。friction は notional ¥100M に対し realistic≈20bps RT(+half-spread)/保守端≈60bps の 3 段で net=gross−friction を reconcile（per-trade gross が ¥200k 級の friction を超えるかが wall）。NULL-control は `--null-mode random|hold --rule-trades <rule jsonl>` の matched-firing で同一 (code,day) のみ発火。catalog は split 非調整・低位株含むので bps proxy には必ず eligibility gate（|gap_bps|≤2500 / close≥¥500 / 方向ガード）を通す。offline ラボの定型: (1) minute bar は **Mac なら `/Volumes/StockData/j-quants/equities_bars_minute_YYYYMM.csv.gz`**（未マウントは `osascript -e 'mount volume "afp://sasaco-ds218._afpovertcp._tcp.local/StockData/"'`）／Windows なら `S:\j-quants\...` を gzip 直読み（列 Date,Time,Code,O,H,L,C,Vo,Va; tuple index は time=0,O=1,H=2,L=3,C=4,Vo=5,Va=6; Code=ticker 4桁zero-pad+末尾0）。daily ADV/sigma は `equities_bars_daily_*.csv.gz`（`Code` dtype=str+zfill(5)、`132A0` 等英数字コード混在で int cast 禁止、trailing 20d を shift(1) で当日除外）。python は Mac `/Users/sasac/_sacrificial-lamb/.venv/bin/python`。(2) 全 universe × 全月の構築/path 再構築は **数十分級・disk-bound**（v23 probe 実測: 18 月 ≈ 25 分。注: v19 dataset は 202401-202506 の **18 月**で 24 月ではない）→ detached 起動（`nohup ... > log 2>&1 &`）し process 生存（`ps -p <pid>`/CPU 増加）で判断、log は flush 済みでも per-month 出力は月境界でしか出ない（「出力が無い」≠死亡）、重複起動禁止。**完了待ちは `until grep -q DONE log; do sleep N; done` を `run_in_background:true` で 1 本だけ仕掛けて通知を待つ — 空の waiter 出力ファイルを Read で繰り返し覗かない（turn 浪費）。** 1.26M 行の集計フェーズ（variant 別 groupby/codes_to_50/PBO）は path 再構築後さらに数分かかり中間 print 無し。(3) future leakage 厳禁（特徴 09:00→decision_time のみ・当日確定値不可・top decile は同日 cross-section 内・trailing σ/ADV は前日まで）、時系列 walk-forward（random split 禁止）、held-out は 1-shot で threshold/cell 選択に使わない。**B0/baseline 再現は dataset の `future_return` をそのまま使う**（v19 build の exit 参照は最終 bar の **L（安値）**で C ではない quirk。path 再計算で B0 を作り直さない）。(4) friction 壁 ¥22,906（手数料のみ・16bps）/ NOTIONAL ¥14,283,352（v17 long 平均）で net=gross−friction、gross/RT は全 RT 平均で定義 reconcile。impact は `Y·σ·√(Q/ADV)`（出所 `scripts/stress_v18_capacity_cost.py` central=HS10/20bps,Y1.0、median p=Q/ADV≈4% で RT≈112bps が binding wall）、net/gross 厳格分離。anchor smoke = 全 dataset の B0 gross/RT が proposals 記載の random-pick 帯（≈ −¥6,126）と整合（**月サブセットでは帯外に出るので必ず全窓で検証**）。(5) pre-registered 機械判定のみ返し採否は出さない（**PBO=0 でも全 cell net<0 なら degenerate＝robust ではない**と明記）。(6) **既存 harness 追記時は env-var ゲート（例 `EVLAB_MODE=exit_sweep`）で新経路を分け、env 未指定で前フェーズ経路を byte 一致保持（regression）**。新 cell の数値を信じる前に **内部 regression gate** を必ず通す: 前フェーズの baseline cell（例 exit=14:55 held net/RT・t_day・codes50・n）を **EXACT 再現**できることを SMOKE で確認し、乖離したら数値を出さず停止して報告。再現には canonical loader を **verbatim import**（`window_close_at`=C / `close_at_exit`=最終 bar の **L** quirk を自前で書き直さない）。**anchor が ~20% ずれたら、まず reconstruction の exit 規約（C@14:55 vs Low@14:55）を疑え（model/refit を疑う前に）**: dataset `gross_yen` ベースの anchor（例 -1f per-dt 10:00 ~1,894/~1,565）は **Low-exit**（`close_at_exit`）で作られた値なので、それを minute-path の C@14:55（`window_close_at`）で再構築すると C>Low で系統的に高く出て **偽 MISMATCH** になる（S6 で実発生＝false ~20% mismatch・overlap は健全だった）。anchor target がどの exit 規約で作られたか（dataset gross_yen=Low / baseline-C=C）を確認し同じ規約で照合する（baseline-C anchor=C-exit 2,162 と per-dt refit anchor=Low-exit 1,894 は意図的に別規約）。launch は **1 回のみ・foreground 完走・重複/再起動禁止**、分足 I/O は picks の (code,date) に絞る。template は `scripts/probe_v23_sequential_control.py`（path 再構築+oracle/rule exit/sizing+capacity cost の最新版）/ `scripts/probe_v18v22_separability.py` / `scripts/build_v19_ranker_dataset.py` / `scripts/eval_v19_ranker.py`。
---

# sacrificial-lamb-replay

Full operational workflow for running strategy replays in the `_sacrificial-lamb` project using the NautilusTrader-based `engine.strategy_replay` CLI in The-Trader-Was-Replaced (TTWR).

---

## Directory layout

```
D:\Documents\
├── _sacrificial-lamb\            ← strategies, data, scripts, Silver/, raw/
│   ├── strategies\         ← strategy .py files (e.g. gap_reversion_01.py)
│   ├── data\universe\      ← universe JSON files (v05_B_top100_*.json)
│   ├── scripts\            ← ingest_run.py, publish_run.py
│   ├── Silver\runs\        ← ingested summary.json + breakdown.json
│   └── raw\replay-runs\    ← Bronze (immutable raw output)
└── The-Trader-Was-Replaced\    ← engine repo (TTWR)
    └── artifacts\jquants-catalog\  ← Parquet catalog (required for replay)
```

Trade logs and env sidecars: `C:\tmp\<strategy_stem>_trades_<ts>.jsonl` and `C:\tmp\<strategy_stem>_env_<ts>.json` (e.g. `gap_reversion_01_*`, `rs_breakout_01_*`). `<ts>` is the unix timestamp captured at `on_start`, so it is slightly *later* than the `run_id` prefix — match by sort order, not exact value.

Run buffer: `%APPDATA%\flowsurface\run-buffer\<run_id>\` — contains `fills.jsonl`, `equity.jsonl`, `meta.json`, `summary.json`. (No strategy log file is persisted here — the `on_start`/`on_stop` log lines, including any `qty_zero_skips` counter, only go to replay stdout. Capture stdout if you need them.)

---

## 1. Switch SCENARIO window in strategy file

Each strategy file has a block pattern:

```python
# --- Run 1: Jan13-24 (active) ---
SCENARIO: Scenario = { ... "start": "2025-01-13", "end": "2025-01-24", ... }
UNIVERSE_JSON_PATH = "...jan1324.json"

# --- Run 2: Jan06-10 ---
# SCENARIO: Scenario = { ... }
# UNIVERSE_JSON_PATH = "...jan0610.json"
```

To switch: comment out the active block, uncomment the target block. Only one `SCENARIO` assignment can be active.

**Critical — the SCENARIO block, not an env var, controls instrument subscription.** `SCENARIO["instruments_ref"]` (schema_version 3) is what the CLI uses to subscribe instruments. `STRATEGY_PARAM_UNIVERSE_JSON_PATH` only overrides the strategy's *daily allowlist gate* — it does **not** change which instruments the engine streams. So to run a cross-check window with a different universe (e.g. `jan0610` vs `jan1324`), you **must edit the SCENARIO block**; setting the env var alone will subscribe the wrong instrument set and silently mismatch the allowlist. `--start`/`--end` override the SCENARIO dates but not `instruments_ref`.

**Critical — strategies that use SCENARIO["start"]/["end"] internally must have the SCENARIO block updated for each window.** Some strategies (e.g. `zar_v2_01.py`) call `SCENARIO["start"]`/`SCENARIO["end"]` directly at `on_start` to build internal state (e.g. RVol lookback table). Using `--start`/`--end` CLI overrides without updating the SCENARIO block causes 0 trades silently — the internal table only covers the SCENARIO dates, not the actual replay dates. **Always update the active SCENARIO block to match the target window before running.** Symptom: replay completes with correct `bars=N` but `trade_count: 0` despite adequate data.

---

## 2. Run replay

**Always run from the TTWR directory.** The catalog path is relative to TTWR.

```powershell
# Set strategy param overrides via env vars (STRATEGY_PARAM_<KEY>)
$env:STRATEGY_PARAM_UNIVERSE_JSON_PATH = "D:\Documents\_sacrificial-lamb\data\universe\v05_B_top100_jan1324.json"

cd D:\Documents\The-Trader-Was-Replaced

uv run python -m engine.strategy_replay run `
    --strategy "..\\_sacrificial-lamb\strategies\<strategy_stem>.py" `
    --catalog "artifacts\jquants-catalog" `
    --granularity Minute `
    --start 2025-01-13 `
    --end 2025-01-24
```

**`--start` / `--end` override SCENARIO dates.** The `--catalog` flag is required (no catalog → error). `--granularity Minute` matches the SCENARIO granularity; pass it explicitly when the task specifies it.

**Smoke first.** Before the real baseline runs, do a short 1–2 day smoke (e.g. `--start 2025-01-14 --end 2025-01-15`) to confirm "no env = defaults, no errors, trades emitted". Note 2025-01-13 is a JP holiday (Coming of Age Day) — pick a smoke window that has data.

On success, the last output is a JSON block:
```json
{
  "run_id": "1778803136-gap_reversion_01-1360_TSE",
  "run_dir": "C:\\Users\\...\\run-buffer\\...",
  "total_pnl": -510994.0,
  "max_drawdown": 834151.0,
  "trade_count": 115,
  "win_rate": 0.4,
  "fills_count": 131
}
```

### env var pattern for strategy params

All strategy params are overridable via `STRATEGY_PARAM_<KEY>` (uppercase).  
Example: `$env:STRATEGY_PARAM_BASKET_SIZE = "10"`, `$env:STRATEGY_PARAM_STOP_PCT = "0.02"`.  
The CLI also injects `universe_json_path` and a no-op `warmup_loader` automatically.

---

## 3. Ingest into Bronze / Silver

```powershell
cd D:\Documents\_sacrificial-lamb
uv run python scripts/ingest_run.py <run_id>
```

Verify the output contains **`Silver breakdown:`** line. If it doesn't appear, ingest failed.

Ingest also writes `wiki/runs/<strategy_stem>-<ts>.md` with auto-filled Metrics/References and **empty `## Notes` / `## Decision` sections** — these must be hand-written (they survive re-ingestion). `Instrument: unknown` in the auto Scenario block is normal for multi-instrument strategies; ignore it.

W&B publish (`scripts/publish_run.py`) requires `wandb login`. Without it, `publish_run.py` exits with code 3 and `ERROR: ... No API key configured`. In that case skip publish entirely and ingest **without** `--wandb-url` — ingest still fully succeeds (Bronze + Silver + wiki page). Note in the report that the W&B URL is pending `wandb login`.

---

## 4. Analyze trade log

Find the most recent trade log:
```powershell
[System.IO.Directory]::GetFiles("C:\tmp", "gap_reversion_01_trades_*.jsonl") | Sort-Object | Select-Object -Last 1
```

Key metrics to compute (run in TTWR with `uv run python -c "..."`):

```python
import json
from collections import Counter, defaultdict

records = [json.loads(l) for l in open('C:/tmp/gap_reversion_01_trades_<ts>.jsonl') if l.strip()]

# exit reason breakdown
exits = Counter(r['exit_reason'] for r in records)

# PnL
pnl_gross = sum(r['pnl_gross'] for r in records)
pnl_net   = sum(r['pnl_net'] for r in records)

# win rate (trade log perspective)
wins = sum(1 for r in records if r['pnl_gross'] > 0)

# single-instrument concentration
inst_pnl = defaultdict(float)
for r in records:
    inst_pnl[r['instrument']] += r['pnl_gross']
sum_abs = sum(abs(v) for v in inst_pnl.values())
top_inst = max(inst_pnl.items(), key=lambda x: abs(x[1]))
concentration = abs(top_inst[1]) / sum_abs
```

### Basket fill rate

From `fills.jsonl` in the run buffer (counts engine-level BUY fills):
```python
import json, datetime, os
fills = [json.loads(l) for l in open(
    os.path.join(os.environ['APPDATA'], 'flowsurface', 'run-buffer', '<run_id>', 'fills.jsonl')
) if l.strip()]
buys = [f for f in fills if f['side'] == 'BUY']
days = {}
for f in buys:
    dt = datetime.datetime.fromtimestamp(f['ts_event_ms']/1000, tz=datetime.timezone.utc) \
         + datetime.timedelta(hours=9)
    days[dt.strftime('%Y-%m-%d')] = days.get(dt.strftime('%Y-%m-%d'), 0) + 1
```

Or from the trade log (strategic entries = what the strategy decided to enter):
```
fill_rate = len(records) / (n_trading_days × basket_size)
```

The two differ because the engine may count partial fills or rollover fills separately.

**`basket_size` semantics matter.** If `basket_size` is a *daily basket decision* cap (one decision per day, e.g. gap_reversion), `fill_rate` is naturally ≤ 100%. If it is a *concurrent-holding* cap (slots free up intraday on stop/tp/stale and get re-filled by new entries the same day, e.g. rs_breakout), `fill_rate` routinely **exceeds 100%** — that is expected, not a bug. Report it as-is and note which semantics apply.

### trade_count (engine) vs trade log records

These rarely match. The engine `trade_count` / `fills_count` count fill *events* (BUY + SELL). The trade log only writes a record **on exit**, so positions still open at run end are never logged. Therefore `BUY fills ≥ trade log records`, and `BUY fills − trade log records = positions open at run end`. Always report both numbers; don't treat the gap as an error.

### breakdown.json avg_pnl is net-based

`Silver/runs/<run_id>/breakdown.json` → `by_exit_reason.*.avg_pnl` (and `avg_win`/`avg_loss`, `loss_distribution`) are computed from **`pnl_net`**, not `pnl_gross`. A gross computation off the trade log will not match it — the difference is fee per trade. `avg_bars_held` is `0.0` whenever the trade log has no `bars_held` field.

---

## 5. PnL discrepancy — engine vs trade log

The engine's `total_pnl` is based on **actual fill prices** (bar close at the time the exit is triggered).  
The trade log's `pnl_gross` uses:
- **stop exits**: `stop_price = entry_price × (1 − stop_pct)` (theoretical stop level)
- **time / force_close exits**: bar close (same as engine)

When the bar that triggers a stop has a close far below the stop level, the engine's fill is much worse than the theoretical stop price. This creates a systematic gap: trade log understates losses on stop exits. The gap widens as `stop_pct` gets tighter (a tight 0.8% stop is pierced by minute bars more often and harder than a loose 3% stop).

A **second component** of the gap: positions still open at run end are marked-to-market in the engine's `total_pnl`/equity but have no trade log record at all (see §4 — trade log only writes on exit). So `engine total_pnl − trade log pnl_gross` reflects both stop-fill slippage *and* unrealized PnL on unclosed positions.

**This is not a bug** — it reflects the reality that market orders fill at close, not at the exact stop price. In the gap reversion baseline (Run 1 Jan13-24) the discrepancy was ~−440K JPY; in the rs_breakout_01 baseline (stop_pct=0.008) it was ~−82K (main) / ~−26K (cross-check).

To eliminate the discrepancy: record `close` (bar close) as `exit_price` for stop exits in the trade log instead of the theoretical `stop_price`. This makes the trade log match the engine.

---

## 6. AccountBalanceNegative prevention

The engine batches all market orders from one `on_bar` call and fills them simultaneously. A sequential per-order balance check inside the strategy does **not** prevent overdraft — all orders are submitted before any fills arrive.

**Fix**: cap daily spend at 90% of estimated balance before submitting the basket:

```python
budget = min(self._initial_cash, self._balance_estimate * 0.90)
alloc = budget / self.basket_size
```

The `_balance_estimate` is updated:
- On entry: `self._balance_estimate -= ref_price * qty`
- On exit: `self._balance_estimate += exit_price * qty`

The 10% buffer absorbs fill-price drift (actual fills at bar close vs ref_price = prev_day_close).

---

## 7. Trade log buffering

Use **line-buffered** (`buffering=1`) for the trade log file, not block-buffered. With block buffering (64KB default), records sit in the buffer and are lost if `on_stop` is not called by the engine:

```python
self._trades_file = open(self._trades_path, "w", encoding="utf-8", buffering=1)
```

### Don't put summary counters in `on_stop`

The engine **does not reliably call `on_stop`** at the end of a replay (observed empirically in rs_breakout_01 — `on_start` log lines and the trade log records all appear, but the `on_stop` log line and any `on_stop`-only writes silently disappear). So any summary metric you only emit at `on_stop` (a final `qty_zero_skips` count, end-of-run statistics, a closing `_meta` row) **will be missing for many runs**. The env sidecar avoids this because it's written at `on_start`, but anything that depends on counters accumulated during the run cannot live there.

Pattern to use instead — write the metric **incrementally**, every time the counter changes, and parsers read the **last** matching line as the final value:

```python
# in the strategy
def _write_meta_line(self) -> None:
    if self._trades_file is None:
        return
    self._trades_file.write(json.dumps({
        "_meta": True,
        "zero_lot_skips": self._qty_zero_skips,   # cumulative
        # plus any other in-flight counters
    }, ensure_ascii=False) + "\n")

# call at every event that updates the counter (e.g. on each 0-lot skip),
# AND best-effort once more in on_stop for runs where it does fire.
```

Update `_compute_trade_breakdown` and any analyze script to **skip `_meta` lines** at the top of the loop (`if rec.get("_meta"): continue`), otherwise they pollute the breakdown as `unknown` exit_reason rows. To extract the final counter, scan with `zls = rec.get("zero_lot_skips", zls)` so the loop's last `_meta` line wins.

### When adding a new lever, also add per-record observation fields

When a Phase Rn task adds a new entry/exit lever, also extend the per-trade record with **observation fields** that capture the entry-bar state the lever decides on — even if the lever itself doesn't read them. The next phase will need them to diagnose, and adding logging retroactively means re-running every prior cell.

In rs_breakout_01 this was the Phase R1 → R2 mistake-then-fix: R1's `entry_confirm_bars` shipped without entry-bar feature logging, so R2's diagnostic was blind to which trades had abnormal `vol_ratio` / `rs_rank` and had to invent the logging. R2 then shipped 3 fields (`entry_jst_hm`, `entry_vol_mult_ratio`, `entry_rs_rank`) alongside its own gate, and that data drove the Phase R3 hypothesis (vol-cap) without a separate logging-only run.

Heuristic: for any new gate that *decides* on `(time, vol, rank, rs, price-relative)` features, write the actual feature value the gate saw onto the trade record at entry time. Cost is ~3 lines of code per field; payoff is one phase saved.

---

## 8. §6 report format

Return this report to 司令塔. By default do **not** write log notes to `bs-wiki` — that's 司令塔's job (司令塔 writes log notes via `mcp__basic-memory__write_note(project='bs-wiki', folder='log', ...)`). **Exception:** when the 実装指示書 (task file) explicitly instructs writing a log note and lists it as a 完了条件 (as the v07 rs_breakout baseline task did in its §5.4 / §6), the task file overrides this default — write the note via `write_note(project='bs-wiki', folder='log', ...)`. The task file is the authoritative instruction for that piece of work; this skill's default is generic guidance.

Report format:

```
### Run N — <window> (<universe label>)

| Field | Value |
|---|---|
| window | <start> – <end> |
| run_id | `<run_id>` |
| gross total_pnl (engine) | <value> JPY |
| net pnl_net (trade log) | <value> JPY |
| trade_count (engine) | <value> (fills_count=<value>; BUY=<x>, SELL=<y>) |
| trade log records | <value> |
| basket fill rate | <N> / (<days> × <basket_size>) = <pct>% |
| max_drawdown (engine) | <value> JPY |
| gross single-inst. concentration | <instrument>: <pnl> / <sum_abs> = <pct>% |
| Silver breakdown | ✓ confirmed |

Exit reason breakdown (trade log):
| reason | count | share | avg_pnl | win_rate |
stop | ... | ... | ... | ...
time | ... | ... | ... | ...

Basket fills per day:
<date>: <N>/basket_size
...

0-lot skips: <count if captured, else "not captured — shortfall = <N> slots (upper bound)">
```

---

## 9. Common errors

| Error | Cause | Fix |
|---|---|---|
| `AccountBalanceNegative(balance=<N>, currency=JPY)` | Daily basket spend exceeds engine balance (batch fill) | Use 90% budget cap (§6 above) |
| `either --catalog or --bars-json is required` | Missing `--catalog` flag | Add `--catalog artifacts\jquants-catalog` |
| `error: Failed to spawn: engine` | Wrong invocation (not `python -m engine.strategy_replay`) | Use `uv run python -m engine.strategy_replay run` |
| Trade log 0 bytes | Block buffering + `on_stop` not called | Change to `buffering=1` |
| `ModuleNotFoundError: No module named 'engine_pb2'` | Wrong module (`engine` instead of `engine.strategy_replay`) | Use `python -m engine.strategy_replay run` |
| W&B `No API key configured` | `wandb login` not run | Ingest without `--wandb-url` (optional field) |
| `trade_count: 0` despite bars streaming correctly | Strategy uses `SCENARIO["start"]/["end"]` for internal state (RVol etc.) but SCENARIO block not updated for target window | Update the active SCENARIO block to match the target window (§1) |
| `SCENARIO has unknown keys: ['allow_short']` | Latest TTWR removed `allow_short` from `_V3_OPTIONAL` | Remove `allow_short` from SCENARIO dict — it is unused by engine and strategy code |
| `failed to load bars from catalog: 'instruments'` | Loading from `.py` — latest TTWR does NOT resolve `instruments_ref` in the `.py` load path | Create sidecar JSON `strategies/<name>.json` (see §11) |
| `bars=N` but `trade_count: 0` when sidecar dates differ from `.py` SCENARIO | Strategy reads `SCENARIO["start"]/["end"]` from `.py` at runtime for RVol table; sidecar controls engine dates only | Keep `.py` active SCENARIO dates in sync with sidecar JSON dates |
| Offline probe: filter on an artifact column silently returns 0 rows (e.g. `split=="held_out"` but file has `heldout`); a derived metric looks absurd (concentration share >1.0) | Task doc *assumed* schema (column names / enum values / units) that does not match the real artifact | **Before coding against a reused artifact, verify actual values** (`df["split"].value_counts()`, `df.columns`, sample rows). Don't trust the task's stated schema verbatim. For ratio metrics, define them so they stay legible when the denominator is small (e.g. share of the positive-contribution pool, not share of net total). |
| `0 fills` on a SELL-to-open (信用新規売) strategy | sidecar SCENARIO has no `account_type` (CASH default rejects short-open) | set `"account_type": "MARGIN"` in the `.json` |
| Phantom "order DENIED / per-order notional cap" while validating a strategy | you hand-rolled a `BacktestEngine` harness whose config differs from production (e.g. a RiskEngine per-order notional default ~1M JPY) | validate ONLY via `uv run python -m engine.strategy_replay run --strategy ... --catalog ...` — the canonical runner has no such cap; never judge a strategy from a bespoke `add_venue/add_instrument/add_strategy` script |

---

## 11. サイドカー JSON (新 TTWR 互換)

最新 TTWR は `.py` からロードする場合 `instruments_ref` を解決しない。サイドカー JSON 経由でのみ解決する。

**サイドカー JSON の作成:**
```json
// strategies/zar_v4_01.json
{
  "scenario": {
    "schema_version": 3,
    "instruments_ref": "../data/universe/v17_liquid300_universe.json#/instruments",
    "start": "2025-01-13",
    "end": "2025-01-24",
    "granularity": "Minute",
    "initial_cash": 50000000
  }
}
```

**重要な制約:**
- `allow_short` は SCENARIO に含めない（最新 TTWR の validator が拒否する）
- サイドカー JSON と `.py` active SCENARIO の `start`/`end` は**必ず一致**させる
  - `.py` の `SCENARIO["start"]/["end"]` は戦略コード（RVol テーブル等）がランタイムに読む
  - 不一致だと `bars` はストリームされるが `trade_count: 0` になる

**ウィンドウ切替時の手順:**
1. `.py` の active SCENARIO ブロックを目的の window に切替（`_set_active_scenario.py` または手動）
2. サイドカー JSON の `start`/`end` を同じ日付に更新
3. replay 実行（`--start`/`--end` CLI override は RVol テーブルに影響しないので `.py` を変えること）

**サイドカー編集の落とし穴（2026-06-01 実証, down-thrust-scalp P1.5）:**
- **`--start`/`--end` はサイドカー日付を override しない戦略がある**。サイドカーだけで日付を持つ
  戦略（active SCENARIO を `.py` に持たず `.json` の `start`/`end` のみ）では、CLI の
  `--start 2022-10-25 --end 2022-10-25` を付けても**サイドカーの日付（例 10-26）で走り続ける**
  （別日のつもりが同一日を実行）。**別日 replay はサイドカー JSON の `start`/`end` を実際に
  書き換える**こと。CLI override を信用しない。
- **PowerShell `Set-Content -Encoding utf8` は UTF-8 BOM を書く** → TTWR の
  `scenario.py load_scenario`（`json.loads`）が `JSONDecodeError: Unexpected UTF-8 BOM` で即落ち。
  さらに「編集→finally で復元」する PS スクリプトは復元も BOM 付きでサイドカーを破損させる。
  **サイドカー書き換えは PowerShell でなく Python `json.dump(doc, open(p,'w',encoding='utf-8'))`
  か Claude の Write/Edit tool（BOM なし）で行う**。BOM チェック: `open(p,'rb').read()[:3]==b'\xef\xbb\xbf'`。
- 隣接日 sanity 等の単発別日 replay は、Write/Edit で `.json` 日付を書換え → **foreground で
  1 本ずつ**回し、終わったら元日付に戻すのが最も確実（逐次なので run_id 衝突も防げる）。

---

## 12. Catalog 拡張 (新しい日付範囲を追加する) (新しい日付範囲を追加する)

既存 catalog は `artifacts/jquants-catalog/data/bar/<instrument>-1-MINUTE-LAST-EXTERNAL/` 配下に parquet を持つ。`ParquetDataCatalog.write_data()` は新しい日付範囲を**別 parquet ファイルとして追記する**（既存 parquet は上書きしない）。

### 既存スクリプト

`The-Trader-Was-Replaced/scripts/build_catalog_batch.py` が基本パターン。`_needs_build()` で既存 parquet をスキップするが、新しい日付範囲は必ず別 parquet として書かれるので `--force` 不要。

### 拡張スクリプトのパターン (liquid_30 向け)

```python
# extend_catalog_2402_2410.py (example in scripts/)
BASE_DIR = Path(r"S:\j-quants")   # j-quants CSV.gz source
CATALOG  = Path(r"S:\artifacts\jquants-catalog")

# 1. load liquid_30 from v16_universe_profile.csv (tier == "liquid_30")
# 2. read monthly CSV.gz with gzip, filter by Code (instrument symbol + "0")
# 3. write via ParquetDataCatalog(str(CATALOG)).write_data(bars)
```

### 注意事項

- `jquants_loader.py` の `JQuantsLoader(base_dir)` も同じ S: ドライブを使う
- `build_catalog_batch.py` の `_needs_build()` は「そのディレクトリに parquet が 1 つでもある」場合にスキップ → **既存銘柄に新規期間を追加する場合は `--force` か専用スクリプト必須**
- 拡張後 smoke test: 任意 1 銘柄 1-2 日の replay で bars count が想定通り (～299 bars/日/銘柄) か確認
- parquet ファイル名は `<start_ts>_<end_ts>.parquet` 形式で自動命名される。2 parquet が共存していれば catalog が自動で時系列をマージして返す

---

## 13. Sweep — パラメータスイープと横断比較

§1–3 の単発 replay→ingest を**複数セルに展開**し、最高収益のパラメータセットを特定する手続き。
2 モードある:

| モード | 使う場面 |
|--------|---------|
| **A. 既存結果の分析** | すでに `Silver/runs/` に複数ランがあり、ベストを探すだけ |
| **B. パラメータスイープ + 分析** | 新しいパラメータ範囲で replay を回し、最高設定を決定する |

> **baseline が net 赤字なら、いきなり Mode B に進まない。** sweep は「どの値が最良か」の道具で「何が損失の
> 主因か」の道具ではない。先に §4 の trade log 解析で exit_reason 別 hold-time / `loss_by_bars_held` を見て
> レバーを 1 つに絞り、その軸だけを sweep する（診断カタログは分析官 agent `.claude/agents/strategy-optimizer.md`）。
> 診断がレバーを排除したら、その軸の sweep はスコープ外。

### Mode A: 既存 Silver 結果の横断比較

```bash
uv run python .claude/skills/sacrificial-lamb-replay/scripts/compare_runs.py \
    --silver Silver/runs \
    --top 10
```

出力は `total_pnl` 降順の `run_id / total_pnl / max_dd / win_rate / trades` 表。読み方:
- `total_pnl` 最大がベスト候補。ただし `max_drawdown` が極端なら採用しない。
- `trade_count < 5` は統計的に薄い → 参考程度。
- 未 ingest のランは §3 で ingest してから比較。ingest 済みなら `mcp__basic-memory__read_note(project='bs-wiki', identifier='runs/<strategy>-<ts>')` で Notes/Decision を確認（または `wiki/runs/` を直接 Read）。

### Mode B: スイープ → 分析 → 登録

**B-1. スイープ定義**（Python dict。`params` の直積がセル数。**最大 25 セル**目安）:
```python
SWEEP = {
    "strategy_file": "../🐃_sacrificial-lamb/strategies/mean_reversion_01.py",
    "params": {"window": [5, 10, 20], "k": [1.0, 1.5, 2.0], "holding_minutes": [30]},
}  # 3×3×1 = 9 cells
```
パラメータ設計の理論（粗グリッド→精密の 2 段階、採用閾値、過学習注意）→ [`references/param-sweep-guide.md`](references/param-sweep-guide.md)

**B-2. スイープ実行** — 専用 `scripts/run_<strategy>_sweep.ps1` があればそれを使う（replay→ingest まで自動）。
無ければ汎用 `sweep_runner.py` を TTWR ルートから:
```bash
cd ../The-Trader-Was-Replaced
uv run python "../_sacrificial-lamb/.claude/skills/sacrificial-lamb-replay/scripts/sweep_runner.py" \
    --strategy "../_sacrificial-lamb/strategies/<name>.py" \
    --params "window=5,10,20" --params "k=1.0,1.5,2.0" --params "holding_minutes=30" \
    --output-dir "%APPDATA%\flowsurface\run-buffer"
```
各セルを順番に replay し、完了 run_id を stdout に 1 行ずつ出す。失敗セルは stderr に WARN を出して続行。
`.ps1` を `run_in_background` で呼ぶと `Set-Location` が効かず SCENARIO 未認識で即失敗することがある → `.ps1` は前景実行。

**B-3. Silver 集計と比較** — 全 run_id が run-buffer に揃ったら sacrificial-lamb に戻り、各 run_id を §3 の手順で
ingest（この時点は W&B 無しで可）→ `compare_runs.py --strategy-filter <name>` で横断比較。

---

## 14. W&B でパラメータ比較

**ablation 全件を `--params` 付きで publish する**（ベストランだけ上げると比較パネルの点が揃わない）:
```bash
uv run python scripts/publish_run.py <run_id> --tags ablation \
    --params exit_th=0.05 --params quick_fail_bars=0 --params top_k_per_minute=0
# → 全 cell 分繰り返す。各 run の stdout に W&B URL が出る
```
`--params` のキー名はパラレル座標の軸名になるので strategy 内の変数名と統一する。
（`wandb login` 未実行なら publish はスキップ可 — §3 参照。ingest 自体は W&B URL 無しで完走する。）

**パラレル座標パネル**（ワークスペースに 1 回）: `+ Add panels` → `Parallel coordinates` → スイープした各
パラメータキー + `total_pnl`（最後の軸）→ Apply → Save。読み方:
- 黄/オレンジ線（pnl 最高）が通る値がベスト候補。
- 全線が `total_pnl` 低側に集まる → 仮説そのものの問題（パラメータでなく設計を見直す）。
- 線が交差して一貫性なし → ノイズ過多、期間を変えて再確認。

**`exit_reason_breakdown` テーブル**（Artifacts タブ）: `trail` が大多数なら trailing が主出口 / `trail` の
win_rate <20% なら `exit_th` を下げる余地 / `hard_stop` のみ大負値なら stop 幅見直し。
**`loss_by_bars_held` テーブル**: `1-2 bars` の count 多 → quick exit で損失 / `p90` が `p50` の 3 倍以上 → 裾が重い・大損 outlier / 長バケツ(10+)に損失偏り → holding 長すぎ。

---

## 15. 収益比較の判断基準

### mean_reversion 系（参考値）
| 指標 | 採用の目安 |
|------|-----------|
| `total_pnl` | > baseline ベスト（現状: 約 6900 JPY）|
| `max_drawdown` | ≤ baseline の 1.5 倍（現状 -2100 → 上限 -3150）|
| `trade_count` | ≥ 5 |
| `win_rate` | 参考値（高 PnL かつ low win_rate の順張りも許容）|

### order_flow 系（in-sample AND 外挿の両方をクリア）
| 指標 | 基準 |
|------|------|
| `win/loss_ratio` | ≥ 1.8 |
| `max_drawdown` | ≤ 33,332 JPY（initial_cash 100 万の 3.3%）|
| `expectancy(gross)` | ≥ v02 比 +50%（≥ -44 JPY/trade）|
| 外挿検証 | in-sample の win/loss 比が外挿期間でも維持される |

**外挿検証**: in-sample でスイープ→ベスト候補→out-of-sample で同パラメータ再走→同等なら採用候補・大幅劣化なら過適合として棄却。さらに長期でも確認できればより確実。

### 共通ルール
- **採用（AND）**: `total_pnl > 0` AND `total_pnl > baseline_best` AND `max_drawdown` 許容範囲内。
- **保留**: `total_pnl > 0` だが baseline 未達 → 銘柄/期間を変えて再試験。
- **却下**: `total_pnl ≤ 0` → この instrument/period での仮説は棄却（アイデア全体の否定ではない。条件付き有望として wiki に記録）。

> 採否の最終判定（Adopt/Hold/Reject）・フェーズ設計は司令塔 (`.claude/agents/strategy-commander.md`) の仕事。
> このスキルは数値基準と材料を提供する。

---

## 16. Sweep で踏みやすい罠とコツ

長時間 ablation の前に、必ず 1 セルの smoke run で「パラメータが実際に効く」「run_id を回収できる」を確認する。
2 時間走った後に全部デフォルト条件だったと気づくのが最も高い失敗。

1. **パラメータ注入はログで検証**: env を set しただけでは不十分。1 セル目の前に strategy の `on_start` ログへ
   sweep 対象パラメータを必ず出す（例: `OrderFlow04 started: exit_th=0.05 quick_fail_bars=3 ...`）。
   default 値のままなら sweep を止める。実装・constructor 引数・ログ出力の 3 点が揃ってから replay。
   bool パラメータは `--strategy-param key=0` だと `bool("0")==True` で ON のまま → 必ず `STRATEGY_PARAM_<KEY>`
   env で渡す（§2 の env パターン）。env は run 間で必ずクリア（残留すると「汚染された baseline」になる）。
2. **run_id 捕捉は before/after 差分で**: PowerShell の `Get-Date -UFormat %s` は JST と混ざり 9h ずれる。実行
   前後の run-buffer ディレクトリ差分で新規 run を拾うのが堅い。途中再開時は `label→run_id` 表を別ファイルに保存。
   **run_id は unix 秒ベースなので、複数 replay を同一秒に並列起動すると run_id が衝突し同じ run-buffer を
   上書きし合う**（total_pnl は各プロセスの stdout で正だが、equity.jsonl / max_drawdown が clobber されゴミ化）。
   並列で回すなら起動を秒単位でずらすか、別日 sanity 等は逐次（foreground 1 本ずつ）で回す（2026-06-01 実証）。
3. **8 本まとめる前に 1 本通す**: smoke で「パラメータが `on_start` に出る / run_id 自動回収 / `meta.json`・
   `fills.jsonl`・`equity.jsonl` 生成 / `ingest_run.py` が Silver summary を作る / breakdown が作れる」を確認。失敗したら replay 継続でなく harness を直す。
4. **集計粒度を混ぜない**: `summary.trade_count`（fill 単位）と `breakdown.trade_count`（round-trip 単位）は乖離
   する（例 1690 vs 1155 → 同じ total_pnl で 46% 違う expectancy）。**expectancy は常に `breakdown.trade_count`(RT)
   を分母**にする。exit_reason 別の win/loss を語るとき summary 側 `win_rate` を混ぜない。
5. **gross の出所も明記**: engine `summary.total_pnl` と trade log `pnl_gross` はどちらも gross だが乖離する
   （stop exit を engine は実 fill 価格・trade log は理論 stop_price で記録 + engine は未決済建玉を時価評価。
   §5 参照）。run 比較は同じ出所どうしで揃える。net（trade log `pnl_net`）を主、gross を従。
6. **exit 系 ablation の読み方**: `exit_th`↑は trailing 後段の `pressure_exit` を早める効果。`quick_fail` は
   価格・ATR・pressure の 3 条件で「entry 仮説が失効」を表す形にする。`top_k_per_minute` は同一 minute 内で弱い
   シグナルを落とすので、機会の少ない局面でも完全に止まりにくい（entry_score 下限より扱いやすい）。
7. **レポートは結論より先にハーネス健全性**: strategy SHA / period・universe・initial_cash / 各 cell の effective
   params / run_id 一覧 / run_id 回収方法 / ingest・breakdown 成否 / 勝率の集計粒度 を先に書く。その後で metrics
   表と採否。ハーネスが怪しい結果は見た目が良くても採用しない。

### 16-bis. offline 解析（replay 不要・既存 artifact 再利用）で踏みやすい罠

probe / stress test など replay を回さず `data/*.parquet` や `S:\j-quants\*.csv.gz` を直接読む解析タスク特有の罠:

1. **LFS pointer 罠**: `data/*.parquet` が **数百バイト（例 134B）**なら実体ではなく Git LFS pointer
   （`version https://git-lfs.github.com/spec/v1` で始まる）。pyarrow は `Parquet magic bytes not found` で落ちる。
   着手時に `ls -la` でサイズ確認 → pointer なら `git lfs pull --include="data/<file>.parquet"` で実体取得してから読む。
2. **code 形式 drift（j-quants daily）**: parquet `code` は 5桁 string（`'20010'`）だが daily `Code` には
   新 TSE の **英数字コード（`'132A0'` 等）が混在** → `astype("int64")` が `invalid literal for int()` で落ちる。
   `pd.read_csv(..., dtype={"Code": str}, low_memory=False)` で読み `str.strip().str.zfill(5)` で照合する（int cast 禁止）。
   着手時に両者の overlap 件数を実確認（mid_small 1478 codes は daily と完全 overlap が期待値）。
3. **look-ahead 厳禁**: trailing 統計（ADV/σ）は trade_date を**含めない** → `rolling(N).mean()` を `groupby(code).shift(1)`
   で当日除外。time-varying な universe split（mid500/small_rest 等）も各日の trailing 値でランクする。
4. **fees-only baseline = 既存 probe の net と一致を smoke で担保**: コスト env を全クリア（=0）した経路が
   先行 probe report の net 値と**完全一致**するかを最初に確認。一致しなければ pick 再現がズレている → 先に直す。
   新コストは env-var A/B（defaults off）で追加し、env 未指定で baseline が保たれることを確認。
5. **daily bars の `AdjFactor` は per-day ex-date 比であって cumulative ではない（multi-day で致命）**:
   `equities_bars_daily_*.csv.gz` の `AdjFactor` は **ex-date 当日の分割比**（2:1 split で当日 0.5、それ以外の日は 1.0）。
   `raw × AdjFactor`（当日掛け）は**誤り**で偽 gap を生む（v24 probe で 2:1 split が調整後 −75% に悪化した実例）。
   連続調整は **backward-cumulative = 各日 × その日より「後」の全 AdjFactor の累積積**: `groupby(code)["AdjFactor"]`
   を逆順にして `shift(1,fill=1).cumprod()` を逆順に戻し、O/H/L/C に掛ける。`Va`（円建て売買代金=price×volume）は
   split 不変なので無調整で ADV proxy に使える。**adj smoke 必須**: 既知 split 銘柄（例 22670, 2023-09-28 2:1）の
   調整後 close gap が小（〜−1.4%）に収束し、生 gap（〜−50%）が消えることを print で確認。
   （注: v23 系の `load_trailing_daily` は intraday/単日中心で `raw × AdjFactor` のままだが、multi-day を跨ぐ probe では必ず backward-cum を使う。）
6. **固定 universe の survivorship + passive-hold net 正の long-beta 偽陽性（v24 −1b 実証）**: probe が
   **固定 liquid300（事前選抜リスト全期間使用）** で「passive buy&hold net 正」を出しても、それは real edge とは限らない。
   非交渉の 3 殺し: **(a) point-in-time universe**（各月初に full daily 全上場を trailing-60d median Va で再ランク
   →top-N、shift で look-ahead なし）で再走 → 固定版の net が崩壊すれば survivorship 偽陽性（v24 −1b: 固定 BH_H20 dev
   ¥205,912/RT → point-in-time ¥16,022/RT で −92%）。**(b) market-excess**（各 trade の return から同一保有期間の
   1306.TSE = TOPIX 代理の return を AdjFactor backward-cum で控除）→ excess net が消えれば long-beta（同 −1b: β 控除後
   ほぼ全 cell 負）。**(c) bull/bear split**（保有期間の市場 return 符号で分割）→ bear で大きく負なら beta-timing
   （同 −1b: bull +¥360K〜600K/RT・bear −¥350K〜−¥760K/RT）。**anchor smoke 必須**: 固定 universe で先行 probe の
   net（−1a の ¥205,912/RT 等）を rel diff ±5% 内で再現＝pick 忠実性 gate。**survivorship audit**: full daily の code 別
   最終 Date 分布で途中消滅（上場廃止/併合）件数を数え、data 自体が survivor-only か（>50 件あれば delisted を含む＝健全）を明記。
7. **「launch は 1 回・foreground 完走・detach 禁止」と harness の auto-background は両立する**（v27 S5 実証）:
   長時間 script を **foreground（blocking）で起動**しても、Claude Code harness は出力が一定量を超えると自動で
   background task 化して `task-id` を返すことがある（これは指示書が禁じる「自分から detach」ではなく、起動は 1 回のまま）。
   このとき python.exe が **2 つ**見えるが、`Get-CimInstance Win32_Process` で **ParentProcessId を必ず確認** — 片方
   （親）が launcher、もう片方（子, ParentProcessId=親PID）が worker なら**重複起動ではない**（同 CreationDate）。
   絶対にやってはいけない: パニックして kill / 再起動（過去事故）。正しい対応 = **再起動しない・polling ループで babysit しない**
   （token 浪費 + stall 誤認の再起動を誘発）。生存は process の有無で判断し、**完了通知（task-notification status=completed）を待つ**。
   解析 script は最後に csv+report を一括で書く → 出力が出ていない＝死んだ ではない。
8. **canonical engine の約定/手数料モデルと catalog bar の生バイト（offline sim が忠実に合わせる対象, practical P-1 実証）**:
   offline rule backtest を canonical TTWR replay に anchor するなら、engine 側の真の fill モデルを先に確認する。
   - **TTWR `BacktestEngine` は FillModel も commission も設定していない**（`engine_runner.py` / `nautilus_backtest_runner.py`
     の `add_venue` に FillModel 無し、`make_equity_instrument` に fee 無し）→ market order は **bar close で約定・slippage 0・fee 0**。
     friction-off の offline net は engine と一致すべき。slippage/手数料/spread は offline 側で friction layer として上乗せする
     （指示書の「サイズ依存 slippage」は #1 grill-me の経験則であって engine の挙動ではない）。
   - **catalog bar parquet は `open/high/low/close/volume = fixed_size_binary[8]`**（little-endian int64）。実価格 = `struct.unpack('<q',raw)[0]/1e9`
     （nautilus standard-precision RAW_SCALE=1e9）。**`price_precision` は per-instrument で `1` のこともある**（task が「precision 8」と書いていても
     列の値 precision と raw scale は別物）。`ts_event` は UTC ns、bar label は **bar-close 時刻**（minute :59.999...）。JST=+9h。
     pyarrow 直読の値は canonical loader (`load_bars`) の `float(bar.close)` と一致することを必ず 1 本突合してから全件回す。
   - **engine の per-order fill latency は非対称で offline では完全再現できない**（anchor smoke 9107 2022-10-26: signal@10:55 → engine entry fill@10:57
     ＝2 bar 遅延 + sub-tick slippage、cover は ~0 遅延）。offline は causal **next-bar-open**（fill_lag=1）が defensible 既定だが、
     これでも engine 比で **net が +10〜20% optimistic** になりうる。**この向き（optimistic）なら FAIL 判定は conservative-safe**。
     anchor が ±数% に収まらなくても、gate 結論が fill 誤差に robust（realistic friction が gross edge の数倍）なら数値を明記して進めてよい。
   - **NULL-control の random は rule が発火した (code,day) に matched-firing で限定**（同じ発火回数・同じ stock-day）。
     hold-to-close が大きく正なら universe/期間が方向ドリフト持ち（long/short-beta proxy）→ rule がそれに負けるなら churn/負選択。
   - **verbatim 再実装の OOS-set identity lock は「発火検出器」でなく「元 script の leg-EMISSION フィルタ」に一致させる（gh #12 実証）**:
     既存 rule を新 script に verbatim 再実装して `(code,day,entry_jst_hm)` の exact set equality で identity lock する時、生の発火集合は
     元 script が JSONL に書いた leg 集合の **superset** になりやすい（#12: missing=0 / extra=1011）。原因 = #9 `simulate_stock_day` は
     cover が EOD 前に完了（tp / cover_time_stop）した時だけ SHORT leg を書き、引け間際の発火は建玉が open のまま **leg を一切記録しない**。
     対策 = detected set を「元 script (`simulate_stock_day` 等) を read-only import → 実行 → 実際に emit された leg の `entry_jst_hm`」で定義する
     → diff=0。新 lever 側（exit grid の eod cell は必ず exit する等）は生の発火検出器を直接使ってよい。**identity lock の比較対象は anchor JSONL を
     生成した経路そのもの**であって、トリガー述語だけではない。
   - **catalog parquet は corrupt/壊れていることがある → offline loader は fail-open（1 ファイル死で全 run を止めない, practical P-1 #9 実証）**:
     full OOS launch が universe top-N に入った **4385.TSE の corrupt parquet**（`Parquet magic bytes not found in footer`）で
     500 銘柄 run ごと crash した実例。対策 2 段（`_load_day_bars` / `_va_median_for_instrument` 等 全 parquet-loop に必須）:
     (1) **filename-day-range window-skip**: catalog 名は `<START_ISO>_<END_ISO>.parquet`（各 ISO 先頭 10 字 = `YYYY-MM-DD`）。
     要求窓 [start,end] と重ならない file は **読まずに skip**（速い & out-of-window corrupt を自然回避）。
     parse 不能名は conservative に読む。**UTC→JST(+9h) ずれで tail bar が翌 JST 日に転がるので file の end-day は +1d pad**
     （false-keep は無害・false-skip は data 欠落で禁）。(2) **read を try/except**（`pyarrow.lib.ArrowInvalid` / `OSError` +
     防御 `Exception`）し corrupt は `WARN corrupt/unreadable parquet skipped: <path>` を stderr に出して skip。
     **回帰アンカー必須**: corrupt-skip / window-skip は in-window の readable な結果を 1 byte も変えない → SMOKE（既存 30-code・2022-10）の
     trades JSONL が修正前後で **byte 一致（sha256・net 同値）**することを必ず確認してから commit（変われば loader ロジックを壊している）。
     なお該当ファイルは後で再 ingest され読めるようになることもある（transient/repaired）— これは fail-open 防御なので catalog が直っても残す。
9. **per-stock 逐次設計は RSS をフラットに bound する（OOM 回避の正攻法, practical P-1 実証）**: 1 instrument-dir ずつ
   parquet を読み→ per stock-day で sim→ JSONL に逐次 write すると、peak working set は **universe サイズに依らず一定**
   （30 銘柄 152MB / 100 銘柄 155MB ＝ +0.04MB/code、time のみ ~0.27s/code で線形）。full(300-500) は RAM ~155MB・time 線形投影。
   **peak RSS の実測は in-process が正**: `uv run python -m ...` を subprocess で起動して親 PID を sample すると launcher を見て
   過小報告する（22MB と出た実例）。`importlib` で backtest を **同一プロセスに import→main() 実行**し、`GetProcessMemoryInfo(OpenProcess(own_pid))`
   の `PeakWorkingSetSize` を読むと真値（155MB）が出る。psutil 不在環境では ctypes で psapi.dll を叩く。
   **ctypes 罠（2026-06-04 conviction-pyramid 0g 実証）**: 新しい Windows では `windll.psapi.GetProcessMemoryInfo`
   が解決できず（または呼べても）戻り値 0＝失敗で **-1MB と出る**。正解は `windll.kernel32.`**`K32GetProcessMemoryInfo`**
   （modern forwarder）を `argtypes=[HANDLE, POINTER(PROCESS_MEMORY_COUNTERS), DWORD]` / `restype=BOOL` を明示して
   呼ぶこと。fallback で `psapi.GetProcessMemoryInfo` → `kernel32.GetProcessMemoryInfo` の順に getattr で試すと堅い。
   なお **per-month streaming（大 universe の minute CSV を 1 月ずつ読む）でも peak は #月に依らずフラット**
   （0g: 1 月 ~1.24GB = 全 ~4000 codes の minute 8.45M 行読み + filter copy が支配・month 累積ではない）。
   **「smoke peak × 月数」の linear 投影は streaming では過大**（0g で 1.24GB×18≈22GB と出るが実際は ~1.24GB）。
   投影は flat-bound（≒1 月 peak）で報告し、naive linear は ceiling 注記に留める。
10. **offline lab script は pyarrow を import するが repo 既定 uv env に pyarrow は無い（weak_basket / practical_* 系 実証 2026-06-01）**:
    `scripts/weak_basket_ladder.py` / `practical_rule_backtest.py` は `import pyarrow.parquet` する。だが
    `_sacrificial-lamb` の `pyproject.toml` は `wandb` しか dependency に持たず、`uv run python scripts/...` は
    `ModuleNotFoundError: No module named 'pyarrow'` で即死する（TTWR の uv env にも `.venv` にも pyarrow 不在）。
    **正しい起動 = `uv run --with pyarrow python scripts/<x>.py`**（on-the-fly で pyarrow を足す・~200ms）。素の
    `uv run python` で「pyarrow が無い」と出ても data wall ではない・catalog は健在 → invocation を直すだけ。
    着手時の loader smoke は必ず `--with pyarrow` で叩く。**pandas も同様に既定 env に無い**（`pyproject.toml`
    は `wandb` のみ）→ CSV.gz population を `pd.read_csv` で舐める builder（例
    `setup_attention_filter/build_hindsight_labels.py`、2024 minute 全月 scan）は `uv run --with pandas python ...`、
    catalog parquet も読むなら **両方 stack して `uv run --with pandas --with pyarrow python ...`**（2026-06-03 実証）。
11. **「foreground 1 回起動」と harness の auto-background は両立する（§16-7 の再確認・reversal P-1 実証）**:
    400-code 級の重い解析を **PowerShell で foreground（blocking）起動**しても、harness は出力量で自動 background task
    化し `task-id` を返すことがある（指示書が禁じる「自分で detach」ではない・起動は 1 回のまま）。`run_in_background` を
    自分で指定していなくても起きる。**対応 = panic kill / 再起動しない・polling ループで babysit しない**（token 浪費 +
    stall 誤認）。`task-notification status=completed` を待つ（Monitor の `until [ -f <出力> ]` ワンショットで待つのは可）。
    解析 script は最後に csv+selfscore を一括 write → 「まだ selfscore が無い」＝死んだ ではない。
12. **同一 `--out-dir` で bucket 違いの run を回すと bucket 無印の出力（`selfscore.md`）が後勝ちで上書きされる（reversal P-1 実証）**:
    large-liquid（採否）と mid-liquid（observation）を同じ out-dir に書くと、後発 run の `selfscore.md` が先発を潰す
    （per-block/per-bucket suffix 付き json は安全だが `selfscore.md` は無印）。**出力は bucket-suffix 付き
    （`selfscore_<bucket>.md`）にし、無印 `selfscore.md` は採否本線 bucket の copy にする**。複数 bucket を回すなら
    採否 bucket を **最後に** 走らせて canonical を確定するか、suffix 化して衝突を構造的に消す。
13. **harness の background/Monitor 完了通知は信用しすぎない・真値は process と redirect 実体（peer-relative P0 実証 2026-06-02）**:
    重い offline script を `uv run --with pyarrow python ... > redirect.out` で起動すると、この session では次の罠が連鎖した:
    - **(a) Monitor / run_in_background の "completed"・Monitor の streamed event が PHANTOM/STALE**: 既に kill 済みの
      別 run の redirect tail を echo して「`-> selfscore.md` … DONE」を**偽完了**として報告したり、launch wrapper が
      handoff した時点で "completed exit 0" を出す（python 本体はまだ report stage を回している）。**信用できる完了
      signal は 3 つだけ**: ① script 自身が redirect に最後に書く `DONE.`/`EXIT=` 行（**新しい redirect file のみ**・
      古い run の redirect を tail しない）、② Python `os.path.isfile(out/'selfscore.md')`（`ls`/`dir`/bash `[ -f ]` は
      この dir で listing race して空を返すことがある → **`os` stat が最も確実**）、③ `Get-CimInstance Win32_Process
      -Filter "Name='python.exe'"` の CommandLine match による生存確認＋`UserModeTime`/`WorkingSetSize` の前進。
      `tasklist //FI "PID eq N"` と CIM の count-only は **false-negative「PID exited」**を出す（race）→ これを信じて
      relaunch すると二重起動になる。
    - **(b) `uv run --with pyarrow` の ephemeral-venv child は harness の process-group teardown で report stage 途中に
      kill されうる**: launch wrapper の "completed" 後に子 python が掃除され、**parquet（pyarrow C++ writer で直書き）
      だけ残り、その後に Python `open()+json.dump` で書く report_*.json / selfscore.md が flush 前に消える**（write 順の
      途中で死ぬ＝pooled.json まで出て sector_by_sector 以降が無い、という部分成果になる）。**対策 = foreground smoke を
      teardown 前に終わる軽さにする**。**コストは code 数でなく anchor 数で決まる**（permutation が anchor×day をループ
      するため）: 50-code smoke（476 anchors）は perm_n=2000 でも ~30s で完走したが、full 471-code（5190 anchors）は
      pooled report だけで 13min。重い full run は司令塔 main 駆動・実装者 smoke は **anchor-light な小 subset + 小さい
      perm-n（behavior-preserving default を保った smoke-only lever）**で回す。permutation 回数は `--perm-n`/`--null-reshuffle`
      のような **default=frozen の lever** にしておくと smoke だけ軽くできる（`--limit-scan` と同じ思想）。
    - **(c) relaunch 前に必ず同名 script の既存 python を Get-CimInstance で確認**（実装者 agent の 2026-05-21 教訓の再演）:
      偽「PID exited」を信じて 2 本目を**同一 `--out-dir`** に起動すると、両者が同じ report file を奪い合い clobber する。
      relaunch は「CommandLine match で 0 件」を確認してから。別 out-dir に逃がすのも有効。
14. **accounting-only re-derivation（保存済み集約 artifact から生 leg を復元する task）は「leg 復元 == 保存集約値」の fidelity smoke が pick-fidelity gate（#26 peer-relative spread carry P0 実証 2026-06-02）**:
    保存 catalog が **netted/集約済みの値のみ**（例 `move = self_leg - basket_leg` だけで生 self_leg を持たない）を持つとき、新しい accounting（netting/turnover/cost）を被せるには生 leg を再導出する必要がある。
    正攻法 = engine の leg 式（`_continuous_move` 内の self_leg/basket_leg 構成）を **verbatim 移植**（catalog loader / `basket_excl_self` / span-gap guard / min_peers exception を含め一字一句）→ 再導出した `self_leg - basket_leg` が **保存 `move_bps` を 0.0 bps 以内で再現**することを全 anchor×horizon で smoke する（#26 は 14878 件で max diff=0.0）。これは 16-bis.6 の「fees-off net == 先行 probe net 完全一致」と同型の pick-fidelity gate を accounting 再導出版に拡張したもの。diff>0 なら leg 式の移植が崩れている → 先に直す。`_continuous_move` 等が **nested closure**（module から呼べない）なら、その body を verbatim 再実装し、この fidelity smoke で同値性を担保する（再実装が許されるのは「新 DoF = accounting のみ」「signal leg 式は不変」という task 制約下のみ）。
15. **edge と friction（cost）は必ず同一 base で正規化する（#26 P0 実証 2026-06-02）**: per-event / per-unit メトリクスで gross edge を book gross notional（`sum|net|`）で割り、friction を anchor 数（`n_anchor`）で割ると **base 不一致で friction が膨らむ**（#26 smoke で friction 533bps/event の偽値）。net = gross - friction を出すなら、両者を **同じ分母**（book gross notional sum|net|、または同じ traded-notional）に揃える。reconcile smoke = day-conditional cost JPY / total-traded-JPY ×1e4 が **想定 per-leg bps と一致**するか（#26: flat-leg=20.0bps 厳密一致、day-cond=200bps = range/2×regime と整合）を必ず print 確認してから net edge を信じる。

---

## 17. B-5 / B-6 — Basic Memory への記録（bs-wiki / bs-docs）

sweep 結果は **bs-wiki**（イベント記録）と **bs-docs proposals**（設計の生きたドキュメント）の**両方**に残す。どちらかだけでは不完全。Basic Memory MCP 経由で書く。

**B-5. wiki ページ仕上げ**: `ingest_run.py <best_run_id> --wandb-url <URL>` で `wiki/runs/<strategy>-<ts>.md` を生成（物理ファイル）→ BM が自動 index。`## Notes` / `## Decision` を手書きするときは `mcp__basic-memory__edit_note(project='bs-wiki', identifier='runs/<strategy>-<ts>', operation='append', content='...')`（§3 参照）。スイープ全体のサマリの log note 化は 司令塔の §6 業務 — 既定は §8 に従う（task ファイルが log 記録を完了条件にしている場合のみ実装者が `write_note(project='bs-wiki', folder='log', ...)` で書く）。

**B-6. proposals 更新（必須）**: 検証に少しでも携わったら `mcp__basic-memory__edit_note(project='bs-docs', identifier='plan/<strategy>-proposals', operation='append', content='...')` で proposals を更新する（無ければ `write_note(project='bs-docs', folder='plan', title='<strategy>-proposals')` で新設）。形式:
```markdown
## <実験名> 結果（YYYY-MM-DD）
### 検証概要      … 期間 / baseline run_id / sweep run_ids ファイルパス
### 指標の定義    … expectancy(gross)=summary.total_pnl/trades（手数料なし） / avg_win/avg_loss(net)=breakdown の pnl_net ベース
### 結果表        … | Cell | 独立変数 | total_pnl | trades | expectancy(gross) | 判定 |
### 各実験の結論  … 採用/棄却の根拠
### 次のステップ
```
proposals を更新しないと、次の AI が「なぜこのパラメータか」を理解できないままコードを触ることになる。bs-wiki の run
ページは run 単位の記録で、設計の文脈は bs-docs proposals にしか残らない。

---

## 18. Sweep でよくある失敗

| 問題 | 原因と対処 |
|------|-----------|
| `compute_summary` が 0 trades | エントリ条件が厳しすぎる → `k` を小さくする |
| Silver が空 | `ingest_run.py` を先に実行したか確認 |
| sweep が終わらない | セル数を減らす / `holding_minutes` 短縮。並列実行は e-station が in-process 1 プロセスのため非推奨 |
| **1 run に 18 時間** | `build_universe.py` を `--max-instruments` なしで実行すると 1,800+ instruments が入る。標準は `--max-instruments 50` |
| **expectancy が文脈で大きく変わる** | `summary.trade_count`(fill) と `breakdown.trade_count`(RT) の乖離。expectancy は常に RT 分母（§16-4）|
| **entry filter ON で trade_count が増えた** | 資本制約戦略では高値株を弾くと余力が増え安い株へ追加 entry。env 伝播は trade_count 変化でなく `meta.json` の `strategy_params_env` sidecar / `on_start` ログで確認 |
| **`python3` が Permission denied (Windows)** | `WindowsApps\python3` は実行不可のことがある。スクリプトはファイルに書いて `uv run python <script.py>` で回す（ヒアドキュメント不可）|
