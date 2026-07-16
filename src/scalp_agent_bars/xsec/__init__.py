"""足読み gen4 — 横断ランキング family (`gen4_xsec_v1`)。

方向予測ではなく「日時ごとの市場・業種控除後リターンの横断順位」を教師にする。
既に死んだ gen2 P3 (17 銘柄の分類確率 argmax) とは教師・母集団・判断頻度が異なる:
- 300+ 銘柄 (point-in-time 流動性選定・月次リバランス)
- 固定判断時刻 5 つのみ (毎分評価しない)
- horizon 15/30/60 分の adjusted リターン順位を直接教師に
- cheap gate は線形 ranker + LightGBM ranker のみ (NN/Transformer はゲート通過後)
"""
