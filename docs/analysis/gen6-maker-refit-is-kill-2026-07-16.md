# 足読み gen6 — maker 執行ルール再検証 family (`gen6_maker_refit_v1`) IS-KILL

- Date: 2026-07-16
- Verdict: **IS-KILL**（val 全 8 執行セルが候補条件未達。gross 自体が全セル負。
  OOS 2025-04 は封印のまま未開封）
- config_hash: `38fcaf49c3b2c4e08a9d66ad3562a666e61d759b5c6c000439e7ee000b0a0f66`
- Code: `src/scalp_agent_bars/xsec/maker.py` + `scripts/gen6_maker_pipeline.py`
- Artifacts: `artifacts/gen6_maker/`（maker_isval.parquet / sweep_val_results.json /
  frozen_config.json）
- 台帳: 379 件目。憲章修正（maker 執行の検証対象化）は別行 380 件目。

## 1. 仮説（owner 承認・2026-07-16 合意の maker fill ルール）

gen4・gen5 の死因は「信号実在・振幅不足」（gross +1.5〜2.5bps << taker friction
14bps）だった。**問題が信号の不在ではなく振幅 vs taker 摩擦なら、執行を maker 化
して摩擦の幾何を変えれば救えるかもしれない** — これが本 family の起案根拠。

owner 案「次 bar の高値-安値の範囲内なら指値約定」に対し 4 修正付きで採用:

1. **touch ≠ fill — 1 tick 突き抜けを要求**（買い指値は bar low ≤ 指値 − 1 tick。
   価格優先の板で不利な価格の約定が発生したなら自分の指値は必ず先に食われている）。
2. **約定価格 = 指値そのもの**。同一 bar 内の値動きで exit を評価しない
   （分足は bar 内順序を失っている）。exit 評価は約定 bar の次以降。
3. **exit 規約固定**: (a) entry maker / exit horizon taker、(b) 両側 maker +
   horizon で taker フォールバック。exit は decision+30 分 bar open に固定。
4. **注文単位の会計**: fill 率（tod 別・side 別）と未約定注文のカウンター
   ファクチュアル taker gross を診断必須出力。

追加の保守化（owner 未異論・採用）: 発注が最初に生きる bar の penetration は
カウントしない（レイテンシ対策）。取消時刻を跨ぐ bar も不算入。

**着手前の台帳形式確認**: maker 執行系の先行判定は 3 件存在し全て逆選択で KILL
（winner-factory #77: 未約定反実仮想 +200bps / 旧 _stocktrading maker family
0/50 / ギャップ安リテスト maker 対照 −2.68bps）。ただしいずれも別シグナル・
別ハーネスであり、「実在信号 × 分足 maker シム」は未判定 — owner が実行を承認。

## 2. 凍結構成

- **シグナルは gen5 の val 最良セル (hl5, K10) に凍結**（gross +1.54bps・
  gap_z +2.54・decile spearman 0.96）。本 family の探索対象は執行ルールのみ。
- gen4/gen5 から継承: 月次 PIT ユニバース 400 銘柄 / 判断時刻 5 枠 / 分割
  train 2024-04-01〜11-29・val 2024-12-02〜2025-03-31 (78 日)・OOS 2025-04
  sealed / 呼値ラダー保守 friction。
- **G8 格子（grill-me で owner が確定・凍結）**: 指値深さ {join=直前 close,
  m1=1 tick 深い} × resting window {5 分, 30 分} × exit 構成 {a, b} = **8 セル**。
- データ: 判断時刻後の bar 別 low/high/open 系列で fill を再現するため分足を
  再走査（`maker_isval.parquet`、485,949 行、gen4 dataset と 100% join）。
- friction 分解: taker 片側 = スプレッドモデル × 1.25 / 2、maker 側 = 0、
  flat 1bp（往復金利等）。(b) で exit maker 成立なら明示摩擦は flat のみ。
- **G6 代替（val を見る前に凍結）**: 両側 maker では ratio ≥ 3 が自明に通るため、
  **signal-free maker 対照**（同日・同時刻・同 K・同深さ・ランダム銘柄の maker
  ブック、200 shuffles）との gap_z ≥ 2 を候補条件に据える（G2 拡張）。
  スプレッド収入も逆選択も対照に等しく入るので、実測 − 対照 = 信号の増分価値。
  構成 (a) は従来どおり ratio ≥ 3 も併用。

## 3. 結果（val 78 日・ADR-0001 採点）

全 8 セル。**gross 自体が全セル負** — friction を引く前に既に死んでいる:

| セル | n | fill率 | net/entry | gross/entry | friction | t_net | 対照 net | gap_z |
|---|---|---|---|---|---|---|---|---|
| join w5 (a) | 4,193 | 53.8% | −18.69 | −12.02 | 6.67 | −15.5 | −16.78 | −2.5 |
| join w5 (b) | 4,193 | 53.8% | −24.60 | −22.54 | 2.05 | −35.7 | −20.63 | −8.9 |
| join w30 (a) | 6,011 | 77.1% | −18.68 | −11.62 | 7.06 | −19.6 | −17.44 | −2.3 |
| join w30 (b) | 6,011 | 77.1% | −24.06 | −21.53 | 2.53 | −43.2 | −20.77 | −9.4 |
| m1 w5 (a) | 2,797 | 35.9% | −15.52 | −9.50 | 6.02 | −10.2 | −14.05 | −1.3 |
| m1 w5 (b) | 2,797 | 35.9% | −21.07 | −18.65 | 2.42 | −21.7 | −16.96 | −6.0 |
| m1 w30 (a) | 4,907 | 62.9% | −16.62 | −9.94 | 6.68 | −16.4 | −15.34 | −1.9 |
| m1 w30 (b) | 4,907 | 62.9% | −20.80 | −17.42 | 3.38 | −30.1 | −17.40 | −7.8 |

