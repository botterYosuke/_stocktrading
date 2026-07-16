# 足読み gen4 — 横断ランキング family (`gen4_xsec_v1`) IS-KILL

- Date: 2026-07-16
- Verdict: **IS-KILL**（val 全 12 構成が候補条件未達。OOS 2025-04 は封印のまま未開封）
- config_hash: `516090c5edb43c3d9b97cf3ef23af4cffce373fa26e979df09675438a2bec397`
- Code: `src/scalp_agent_bars/xsec/` + `scripts/gen4_xsec_pipeline.py`
- Artifacts: `artifacts/gen4_xsec/`（universe / dataset / sweep_val_results.json / frozen_config.json）

## 1. 仮説（owner 指示・arXiv サーベイ由来）

「方向予測ではなく、予測可能な銘柄の選択＋横断ランキング」:
300+ 銘柄・固定判断時刻のみ・15/30/60 分先の市場・業種控除後リターンの
**横断順位**を直接教師にする。既に死んだ gen2 P3（17 銘柄の分類確率 argmax）とは
教師・母集団・判断頻度が異なる新 family。cheap gate は線形 + LightGBM ranker のみ。

## 2. 凍結構成

- ユニバース: 月次 point-in-time。前月末 trailing 60 営業日 median TurnoverValue
  上位 400（プライム・median close ≥ 200 円・分足カバレッジあり）。毎月ちょうど 400。
- 判断時刻 09:30 / 10:00 / 10:30 / 13:00 / 14:00。horizon 15 / 30 / 60 分。
- entry / exit = 判断直後バー・horizon バーの始値 taker。
- 教師 = (day, tod) 内の市場・業種(Sector33)控除後 forward リターンの横断百分位。
- friction = 呼値一般ラダー保守モデル ×1.25 + 1bp（median 14.0bps、p10 5.9 / p90 21.6）。
- 分割（データ到達性から評価前に確定 — stocks_minute で 300+ 銘柄が揃うのは
  2024-04-01〜2025-04-30 のみ。3,647 銘柄フルカバー）:
  train 2024-04-01〜2024-11-29 / val 2024-12-02〜2025-03-31 (78 日) /
  OOS 2025-04-01〜04-30 sealed。
- データ: 485,949 行・244 日・1,220 グループ（グループ中央値 399 銘柄）。

## 3. 結果（val 78 日・ADR-0001 採点）

全 12 構成（{linear, lgbm_rank} × {h15,h30,h60} × {K5,K10}）:

| 構成 | n | net/entry | gross/entry | ratio | t_net |
|---|---|---|---|---|---|
| 最良 gross: lgbm h15 K5 | 3,900 | **−10.94bps** | **+2.46bps** | 0.18 | −12.1 |
| 最良 linear: h15 K10 | 7,800 | −13.82 | +0.19 | 0.01 | −17.6 |
| 他 10 構成 | — | −11.9〜−15.4 | −1.35〜+1.51 | ≤0.11 | 全て強負 |

最良構成の詳細（lgbm_rank|h15|K5）:

```
発火    : D=78/78 日 (100%)   n=3,900 (50/日)
主指標  : net/entry=−10.94bps  ratio=0.18  t_net=−12.1  hit=30.3%
ヌル    : ランダム同数銘柄 = −13.43bps → GAP=+2.50bps (z=+5.05)   [G2 通過]
集中度  : 最大日 4.6% / 最大銘柄 4.5% / 最大月 27.8%              [G3 通過]
deciles : bottom −0.18 … top +0.59bps (adj、spearman 0.60)
左裾    : worst −650bps   stress(4tick+10bps) net=−47.2bps
IS/OOS  : IS-KILL — OOS 封印のまま未開封                           [G5]
VERDICT : KILL（G6: ratio 0.18 << 3.0、net < 0）
```

## 4. 解釈 — 「選定器は機能するが、グロスが摩擦の 1/20」

- **ランキング信号は統計的に実在する**: ランダム銘柄ヌルとの gap +2.5bps (z=5.0)、
  decile 単調性 0.6〜0.77。ranker は「相対的に上がる銘柄」を確かに選べている。
- **しかし経済的に無価値**: 横断 top decile の控除後リターンは 15 分で **+0.59bps**。
  top-K ブックの gross +2.46bps は、最も楽観的な friction（メガキャップ板実測
  1.5〜5.3bps）と比べても ratio < 1。ratio ≥ 3 には friction ≤ 0.82bps が必要で、
  これは日本株最タイトのスプレッドより小さい。**KILL は保守的 friction モデルの
  産物ではない**。
- gen2（17 銘柄・確率 argmax）→ gen4（400 銘柄・順位教師・固定時刻）と定式化を
  変えても結論は不変。gen1b/gen2/gen3 と合わせ、**「現代日本株の分足 OHLCV には
  15〜60 分 horizon で摩擦を超えるシグナルが無い」**が 4 family・3 学習器種で確認。

## 5. 既知の限界（結論には影響しない方向）

- カバレッジ完備フィルタ（2024-04〜2025-04 を持つ銘柄のみ）= 窓内 survivorship。
  net を**過大**評価する側なので KILL を弱めない。
- Sector33 は 2025-12 静的スナップショット近似（控除のみに使用）。
- OOS が 1 ヶ月と薄いが、未開封のまま KILL のため影響なし。

## 6. 帰結

- MASTER 型 mixing / TRA 型レジーム分離 / DoubleAdapt 型逐次適応へは**進まない**
  （owner 指示: cheap gate 通過が前提条件）。
- 分足・日足「のみ」を入力とする ITAYOMI 外の family は、以後 gross の源泉が
  別途示されない限り起案しない（本 KILL を含め 4 連続で gross < friction）。
- 残る lever は制約の外側: 板 PUSH 特徴とのハイブリッド、日次以上の horizon、
  または maker 執行（スプレッド越えの回避）。
