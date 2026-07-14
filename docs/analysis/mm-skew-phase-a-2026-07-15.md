# Family 2 (両面 MM + inventory skew) — Phase A 決算

Date: 2026-07-15
Status: **CLOSED — KILL A（family 死亡、Phase B は実装しない）**
Scope: `docs/family-designs.md` Family 2 の Phase A（事前登録・実装前凍結プロトコル）

---

## 0. 一行結論

**Family 2 は Phase A で死んだ。** wide 銘柄（spread ≥ 8bps）3 銘柄すべてで
`capture/株 + adverse/株 ≤ 0`。両面化が変えるのは exit の taker→maker 置換と在庫制御であって、
per-fill の毒性そのものは変わらない — 入口の期待値がすでに負なので、出口の改善では救えない。
Phase B（両面提示・skew・pull の実装）は書かない。

---

## 1. 方法

`docs/family-designs.md` Family 2 Phase A の凍結スペックそのまま:

- 既存の無条件ベンチマーク maker 戦略（`BenchmarkJoin` = 常に bid に並ぶ→約定したら ask に並ぶ
  →`max_hold_secs` 超過で taker exit）を、追加実装ゼロで dev 3 銘柄（6834, 6368, 3110）に対し
  手数料 0・全営業日で実行（`maker-backtest --strategy benchmark --maker-bps 0 --taker-bps 0`）。
- 各銘柄で `Decomposition.spread_capture` / `Decomposition.adverse_selection` /
  `filled_maker_shares`（= maker 約定株数）を集計し、
  `capture/株 = spread_capture ÷ filled_maker_shares`、
  `adverse/株 = adverse_selection ÷ filled_maker_shares` を計算。
- **Kill A**: wide 銘柄のすべてで `capture/株 + adverse/株 ≤ 0` → family を殺す。

## 2. データ

セッション冒頭で `ingest-bronze` → `build-silver` を実行し、確定日を追いつかせた。

| 日付 | 状態 |
|---|---|
| 2026-07-09 | 確定・取り込み済み（既存） |
| 2026-07-10 | **未確定**（recorder の `.wal` が生存中 = 記録中断のまま放置。`discover_confirmed_dbs` の設計通り自動スキップ） |
| 2026-07-13 | 確定・取り込み済み（既存） |
| 2026-07-14 | **確定・新規取り込み**（今回のセッションで発見・ingest・silver 化） |
| 2026-07-15（当日） | recorder の当日ファイルは未確定（`today` 判定でスキップ、想定通り） |

→ 使用データは **3 営業日（07-09 / 07-13 / 07-14）**。07-10 は WAL が残ったままの中断記録で
確定条件を満たさないため対象外（`bronze.py` の設計通りで、データ欠損ではなく安全側の除外）。

**データ量の注意**: 3 日は family-designs.md の「MM Phase B の fit 開始可」マイルストーン
（5 日）に届いていない。ただし Phase A は「既存結果の再集計」であり日数要件がそもそも
明記されていない構造チェックなので、3 日でも実施可（実施順序§「Phase A は既存結果の
再集計なので即実施可」）。**Phase A の結論が 3 日限定の regime である可能性は残る**——
ただし後述の通り死因の符号は 3 日とも銘柄とも一貫しており、判定を覆すほどの脆さではない。

## 3. spread ≥ 8bps 前提の再検証

silver 全日・全銘柄で `(ask_px - bid_px) / mid * 10000` を集計（continuous session の
気配のみ、`bid_sign = ask_sign = '0101'` かつ `bid_px < ask_px`）:

| 銘柄 | mean spread (bps) | median spread (bps) | n snaps |
|---|---:|---:|---:|
| 6834 | 24.86 | 23.85 | 50,513 |
| 6368 | 16.88 | 16.27 | 30,969 |
| 3110 | 15.19 | 14.34 | 89,499 |

**前提は健在**。3 銘柄とも 8bps を大きく上回る（最も狭い 3110 でも median 14.3bps ≈
閾値の 1.8 倍）。design 文書時点の分類が崩れていないことを確認した。

## 4. BenchmarkJoin（無条件ベンチマーク、手数料 0、全 3 日集計）

