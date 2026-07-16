# gen1 LightGBM × トリプルバリア taker — IS-KILL（2026-07-16）

- Family: `scalp-taker-triplebarrier-lgbm`（gen1、honest-N = 1）
- 判定: **IS-KILL** — 07-13 IS validation で候補セル 0。凍結プロトコルに従い
  **07-14（OOS）は未開封のまま**。OOS 探針・G2 代替ヌルは実施していない。
- config hash: `28eb2ba6cf0c22d718bba5e744bcc2d22c05e14e7447759650ce6b4f3d4db866`
- 実装コミット: `b09b2be`（テスト 47 本通過後に validation を開いた）

## プロトコル（事前凍結・DESIGN.md 準拠）

07-09 学習 → 07-13 で 120 セル（horizon {5,15,30,60,120,300}s × mult {1.5,2,2.5,3} ×
τ {0.40..0.80}）評価。候補条件 `n≥100 ∧ net/entry>0 ∧ gross/friction≥3`。
執行は保守的 taker（next-PUSH 対向 best 約定・トリプルバリア・銘柄単位 1 ポジ・
1Hz 決定・14:55 強制決済）。ラベルとシミュレータは同一のバリア解決関数を共有。

## 結果の構造

- 発火セル 117/120、うち net/entry > 0 は 23 セル。しかし候補条件を全て満たすセルは **0**。
- n≥100 の 91 セル中央値: **gross 1.67 bps < friction 3.96 bps** — 過去の全 family と
  同型の死因「グロスが摩擦の床に届かない」。
- 最良の n≥100 セル: `h5s × m3.0 × τ0.70` → net +2.88 bps, gross 5.95, friction 3.07,
  **ratio 1.94**（< 3.0）, hit 0.68, n=133。
- net>0 の 23 セルは (a) τ=0.80 の極小 n テール（n=1〜50、選択ノイズ域）か
  (b) h5s×τ0.7 帯の ratio 1.7〜1.9 のどちらか。

## 解釈（次サイクルへの手がかり・再選択はしない）

1. **方向シグナルは完全なゼロではない**: h5s×τ0.7 帯が val 日で net 正・hit 0.68。
   ただし ratio ゲートに遠く、2 日学習/1 日検証の範囲では逸話に過ぎない。
2. **学習データ 2 日が支配的制約**。毎営業日録画の再開（キュー #3）が最優先のまま。
3. 本判定を見てのセル/閾値/格子の変更は **新 family + 新 sealed データが必要**（G8）。
   録画が積もった後の同一プロトコル再実行は同 family の次サイクルとして記録する。

## 成果物

- `artifacts/gen1/sweep_val_results.json`（120 セル全成績・ローカルのみ）
- `artifacts/gen1/frozen_cell.json`（IS-KILL 宣言）
- 07-14 のキャッシュ・ラベル・成績は一切計算していない（封印維持）
