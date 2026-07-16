# 足読み gen3 — 系列 NN（生分足入力）での学習器強化 — IS-KILL（2026-07-16）

- 系統名: **足読み (ASHIYOMI)** gen3
- Family: `gen3_minute_nn_v1`（新 family・honest-N +1）
- 発端: gen2 IS-KILL に対する owner の疑問「学習が弱いから gross がゼロなのでは？」
  → 学習器強化の対照実験を owner 指示で実施
- 判定: **IS-KILL** — val 62 営業日で 2 パターン × 18 構成の全てが候補条件を満たさず。
  **OOS（2025-10-01〜2026-02-18）は引き続き未開封のまま封印**。
- config hash: `5230658c92120f9cd88996b57b753975f97cad86fb0ecc3a55e92a51faa65351`

## 実験設計（gen2 との厳密な対照）

**変えたのは学習器と入力表現だけ**。ラベル（ATR 錨トリプルバリア）・保守的バー執行・
friction 較正・train/val/OOS 分割・候補条件は gen2 と行単位で同一
（gen2 キャッシュの決定行と b_ts 完全一致を assert）。

- 入力: 手作り 26 特徴 → **当日直近 32 本の生分足系列**（リターン・レンジ・実体・
  上下ヒゲ・出来高比・時刻・mask の 8ch）+ 静的 4 特徴（gap・前日・ATR）
- 学習器: LightGBM → **GRU 2 層（hidden 64）+ 6 セル・マルチタスク分類ヘッド**
  （PyTorch 2.6 + RTX 3050・DESIGN 決定 5 の NN 比較を前倒し実施)
- 学習規律: fit = 2024-01〜2025-04、early stop は train 末尾窓（2025-05〜06）のみ。
  公式 val は掃引まで不使用。epoch 7 で early stop（estop CE 0.892）

## 結果（val 2025-07〜09・62 日）

- n≥300 の 10 構成・のべ 311,645 取引: **gross 中央値 −0.18 bps・最大 −0.05 bps**
  （全構成で負）。friction 3.59 bps、net 中央値 −3.76 bps。
- NN の softmax score は τ≥0.55 でほぼ発火ゼロ（n=1 のみ）— **モデル自身が確信を
  持てる局面を見つけられていない**。LightGBM（gen2）は τ0.65 でも数十〜数千件
  発火していたので、NN は「自信過剰にすらならず、素直に何もない」と答えた形。
- 学習曲線: fit CE 0.913→0.884 で頭打ち。クラス事前分布+α までは学習するが、
  それ以上の構造が train 内にも見つからない。

## 解釈 — 「学習が弱い」仮説の棄却

1. gen2（勾配ブースティング×手作り特徴）と gen3（系列 NN×生バー）という
   **性質の異なる 2 つの学習器ファミリーが、同じデータ・同じ執行で gross≈0 に収束**。
   学習器の弱さが原因なら、入力表現を生系列に広げた NN で改善の兆しが出るはず。
2. 板読み gen1 では同系の label/実行設計で gross +1.67bps が出ている（学習
   パイプラインは情報があれば拾える）。
3. よって「分足 OHLCV に taker スキャル可能な短期方向情報が実質存在しない」を維持。
   学習器はボトルネックではない。
4. 本判定を見た構成変更は新 family + sealed データ（G8）。OOS 2025-10〜2026-02 は
   未開封のまま次の新 family に使える。

## 成果物

- `artifacts/gen3_minute_nn/`: model.pt・train_log.json・sweep_val_results.json・
  frozen_config.json（IS-KILL 宣言）
- 系列キャッシュ: `artifacts/cache/gen3_seq/isval/`（OOS は未構築）
- 実装: `src/scalp_agent_bars/minute/{nn_config,sequences,nn_model}.py` +
  `scripts/gen3_minute_nn_pipeline.py`（テスト 112 本通過後に val を開いた）
- 依存: pyproject の `nn` グループに torch 2.6.0+cu124（PyTorch index を torch のみに束縛）
