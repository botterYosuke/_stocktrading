# scalp-agent Claude Guide

kabusapi 板 PUSH スキャルピング ML-Agent。設計の正本は `DESIGN.md`（12 決定 + 既定値）。
kabu API を触る作業は必ず先に `C:/Users/sasai/Documents/backcast/.claude/skills/kabusapi/SKILL.md` を読む。

## 外部の司令室 Vault（存在すれば着手前に見る）

`C:/Users/sasai/Documents/note`（Obsidian vault）が戦略リポ横断の司令室。
非自明な作業の前に `Projects/株価シュミレーション/戦略台帳.md`（~372 判定の横断索引）で
「その戦略は他リポで既に死んでいないか」を確認する。歴史的研究成果の正本は
`_bellwether/boardbook`（本リポの前身の移植先）。

## Project Facts

- Source は `src/scalp_agent`、tests は `tests/`。Python 3.13 + uv。
- 録画データ: `S:/jp/stocks_board_kabu_push/<date>.duckdb` の `board_push` テーブル
  （ワイド形式・1 PUSH msg = 1 行・bid_*=買い板/ask_*=売り板に正規化済み）。
- 録画・ペーパートレードの実証済み参考実装: `_bellwether/scripts/kabu_board_paper_trader.py`
  （read-only。本リポへは設計を移植し、_bellwether 側は凍結）。
- PUSH は最新 1 コネクションのみ配信（SKILL R8）。録画とエージェントを別プロセスにしない。

## Development Rules

- `uv run pytest` が通るまで実装完了を主張しない。
- 特徴量・ラベル・シグナル判定は pure（I/O なし・状態は引数と戻り値）。オフライン学習と
  ライブランタイムが同じ関数を共有する。
- シミュレーションは保守的に。fill が不確実な箇所は戦略に不利な側へ倒す。
- セッション境界でポジション・状態・時計を全リセット。overnight を跨ぐ状態は作らない。
- 発注系コードはライブモードフラグの背後に置き、`100368` 解除が確認されるまで
  ペーパー経路のみ結線する。
- 評価は `docs/adr/ADR-0001-evaluation-standard.md` に従う（net per entry・G1-G8・friction 比 >= 3）。
  net PnL 順位付けは「取引しないこと」に報酬を与える罠に注意。
- 判定（PASS/KILL/改良継続）を出したら vault の戦略台帳へ記録する。
