---
title: signal_driven_day_trade proposals
tags: [signal_driven_day_trade, proposals]
strategy: signal_driven_day_trade
---

# signal_driven_day_trade — Research Proposals

**BM sync pending**: BM MCP 未登録のため物理ファイル直書き。MCP 復帰 session で bs-docs に同期すること。

---

## Phase IS-0: catalog 拡張 + IS baseline (2026-06-04)

**Verdict**: Adopt — 実装・実行中

**背景**:
- _stocktrading signals pipeline (LSTM) 完成、manifest.json に 192 銘柄
- TTWR smoke (2025-01-14〜17) 実行: trade_count=25, win_rate=0.48, total_pnl=+46,426 JPY
- fill rate 20/192 (10.4%) — root cause: catalog に signal universe の 20 銘柄しかない

**実施内容**:
- `scripts/extend_catalog_signal_universe.py` を TTWR に新規作成 (commit 5a7b130e)
- 設計 grilling Q1-Q9 完了（manifest 動的読み込み / MINUTE 限定検出 / dict lookup / per-month write / date range CLI / auto-resolve manifest / ASCII print / fail-fast catalog + source）
- 重い catalog build (172+ 銘柄 × 2024-11-01〜2025-01-30) を司令塔 main から launch 中

**IS Baseline 結果 (2026-06-04)**:

| Metric | Smoke (20銘柄) | IS Baseline (192銘柄) |
|---|---|---|
| run_id | 1780546533-...-1306_TSE | 1780554977-...-1306_TSE |
| bars | — | 97,133 |
| trade_count | 25 | 320 |
| fills | 46 | 526 |
| total_pnl | +46,426 JPY | +159,904 JPY |
| win_rate | 0.48 | 0.484 |
| max_drawdown | 5,950 JPY | 44,579 JPY |
| catalog coverage | 20/192 | 192/192 |

run_dir: `C:\Users\sasai\AppData\Roaming\flowsurface\run-buffer\1780554977-signal_driven_day_trade_smoke-1306_TSE`

**Phase IS-0: Completed (2026-06-04)**

**Silver Breakdown 結果 (2026-06-04)**:
- pipeline: **PASS**
- IS baseline: **PASS** as initial evidence
- signal edge: **PROVISIONAL** — 集中度依存、再現性チェック必要
- 2643.TSE 単独 +290K JPY (proxy) → top 2 銘柄で engine PnL の ~2.7x
- SHORT 主体 84%、confidence mean 0.813、190/192 銘柄で約定、全ポジション決済

wiki: `_sacrificial-lamb/wiki/runs/signal_driven_day_trade_smoke-1780554977.md`

**次のステップ (優先順)**:
- [x] 1. 2643.TSE 除外 replay → 集中度感度確認 **PASS**
  - no2643 total_pnl = +159,904 JPY (baseline と 1円も変わらず)
  - proxy PnL (+290K) は notional flow であり PnL 帰属ではなかった
  - 2643.TSE の真の PnL 寄与 ≈ 0 JPY、PnL は 189 銘柄に分散
  - run_id: 1780566808-signal_driven_day_trade_no2643-1306_TSE
- [ ] 2. 広い IS window (2025-01-14〜2025-03-31) → signals 再生成 + catalog 期間拡張
- [ ] 3. 日別 PnL と symbol contribution の安定性確認
- [ ] 4. SHORT basket の market/sector 偏り確認

**参照**:
- task doc: `docs/task/signal_driven_day_trade-phase-IS0-catalog-extend-task.md`
- handoff: `C:\Users\sasai\AppData\Local\Temp\handoff-catalog-expansion-2026-06-04.md`
- smoke run_id: `1780546533-signal_driven_day_trade_smoke-1306_TSE`
