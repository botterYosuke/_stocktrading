# gen1b 板非依存（分足/日足）LightGBM × トリプルバリア taker — IS-KILL（2026-07-16）

- 系統名: **足読み (ASHIYOMI)** — 日足・分足シグナル系（板 PUSH 系は「板読み (ITAYOMI)」）
- Family: `scalp-bars-triplebarrier-lgbm`（足読み gen1b・**新 family**、honest-N +1）
- 発端: owner 指示「板情報を使わずに日足・分足を用いてスキャルピングする兄弟 ML-Agent」
- 判定: **IS-KILL** — 07-13 IS validation で候補セル 0。凍結プロトコルに従い
  **07-14（OOS）は未開封のまま**。OOS 探針・G2 代替ヌルは実施していない。
- config hash: `2d25ce07205dce80801198511ed3313b49f37960f8833358fd7c201bbfb6beb3`
- feature schema hash: `fec7cbbf79a36cc8d359ff47dd76409191cb1adf7d277755ee831441cc02a9b8`

## プロトコル（事前凍結・DESIGN.md「兄弟エージェント gen1b」節準拠）

07-09 学習 → 07-13 で 120 セル（horizon {60,120,180,300,600,900}s × mult {1.5,2,2.5,3} ×
τ {0.40..0.80}）評価。候補条件 `n≥100 ∧ net/entry>0 ∧ gross/friction≥3`（gen1 と同一）。

gen1 との違いは**シグナルだけ**: 特徴量は板（spread/imbalance/depth/OFI/microprice）を
一切使わず、歩み値由来の 1 分足 18 特徴（k 本リターン・レンジ・close 位置・出来高比・
セッション VWAP/寄り/高安乖離・時刻・前日 gap/リターン/レンジ）。決定は取引のあった
1 分バーの確定境界のみ。執行・ラベル・friction 計上は gen1 と同一コード
（保守的 next-PUSH 対向 best 約定・トリプルバリア・銘柄単位 1 ポジ・14:55 強制決済）。

## 結果の構造

- 発火セル 118/120。**net/entry > 0 のセルは n を問わず 0**（gen1 は 23 セルあった）。
- n≥100 の 95 セル中央値: **gross −0.15 bps / friction 4.13 bps / net −4.29 bps**。
  グロスが摩擦に届かない以前に、**グロス自体がゼロ近傍〜負** — 1 分足に taker
  スキャルの方向情報が実質存在しない。
- 全セル最良（n=6 の極小テール）でも net −2.06 bps。n≥100 の最大 gross は +1.03 bps。

## 解釈

1. **板なし分足のみでは、gen1（板あり 1Hz）より情報が明確に少ない**。gen1 の
   gross 中央値 1.67 bps に対し gen1b は −0.15 bps。板特徴（OFI・imbalance・
   microprice）が gen1 の限られたグロスの源泉だったことの傍証。
2. 台帳の先行 KILL（寄り ORB・日中モメンタム・daily VWAP reclaim 等の分足/日足系）と
   整合的。friction 正直会計の下で bars-only 日中スキャルは繰り返し死んでいる。
3. **学習データ 2 日という制約は gen1 と共通**だが、gen1b は gross 自体が負のため
   「データ不足で候補が薄い」ではなく「シグナル欠如」の側に読み取れる。録画が
   積もった後の同一プロトコル再実行は同 family の次サイクルとして記録可能だが、
   優先度は gen1 系（板あり）より下と評価する。
4. 本判定を見てのセル/閾値/特徴/イベント定義の変更は **新 family + 新 sealed
   データが必要**（G8）。

## 成果物

- `artifacts/gen1b/sweep_val_results.json`（120 セル全成績・ローカルのみ）
- `artifacts/gen1b/frozen_cell.json`（IS-KILL 宣言）
- キャッシュ: `artifacts/cache/gen1b/`（07-09・07-13 のみ。07-14 は一切計算していない）
- 実装: `src/scalp_agent/bars/` + `scripts/gen1b_pipeline.py`（テスト 88 本通過後に
  validation を開いた）
