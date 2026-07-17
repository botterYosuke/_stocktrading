# 足読み gen8 — cost-aware complete-transaction selector 事前凍結 (2026-07-17)

- Family: `gen8_net_selector_v1`
- Status: **PREREGISTERED**（本書コミット後にコード実行。実行後の本書変更は禁止 — 変更したら新 family）
- 台帳: 起案時点の直近判定は 383。本 family は G8 honest-N を 1 消費する新 family。
- 位置づけ: **足単独エントリーの最後の未検証仮説**。2026-07-17 の診断
  （vault: 株価シュミレーション.md シグナル節・Diary/2026-07-17）の実行部。

## 1. 仮説（何が未検証か）

gen2/gen3 は「long/short どちらが TP へ先着するか」の 3 クラス分類、gen4/gen5 は相対
リターン順位を学習した。**保守的な完全往復後の実現 `net_bps` を直接教師にした学習は
7 世代のどれも行っていない**。「分足に方向情報がない」は確定しているが、「分足から
正の条件付き net を持つ少数取引を、下側分位の棄却付きで選べない」は直接検定していない。

- H1: 足由来特徴で `E[net_bps | x]` の下側分位が正となる決定行が存在し、そこだけ
  発注すれば ADR-0001 G1-G8 を通過する。
- H0: そのような決定行は選べない（予測分位が正の行が僅少・または正でも実現 net が
  ヌルと区別できない）。**7 世代の証拠は H0 側に強く傾いている。**

## 2. 凍結する構成要素（探索はこの外に出ない）

### データ・特徴・執行（gen2 と完全同一 — 追加特徴量競争なし）

- ユニバース: gen2 の 17 銘柄（フル履歴 2024-01-04〜2026-02-18・事前固定）。
  survivorship 限界は gen2 文書と同じ（フル履歴がある = 生き残り大型株）。
  gen4 型カバレッジバグは構造上該当しない（OOS 月のデータ有無による脱落なし）。
- 特徴量: `ALL_FEATURE_NAMES`（own + cross）。gen2 の feature_schema_hash と同一。
  新特徴・銘柄 ID 特徴は追加しない。
- friction: gen2 凍結値（板録画 07-09+07-13 実測 median spread × 1.25）。
- 執行: `exec_bars.barrier_outcomes_bars` の保守的バー執行（次バー始値 + 不利半
  スプレッド・両接触は SL・ギャップ SL は始値・timeout/EOD はバー始値）。
- 板・quote の用途は約定価格・spread・friction 会計のみ（シグナルに使わない）。
- キャッシュ: gen2 の isval キャッシュを読み取り専用で再利用（同一 config のため）。

### 教師（本 family の新規部分）

決定行 i × サイド s ∈ {long, short} × セル (h, mult) の**実現 net_bps**
（`execution.trade_pnl_bps` の恒等式 gross − friction = net。半スプレッド往復込み。
`reason != EXIT_NONE` の行のみ有効）。3 クラスラベル・softmax・τ は使わない。

### モデル

- LightGBM 2 本/サイド × セル: ①期待値回帰（objective=regression, L2）
  ②下側分位回帰（objective=quantile, alpha=α）。
- ハイパーパラメータは gen2 の `LGBM_PARAMS` から objective 系のみ差し替え
  （`num_class` 除去）。他は全て同一・seed 20260716・300 rounds・early stop なし。
- プール学習（全銘柄結合・P2 相当）。パターン次元の探索はしない。

### 売買規則

決定行 d で: long 発火 ⟺ q̂_α(net_long|x_d) > 0、short 発火 ⟺ q̂_α(net_short|x_d) > 0。
両サイド発火なら期待値回帰の大きい側、同値なら見送り。銘柄単位 1 ポジション
（busy_until は gen2 と同一）・銘柄間は無制限・仮想 1 単位。**棄却率 99% 超も許容**
（発火しないことは失点ではない — ADR-0001 §1）。

### 探索格子（これが探索の全て・18 構成）

