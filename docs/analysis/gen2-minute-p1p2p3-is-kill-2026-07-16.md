# 足読み gen2 — stocks_minute 歴史分足での P1/P2/P3 再学習 — IS-KILL（2026-07-16）

- 系統名: **足読み (ASHIYOMI)** gen2
- Family: `gen2_minute_v1`（新 family・honest-N +1）
- 発端: owner 指示「`S:/jp/stocks_minute` を使って再学習。銘柄別 / 相関銘柄インプット /
  複数銘柄同時学習+シグナル銘柄エントリーなどを試行錯誤」
- 判定: **IS-KILL** — 2025-07-01〜09-30 の 62 営業日 val で、3 パターン × 18 セルの
  全 54 構成が候補条件（n≥300 ∧ D≥20 ∧ net>0 ∧ ratio≥3 ∧ 銘柄集中≤0.5）を満たさず。
  **OOS（2025-10-01〜2026-02-18）は未開封のまま封印**。
- config hash: `7f05c68021ec5ad2cdcec4a1ac0f62623a3ebf21f6626710aa7b3beab44d291e`
- feature schema hash: `f2fef1441627a9382cbf8f04f13f6a6c5da934f3c7974fcac63abfb97cfae5ec`

## gen1b からの前進点（データと検定力）

gen1b は録画 2 日（train 1 日 / val 1 日）だったが、gen2 は歴史分足
2024-01-04〜2026-02-18 のフル履歴 17 銘柄（メガキャップ・板録画ユニバースの部分集合）
で train 371 日 / val 62 日を確保。**G4 の D≥20 を初めて正式に満たす検定力**での判定。

執行は板が無いため保守的バー執行モデル（DESIGN「足読み gen2」節）: 次バー始値 +
不利半スプレッド、同一バー両接触=SL、ギャップ不利約定、ATR20 錨バリア、14:55 強制決済。
friction は板録画 07-09+07-13 の実測銘柄別スプレッド（1.5〜5.3bps）× 1.25。

## 結果（val 62 日・n≥300 セルの中央値）

| パターン | セル数 | のべ取引 | gross/entry | friction/entry | net/entry |
|---------|--------|----------|-------------|----------------|-----------|
| P1_single（銘柄別 17 モデル） | 17 | 545,486 | **−0.046 bps** | 3.53 bps | −3.54 bps |
| P2_cross_features（プール+市場/ペア特徴 26） | 10 | 301,165 | **−0.073 bps** | 3.53 bps | −3.56 bps |
| P3_pooled_topk（各分境界 score 最大 1 銘柄） | 9 | 86,707 | **+0.007 bps** | 3.56 bps | −3.53 bps |

- n≥300 の全セルで最大 gross は +0.22 bps（P1）。ratio≥3 に必要な ~10.6bps の 1/48。
- net>0 は n≤21 の極小テール（hb5_a10 τ0.65 の n=2 等）のみ — 選択ノイズ域。
- 日クラスタ t は大 n セルで −20〜−113（gross=0 で friction 一定なら機械的に強負）。

## 解釈

1. **3 つのアーキテクチャ（銘柄別・クロス特徴・クロスセクション選択）はどれも
   グロスをゼロから動かせなかった**。分足 OHLCV に taker スキャル向きの短期方向情報が
   実質存在しないという gen1b の結論が、62 日 × 93 万取引 × 17 銘柄で再確認された。
2. 板読み gen1 の gross 1.67bps（1Hz・板特徴）との対比が明確になった。**エッジ候補の
   源泉は板のミクロ構造（OFI・imbalance・microprice）**であり、価格・出来高の
   バー集計では消える。
3. クロス銘柄情報（市場平均・ブレス・最相関ペア）も分スケールでは足しにならない
   （P2 は P1 と同水準）。P3 の「最強シグナルだけ取る」選択も、選択対象のシグナル
   自体に情報が無いため機能しない。
4. 本判定を見た構成変更（特徴・バリア・horizon・universe・執行モデル）は
   **新 family + sealed データが必要**（G8）。sealed OOS 2025-10〜2026-02 は
   未開封なので、次の新 family が同データを OOS として使う設計は可能。

## 成果物

- `artifacts/gen2_minute/sweep_val_results.json`（54 構成の全成績）
- `artifacts/gen2_minute/frozen_config.json`（IS-KILL 宣言）
- `artifacts/gen2_minute/friction_spread_bps.json`・`peer_map.json`（凍結較正）
- キャッシュ: `artifacts/cache/gen2_minute/isval/`（train+val のみ。OOS は未構築）
- 実装: `src/scalp_agent_bars/minute/` + `scripts/gen2_minute_pipeline.py`
  （テスト 106 本通過後に val を開いた）
