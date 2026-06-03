# 機械学習による日本株シグナル予測器（頭脳）

> [!CAUTION]
> - 本プログラムを使用することによって被った損害等について、制作者は一切の責任を負いません。投資はご自身の判断と責任のもとで行ってください。
> - 本プログラムから得られた投資判断は、特定銘柄の取引を推奨するものではありません。
> - 本プログラムの使用にあたっては、コードを慎重にレビューした上で、その動作原理を熟知しておくことを推奨します。

このリポジトリは **ML 予測の「頭脳」** です。日足データから point-in-time で深層学習モデルを学習し、翌営業日の売買シグナルを **JSON 契約**として出力します。

**取引の実行（建玉・ロスカット・発注ガード・kabu/立花 API 連携）は本リポジトリにはありません。** 実行は別リポジトリ [The-Trader-Was-Replaced（TTWR）](https://github.com/botterYosuke/The-Trader-Was-Replaced) が担い、両者は `signals/` の日次 JSON ＋ `manifest.json` を境界契約として疎結合に連携します（Replay／Live 共通）。

```
_stocktrading（本リポジトリ）: CSV.gz(日足) -> point-in-time LSTM(UP/DOWN) -> signals_YYYY-MM-DD.json + manifest.json
TTWR                         : manifest/instruments_ref で購読 -> 日次 signals を正本に 寄り建て/引け返済/ロスカット/発注ガード
```

# 動作環境

- **Python 3.12**（TensorFlow が 3.13 以降に未対応のため必須。3.13 では動作しません）
- **J-Quants の日足 CSV.gz キャッシュ**：環境変数 `DEV_J_QUANTS_CACHE` が指すディレクトリの `equities_bars_daily_*.csv.gz`（TTWR と共有する素データ）。本リポジトリは stdlib で直読し、nautilus には依存しません。
- **TTWR submodule**（`vendor/The-Trader-Was-Replaced`）：データ系譜・契約・listed-symbols の参照用。頭脳は `engine` を import しません。

# セットアップ

```
$ python3.12 -m venv env
$ source ./env/bin/activate
(env) $ make install
$ git submodule update --init   # vendor/The-Trader-Was-Replaced
```

プログラム直下に `.env` を置きます（`config_manager` が読み込みます）。

```:.env
BaseDir=/path/to/_stocktrading
DEV_J_QUANTS_CACHE=/path/to/jquants-cache
```

- `BaseDir`：本リポジトリの絶対パス（`config.yaml` の解決に使用）
- `DEV_J_QUANTS_CACHE`：J-Quants 日足 CSV.gz の格納ディレクトリ

# 使い方

## 単日（当日 as_of → 翌営業日のシグナル）

```
(env) $ make predict
```

`signals/signals_<翌営業日>.json` と `signals/manifest.json` を生成します。

## 日付レンジの point-in-time 生成（再開可能・モデルキャッシュ付き）

```
(env) $ make generate START=2021-06-04 END=2021-06-08
# = python daily_generator.py --start 2021-06-04 --end 2021-06-08 --out signals
```

- 各営業日 `target_date` ごとに `as_of = 前営業日` で学習・予測し `signals_<target_date>.json` を出力。
- モデルは `models/<key>/{up,down}.keras + meta.json` にキャッシュ（同一 `as_of`／窓／ユニバース／パラメータなら再学習せず再利用）。
- 既に valid な `signals_<date>.json` があれば skip（`--force` で再生成）。
- 最後に `manifest.json`（`files` 日付昇順、`instruments` は全 signals の和集合）を集約。
- `--dry-run` で `target_date <- as_of [generate/skip]` の計画のみ表示。

# 出力契約（境界の正本）

`signals/signals_YYYY-MM-DD.json`：

```json
{
  "schema_version": 1,
  "target_date": "2026-06-04",
  "as_of": "2026-06-03",
  "signals": [
    {"symbol": "7203.TSE", "side": "LONG",  "confidence": 0.83, "code": "7203"},
    {"symbol": "6758.TSE", "side": "SHORT", "confidence": 0.79, "code": "6758"}
  ],
  "regulation_filter": {"brain": "disabled", "replay": "not_available", "live": "pre_trade_check"}
}
```

- `side`：`LONG`（買い）／`SHORT`（売り）。`confidence ∈ (0, 1]`。`symbol = {code}.TSE`。
- 信用規制チェックは頭脳では行いません（`regulation_filter.brain = disabled`）。Live の発注直前で TTWR が弾きます。
- 契約の詳細は TTWR の Issue `botterYosuke/The-Trader-Was-Replaced#119` を参照。

# 投資判断アルゴリズムの概要

下記の 2 種類の深層学習モデル（Bidirectional LSTM）を使います。

- 翌営業日の終値が当日終値から一定割合（既定 +0.5%）超**上がる**か否かを予測する **UP モデル**
- 同じく一定割合（既定 -0.5%）超**下がる**か否かを予測する **DOWN モデル**

`as_of` 以前の直近 `train_window_business_days`（既定 80 営業日）だけで学習し（lookahead 排除）、特徴量（移動平均・MACD・ボリンジャーバンド・RSI とそれらの乖離率など）を入力に、未知の直近系列に対して 0〜1 の確信度を付与します。

1. 全銘柄に「値上がり度」（UP の確信度）を付与
2. 全銘柄に「値下がり度」（DOWN の確信度）を付与
3. しきい値（既定 0.7）を上回る銘柄を「値上がり／値下がり銘柄」として特定（両方に入る場合は高い側を採用）
4. 価格帯フィルタ（700 < 終値 < 6000）で不適切な銘柄を除外
5. 確信度に比例した確率的サンプリングで最大 50 銘柄を抽出し、`signals` として出力

抽出した銘柄を実際にどう建て・返済し、どうロスカットするか（許容リスク配分・寄り建て／引け返済など）は **TTWR 側の戦略**が担います。

# ライセンス

[LICENSE](LICENSE) を参照してください。