`maker-backtest --strategy benchmark --maker-bps 0 --taker-bps 0`（デフォルト qty=100,
max_hold_secs=300, latency=0.5s, attribution=1.0）。

| 銘柄 | round trips | maker 約定株数 | spread_capture (JPY) | adverse_selection (JPY) | net/trip (JPY) |
|---|---:|---:|---:|---:|---:|
| 6834 | 120 | 17,300 | 123,500 | -195,000 | -3,350.00 |
| 6368 | 123 | 18,200 | 95,250 | -106,000 | -1,930.89 |
| 3110 | 726 | 133,200 | 29,250 | -72,750 | -212.12 |

（参考: inventory_drift・taker_edge も全銘柄で負。fill rate は 6834 2.4% / 6368 4.6% /
3110 44.5% — 3110 だけ極端に高いのは spread が最も狭くタッチが動きやすいため。
`docs/architecture.md` 記載の 6834 -4,564円/往復（2026-07-10 時点 1 日データ）と比べて
今回は -3,350円/往復とやや改善しているが符号は変わらない — fill モデルが健全（タダで
儲かる楽観がない）ことの再確認でもある。）

## 5. capture/株・adverse/株・Kill A 判定

| 銘柄 | capture/株 (JPY) | adverse/株 (JPY) | **capture/株 + adverse/株 (JPY)** |
|---|---:|---:|---:|
| 6834 | +7.14 | -11.27 | **-4.13** |
| 6368 | +5.23 | -5.82 | **-0.59** |
| 3110 | +0.22 | -0.55 | **-0.33** |

3 銘柄すべてで `capture/株 + adverse/株 < 0`（≤ 0 の判定を満たす）。

## 6. 判定

**Kill A 発火。Family 2 は Phase A で不成立と判定する。**

wide 銘柄のすべてで、無条件ベンチマークの per-fill 期待値（capture − |adverse|）が
負のまま。両面化の効能は「exit を taker から maker に変える」「在庫を skew/pull で制御する」
の 2 点に限られ、どちらも **入口の fill が「シグナルが外れた瞬間にだけ起こる」逆選択**
そのものは変えない。前 family（imbalance-gated passive entry）で確立した死因
（adverse selection がスプレッド獲得を上回る）が、シグナルゲート無しの素の maker fill
でも再現している ——つまり両面化で解決しようとした taker_edge バケツ（-184,500 / -86,750 /
-33,250 JPY、片側 exit の半スプレッド負担）を maker exit に変換できたとしても、
capture + adverse が既に負である以上、そこで浮く分だけでは埋まらない
（例えば 6834: taker_edge -184,500 を丸ごと 0 にできても、capture+adverse の -71,500 は
残る母数の問題であり別バケツ）。

`docs/family-designs.md` の pre-registered kill criteria に従い、**Phase B（両面
touch/improve 提示・inventory skew・toxic-side pull の実装）は書かない**。

## 7. Phase B が通過していたら何が必要だったか（不実装・参考のみ）

- `maker_strategies.py` に新戦略クラス（例 `TwoSidedSkewMaker`）を追加:
  両側に同時 post（bid・ask 両方を `_reconcile` で維持、片側 fill 後も反対側を再提示）。
  現在の `_reconcile` は「1 つの desired (side, px, qty)」しか扱えないため、
  両側管理には拡張が要る（各 side ごとに独立した desired を渡すインターフェース、
  または 2 回呼ぶ形に変更）。
- 在庫 `q`（`OwnView.position`）に応じた skew: 目標気配を `mid ± half_spread ∓ skew_ticks(q)`
  にずらす関数、`|q|` 上限で新規建て停止。
- imbalance がトキシック側に振れたときの該当側 pull（`l1_imbalance` は既存の
  `maker_strategies.py` にあるので再利用可）。
- 事前登録 ablation（skew off / pull off）を同時に回す仕組み（`maker-sweep` 相当の
  グリッド、または新規スクリプト）。
- Kill B の判定（手数料 0・≥100 trips/銘柄日・wide dev 3 銘柄の 2/3 で net/trip > 0）。

上記はこのセッションでは実装していない。Kill A が発火したため設計のみに留める。
