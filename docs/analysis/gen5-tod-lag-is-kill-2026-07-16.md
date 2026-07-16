# 足読み gen5 — 同時刻 lag 横断ランキング family (`gen5_tod_lag_v1`) IS-KILL

- Date: 2026-07-16
- Verdict: **IS-KILL**（val 全 4 構成が候補条件未達。OOS 2025-04 は封印のまま未開封）
- config_hash: `2d2d3ee94a78e4ff1d2bfbf4ac8ea0ff8d85e378da01e4599cda3df4c6519b60`
- Code: `src/scalp_agent_bars/xsec/tod_lag.py` + `scripts/gen5_tod_lag_pipeline.py`
- Artifacts: `artifacts/gen5_tod_lag/`（sweep_val_results.json / frozen_config.json）

## 1. 仮説（owner 指示・arXiv:1005.3535 由来）

gen4 との違いは signal のみ: 直近 15〜60 分の値動きではなく**「過去営業日の同一
30 分枠」の市場・業種控除後リターンの lag 1〜40 営業日系列**を EWMA
（half-life 5 日 / 20 日の 2 本だけ）して横断 rank する。根拠は米国株で
30 分リターンの横断的継続が 1 営業日の整数倍 lag（同時刻帯）に発生し
40 営業日以上持続するという報告（Heston-Korajczyk-Sadka, arXiv:1005.3535）。

**「同時刻だから効く」を別時刻対照で証明することが本体** — 通らなければ
日足モメンタムの焼き直しにすぎない（owner 指示原文の要旨）。

## 2. 凍結構成

- gen4 から継承: 月次 PIT ユニバース 400 銘柄 / 判断時刻 5 枠（09:30, 10:00,
  10:30, 13:00, 14:00）/ friction 呼値ラダー保守モデル（median 14bps）/
  分割 train 2024-04-01〜11-29・val 2024-12-02〜2025-03-31 (78 日)・
  OOS 2025-04 sealed / 入力は gen4 凍結キャッシュ `dataset_isval.parquet`
  （485,949 行。新しい分足走査なし）。
- signal: (code, tod) × 営業日に pivot した `h30_adj_bps` の lag 1〜40 の EWMA
  （重み 0.5^(L/hl)、有効 lag < 20 本は signal なし）。day d・tod T の値は
  d 当日 T+30 分確定のため lag ≥ 1 日で因果。
- 執行: 次バー open 入り → 30 分後バー open 決済（taker）。horizon 30 分のみ。
- 格子: {hl5, hl20} × {K5, K10} の 4 セル（G8: 探索はこれが全部）。
- 対照 3 種（val を見る前に候補条件へ組み込み）:
  1. **別時刻 lag (rotation)**: 各 tod の score を別 tod の lag 系列で計算（4 shift）。
     候補条件: 実測 gross/entry > rotation 4 種の gross/entry 平均。
  2. **時刻 permutation**: tod 対応の全 non-identity 置換 119 通りをヌル分布に。
     候補条件: net/entry の p 値 < 0.05。
  3. **ランダム銘柄**（gen4 G2 流用）: null_gap_z ≥ 2。

## 3. 結果（val 78 日・ADR-0001 採点）

全 4 構成:

| 構成 | n | net/entry | gross/entry | ratio | t_net | 同時刻gross vs 別時刻対照平均 | perm_p |
|---|---|---|---|---|---|---|---|
| hl5 K5 | 3,900 | −12.89bps | +0.80bps | 0.06 | −9.8 | +0.80 > +0.17 | 0.267 |
| hl5 K10 | 7,800 | −12.25 | **+1.54** | 0.11 | −14.6 | +1.54 > +0.32 | 0.067 |
| hl20 K5 | 3,900 | −12.74 | +1.24 | 0.09 | −11.5 | +1.24 > +0.71 | 0.267 |
| hl20 K10 | 7,800 | −12.74 | +1.39 | 0.10 | −17.0 | +1.39 > +0.24 | **0.042** |

