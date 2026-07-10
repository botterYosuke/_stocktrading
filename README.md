# stocktrading

`C:\Users\sasai\Documents\backcast` をインフラとして利用し、このフォルダで株の自動売買戦略を開発するための作業プロジェクトです。

## Scope

- デイトレード戦略に限定します。
- 板情報は `S:\jp\stocks_board_kabu_push` を入力元にします。
- データ処理は medallion architecture で分けます。
- `backcast` 側のファイルは変更しません。

## Medallion Layers

- `data/bronze`: 入力ファイルの原本性を保った取り込み結果。
- `data/silver`: 時刻、銘柄、気配、約定などを正規化した分析用データ。
- `data/gold`: デイトレード戦略が直接読む特徴量、シグナル、評価結果。

## Quick Start

```powershell
uv sync
Copy-Item .env.example .env
uv run pytest
```

`S:` ドライブが見えない環境では、`.env` の `BOARD_SOURCE_ROOT` を UNC パスなど実際に読める場所に変更してください。

## Commands

```powershell
uv run python -m stocktrading.cli doctor
# Bronze: 確定した日次 DuckDB のみを parquet 化（当日/.wal のライブ DB は除外）
uv run python -m stocktrading.cli ingest-bronze --limit 1
# Silver: 正規化（mid / spread / imbalance）
uv run python -m stocktrading.cli build-silver --date 2026-07-09
# 検証: 板バックテスト（反対側ベスト約定＋手数料＋引け強制フラット）
uv run python -m stocktrading.cli backtest --symbol 9984 --date 2026-07-09
```

## Hypothesis → Implement → Validate loop

板デイトレ戦略の検証は **この Python 側**が権威です（`backtest.py`）。約定は反対側ベストを
舐め（buy@ask / sell@bid）、手数料 bps を課し、引けで強制フラット（持ち越し無し）。シグナル
ロジック（`signals.py`）は純粋関数で、将来 backcast の marimo cell（live 実行）と共有します。
`backcast` のリプレイはバー足・ゼロコスト約定・成行のみで板を扱えないため、検証には使いません。