- セル: (h, mult) ∈ {5, 15, 30} × {1.0, 2.0}（gen2 の 6 セル）
- α ∈ {0.05, 0.10, 0.20}

### 日割り

| 区間 | 範囲 | 用途 |
|---|---|---|
| TRAIN | 2024-01-04〜2025-04-30 | 学習のみ |
| VAL | **2025-05-01〜2025-06-30** | 選定（候補条件・凍結・ヌル） |
| FINAL_FIT | 2024-01-04〜2025-09-30 | OOS 直前の再学習（学習データとしてのみ） |
| OOS | 2025-10-01〜2026-02-18 | sealed・一発・oos_lock |

**選定窓の汚染開示**:
- 2025-07〜09 は gen2/gen3 の val として選定メトリクスを見た → **選定に再利用しない**
  （FINAL_FIT の学習行としてのみ使用。診断 2026-07-17 の凍結条件）。
- 2025-04 は gen4/5/6 の sealed OOS（未開封・封印維持宣言あり: gen7 文書 §封印維持）
  → 選定メトリクスを計算しない。TRAIN の学習行としては gen2 前例（gen2 TRAIN 内）
  に従い使用する。
- 2025-05〜06 は gen3 が early-stop 窓（validation loss のみ）に使用した。取引選定
  メトリクスをこの窓で見た family は無い。既知の最小汚染として開示の上、VAL に使う。

### 候補条件（VAL・gen2 と同一）と選定順

n ≥ 300 / D ≥ 20 / net_per_entry > 0 / ratio = gross÷friction ≥ 3.0 /
max_code_share ≤ 0.5。選定順: net desc → n desc → h asc → mult asc → α asc。
候補ゼロなら **IS-KILL**（OOS は開けない）。

### 凍結構成のヌル検定（OOS 開封の前提条件）

VAL の凍結構成 trades に対し `nulls.time_shuffle_null`（同銘柄・同 30 分帯・同数・
同 side 構成・200 レプリケート）と `nulls.side_shuffle_null`（200 レプリケート）。
**時刻ヌル p_upper ≤ 0.05 を満たさなければ IS-KILL**（選定器が何も選んでいない）。
サイドヌルは報告必須（判定はG2 総合で）。

### OOS 一発

FINAL_FIT で同一ハイパーのまま再学習 → OOS へ適用 → `trade_metrics` + 両ヌル →
ADR-0001 G1-G8 採点。oos_lock.json で再実行を拒否。config_hash 不一致なら開封拒否。

## 3. 判定と撤退条項（全経路を事前宣言）

| 経路 | 判定 | 帰結 |
|---|---|---|
| VAL 候補ゼロ | IS-KILL | 下記「終了条項」発動 |
| VAL ヌル不通過 | IS-KILL | 同上 |
| OOS で G1-G8 いずれか不通過 | KILL | 同上 |
| OOS 全通過 | PASS | forward 鑑定（ペーパー）へ。終了条項は発動しない |

**終了条項**: 本 family が PASS 以外で終わった場合、**分足・日足のみを入力とする直接
エントリー系 family の起案を永久に終了する**。以後の足読み ML はレジーム・銘柄選択・
no-trade ゲート（板読みの前段）としてのみ検討する。「もう一度だけ」は無い —
本書がその「一度」である。

## 4. 実装物

- `src/scalp_agent_bars/minute/selector_config.py` — 本書の凍結値 + config_hash
- `src/scalp_agent_bars/minute/selector.py` — 教師生成・売買規則（pure）
- `scripts/gen8_net_selector_pipeline.py` — sweep（train→val 凍結）/ oos（一発）
- `tests/` — 恒等式・売買規則・分割不重複・config_hash 固定のテスト

## 5. 参照

- 評価基準: `docs/adr/ADR-0001-evaluation-standard.md`（G1-G8）
- 先行世代: `docs/analysis/gen2-minute-p1p2p3-is-kill-2026-07-16.md` ほか gen1b〜gen7
- 診断の正本: vault `Projects/株価シュミレーション/株価シュミレーション.md`
  2026-07-17 シグナル節 / `Diary/2026-07-17.md`
