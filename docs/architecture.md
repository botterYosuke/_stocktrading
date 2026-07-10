# Architecture

このプロジェクトは `backcast` を外部インフラとして参照し、日中の板情報から自動売買戦略を作るためのワークスペースです。

## Constraints

- `C:\Users\sasai\Documents\backcast` は変更しません。
- 戦略はデイトレードに限定します。翌営業日へポジションを持ち越す前提の処理は gold layer 以降に入れません。
- 入力元は `S:\jp\stocks_board_kabu_push` の板情報です。

## Layers

### Bronze

入力ファイルを原本に近い形で保存します。監査性を優先し、この層ではできるだけ解釈を加えません。

### Silver

板イベントを正規化します。主な想定カラムは `ts_event`, `symbol`, `bid_price`, `bid_size`, `ask_price`, `ask_size`, `trade_price`, `trade_size` です。

### Gold

戦略が読む特徴量を保存します。例はスプレッド、板厚、短期インバランス、約定方向、セッション内累積出来高、シグナル、バックテスト結果です。