最大 n セルの詳細（join|w30|b）:

```
(G, S) = gen5 tod_lag hl5 K10 × maker 両側 (join, rest 30 分)
  発注    : 7,800 注文 / fill 6,011 (77.1%)   D=78/78 日
  主指標  : net/entry=−24.06bps  gross=−21.53bps  friction=2.53bps
            t_net=−43.2  hit=7.3%
  対照    : signal-free maker 対照 = −20.77bps (fill 72.2%)
            → GAP=−3.29bps (z=−9.4)。信号はランダム銘柄より悪化させる [G2拡張 FAIL]
  会計    : 未約定 1,789 注文の反実仮想 taker gross = +44.58bps
            約定 6,011 注文の反実仮想 taker gross = −11.28bps
            （加重平均 ≈ +1.5bps = gen5 の taker gross と整合）
  exit    : maker exit 成立 78.6% / taker fallback 21.4%
  集中度  : 最大日 2.1% / 最大銘柄 2.4%                            [G3 通過]
  左裾    : worst=−537bps  p1=−165bps  MAE中央値=38bps
  stress  : −28.5bps                                                [G6 FAIL]
  IS/OOS  : IS で全滅 — OOS 未開封                                  [G5 n/a]
  VERDICT : IS-KILL (G2拡張, G6)
```

## 4. 死因 — 逆選択が信号もスプレッド収入も丸ごと食う

1. **fill の条件付けが致命的**。指値が 1 tick 突き抜けられた時 = 価格が自分に
   不利な方向へ動いた時しか約定しない。約定群の反実仮想 taker gross は
   −11.3bps（未約定群は +44.6bps）— **勝ち注文だけが約定しない**。
   winner-factory #77（未約定反実仮想 +200bps）と同型の死因を、実在信号でも確認。
2. **スプレッド収入では全く賄えない**。(b) の明示摩擦は 2.5bps まで下がり
   exit maker も 79% 成立するが、gross −21.5bps がそれ以前の問題。
3. **signal-free 対照も −14〜−21bps** → 分足 maker ブック自体が有毒。
   「市場メイク family」の起案根拠もない（対照 net > 0 のセルは皆無）。
4. **gap_z が全セル負** — gen5 のモメンタム系信号は maker 執行と逆相性:
   買いサイドの top-K は上昇継続銘柄であり、その指値が食われるのは
   モメンタムが反転した時。信号が強いほど逆選択が濃くなる。
5. 深さを 1 tick 深くしても（m1）、resting を 5 分に絞っても、exit を taker に
   固定しても（a）符号は変わらない。执行格子のどこにも生存点がない。

## 5. 導出判定（grill-me で owner が承認した規約に基づく）

- **gen1b/gen2/gen3 の maker 化は個別再実行なしで KILL 維持**。これらは taker
  gross ≈ 0（信号の振幅自体が無い）で、maker 化で加わる成分はスプレッド収入と
  逆選択 = signal-free 対照そのもの。対照は全 8 セルで net −14〜−21bps と
  決定的に負 → 3 family とも救済不能。
- **gen4 ranker の個別再実行も行わない**。gen5 より強い（gap_z +2.54）信号ですら
  maker 化でランダム銘柄より悪化した（gap_z 全セル負）。gen4 の taker gross
  +2.5bps が対照の −17bps を埋める可能性はない。
- 分足系はこれで **6 family 連続 KILL**（gen1b/gen2/gen3/gen4/gen5/gen6）。
  gen4/gen5 で「振幅不足なら執行で救う」筋を今回閉じたため、**JQuants 分足 ×
  横断ランキング × maker/taker 執行の枠組みでは新 family の起案根拠がない**。

## 6. 既知の限界（判定を覆さない側の注記）

- 分足シムはキュー位置を持たない。ただし penetration 1 tick 要求は「キューの
  最後尾でも約定している」ことの十分条件であり、楽観側ではない。
- 指値参照価格は直前 bar close（bid の近似）。実運用の bid join より
  スプレッド収入を過小評価しうるが、gross −21bps に対し高々数 bps の話。
- gen4 継承の survivorship / 静的業種は今回も同じ（net を過大評価する側 =
  KILL を強める側ではない点は gen4 文書と同じ注記）。

## 7. 憲章修正の記録

12 決定（taker 前提）に対し「**maker 執行を検証対象の執行ルールとして追加**」を
owner が 2026-07-16 に正式承認（grill-me 質問 6）。本検証はその初適用であり、
結果は上記のとおり KILL。**憲章の既定執行は taker のまま変更なし** — maker 経路は
「検証済み・死亡確認」として台帳 380 件目に記録する。

## 8. 帰結

- 分足/日足のみを入力とするスキャルピング戦略は、taker（gen1b〜gen5）と
  maker（gen6）の両執行で死亡が確定した。**gross の源泉が分足に無い/薄い**という
  gen3 の結論は執行ルールに依存しない。
- 生き残っている gross の源泉は板 PUSH 系（ITAYOMI: capture 0.4〜1.0bps/30s は
  実在、taker RT 2bps に届かず CLOSED だが、これは録画データの蓄積で
  fill 較正・機会選別の改善余地が残る唯一の系統）。
- 較正 shadow ランタイム（稼働中）の板 PUSH 録画が、次の起案の唯一の弾。
