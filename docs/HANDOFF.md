# 次セッションへの引き継ぎ（2026-07-16）

## 現在地

2026-07-16 の設計インタビューで scalp-agent の 12 決定を確定（正本: `DESIGN.md`、
メモリ `scalping-ml-agent-charter` にも同内容）。scaffold 完了・テスト 8/8 通過。

- `src/scalp_agent/` — loader（録画 duckdb → numpy）/ features（OFI・microprice・
  imbalance・時間窓集計）/ labels（N 秒先 mid が spread×mult を超えるかの 3 値）
- `scripts/analyze_scalpability_universe.py` — 実行済み。結果は
  `artifacts/scalpability_h30_m1.5.json`（gitignore 対象なのでリポには入っていない。
  再生成は 3 日分で実行 ~15 分）

## ユニバース分析の結論（3 日データ・暫定）

流動性トップ層（285A / 9984 / 5803 / 1570 / 4062 / 5802 / 5016 / 6857 …）が機会密度で
圧倒的。spread 15bps 超の薄板テール（6834 / 6368 / 9278 / 218A 等）は機会が 2 桁少なく、
「ワイドスプレッド中型株を増やす」仮説は初手棄却。新 50 銘柄は「流動性トップを厚く、
テールを切る」方向で確定してよい。ただし発火率 0.7〜0.8 = 機会は常在、勝負は方向予測精度。

## 次の一手（優先順）

1. **LightGBM 第一世代分類器**: day 単位 walk-forward（07-09/07-13 学習 → 07-14 OOS）。
   features.build_features + labels.make_labels をそのまま使う。ターゲットは
   horizon/mult の格子掃引（5s〜5min × 1.0〜3.0）。
2. **taker 執行シミュレータ**: 保守的 fill（エントリ=対向 best、SOR 価格改善は無視）、
   spread friction、14:55 クローズ。ADR-0001 の G1-G8 と net per entry を計算する
   ゲートスクリプトまで含める。
3. **統合ランタイム移植**: `_bellwether/scripts/kabu_board_paper_trader.py`（凍結・参照のみ）
   の WS 受信/録画コアを `src/scalp_agent/runtime/` に移植し、録画を毎営業日再開して
   学習データを積む（現状 3 日分しかない。これが最大のボトルネック）。
   PUSH 1 コネクション制約により録画と推論は同一プロセス必須（SKILL R8）。

## 落とし穴（再確認用）

- kabu API 作業前に `backcast/.claude/skills/kabusapi/SKILL.md` を必ず読む
  （R5 流量制限 / R6 50 銘柄 / R8 PUSH 単一コネクション / ping_interval=None）
- 口座は `100368`（信用新規抑止）未解除。解除されるまでライブ発注コードは結線しない
- 録画 duckdb の bid_*=買い板 / ask_*=売り板（kabu 生 PUSH の逆命名は録画時に正規化済み）
- 検証は `docs/adr/ADR-0001-evaluation-standard.md`。net PnL 順位付けの罠に注意
- 判定を出したら vault（`C:/Users/sasai/Documents/note/Projects/株価シュミレーション/戦略台帳.md`）へ記録