最良 gross 構成の詳細（hl5|K10）:

```
発火    : D=78/78 日 (100%)   n=7,800 (100/日)
主指標  : net/entry=−12.25bps  gross=+1.54bps  friction=13.78bps
          ratio=0.11  t_net=−14.6  hit=38.3%
ヌル    : ランダム同数銘柄 = −13.42bps → GAP=+1.18bps (z=+2.54)  [G2 通過]
対照    : 別時刻 rotation gross 平均 +0.32bps < 実測 +1.54bps
          時刻 permutation p(net)=0.067 (119 置換)               [perm 条件未達]
集中度  : 最大日 3.3% / 最大銘柄 3.1% / 最大月 26.5%              [G3 通過]
deciles : bottom −1.73 … top +1.04bps (adj、spearman 0.96)
左裾    : stress(+4tick+10bps) net=−49.4bps
IS/OOS  : IS-KILL — OOS 封印のまま未開封                          [G5]
VERDICT : KILL（G6: ratio 0.11 << 3.0、net < 0）
```

## 4. 解釈 — 「同時刻 lag 信号は弱く実在するが、gross が摩擦の 1/10」

- **信号は弱く実在する**: decile 単調性 0.92〜0.96（gen4 の 0.60〜0.77 より
  きれい）、同時刻 gross > 別時刻 rotation 平均が **4 セル全部**で成立、
  permutation p は最小 0.042（hl20|K10）〜0.067（hl5|K10）。米国株の同時刻
  継続（arXiv:1005.3535）の弱いエコーは日本株 2024〜25 にも観測される。
- **しかし経済的に無価値**: 最良 gross +1.54bps/30 分 << friction 14bps
  （ratio 0.11）。ratio ≥ 3 には friction ≤ 0.51bps が必要で、日本株最タイトの
  実測スプレッド（1.5〜5.3bps）より 1 桁小さい。**KILL は friction モデル
  非依存**（gen4 と同じ構図）。top decile の控除後リターン +1.04bps/30 分は
  gen4 の +0.59bps/15 分と同じ桁 — 定式化を変えても gross の天井が動かない。
- 「同時刻だから効く」の証明も**未達**: perm_p が 0.05 を切ったのは 4 セル中
  1 セルのみ（hl20|K10 の 0.042、多重比較補正なしの名目値）。信号の同時刻
  特異性は方向としては見えるが、統計的に立証されたとは言えない水準。
  仮に立証されても gross の桁が 2 桁足りないため判定は不変。
- これで**分足・日足系は 5 family 連続 KILL**（gen1b / gen2 / gen3 / gen4 /
  gen5）。「現代日本株の分足 OHLCV には 15〜60 分 horizon で摩擦を超える
  シグナルが無い」がさらに強化された。

## 5. 既知の限界（結論には影響しない方向）

- tod は gen4 継承の 5 枠のみ（論文は全 13 枠）。枠を増やしても gross の桁が
  2 桁不足している構図は変わらない。
- カバレッジ完備フィルタ = 窓内 survivorship（gen4 と同じ）。net を**過大**
  評価する側なので KILL を弱めない。
- 業種控除は Sector33 の 2025-12 静的スナップショット近似。
- permutation 検定は 5 tod の 119 置換が母集団で、粒度が粗い（最小 p =
  1/120 ≈ 0.008）。

## 6. 帰結

- no-trade band による回転抑制（arXiv:2502.04284, arXiv:1501.03756）は
  「生存後に限り」の条件付きだったため**進まない**。
- gen4 の帰結を維持・強化: 分足・日足「のみ」を入力とする family は、gross の
  源泉が別途示されない限り起案しない（5 連続で gross < friction、うち 2 family
  は信号実在を確認済み — 問題は信号の不在ではなく**振幅 vs 摩擦**）。
- 残る lever は制約の外側: 板 PUSH 特徴とのハイブリッド、日次以上の horizon、
  または maker 執行（スプレッド越えの回避）。
