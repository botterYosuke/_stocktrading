# 板読み nightly retrain 運用手順（DESIGN 決定 12 の OPS 足場・2026-07-17 構築）

毎営業日の引け後に point-in-time 再学習を行い、technical gate を通ったモデルだけを
champion として昇格する。**ランタイムの既定動作は一切変わらない**（daily モデルは
env `SCALP_DAILY_MODEL=1` の opt-in。未設定なら従来どおり frozen shadow_h5_m30）。

## 2 層構成（owner 凍結 2026-07-16）

| 層 | モデル | 役割 |
|----|--------|------|
| 1 | **daily champion**（`artifacts/calibration/daily/<YYYYMMDD>/` + `champion.json`） | 毎営業日 as-of 再学習。将来のライブ用。**レシピ（特徴量・LGBM パラメータ・セル h5s×m3.0×τ0.70）は凍結し、学習データ窓だけがスライドする** |
| 2 | **frozen shadow_h5_m30**（git コミット済み・07-09+07-13 固定） | fill 較正専用。nightly retrain で触れない・置換されない |

どちらの層の paper 出力も較正データ扱い（`calibration_only=true`）。PnL・edge・hit・
ratio によるセル選択・台帳採点への利用は引き続き禁止（G8 honest-N 非消費の条件）。

## 日次フロー（`scripts/nightly_retrain.py`）

```
recording check → as-of retrain → technical gate → promote / champion 継続
```

1. **RECORDING CHECK**（day T = 今日 or `--date`）
   - `S:/jp/stocks_board_kabu_push/<T>.duckdb` が存在（`BOARD_PUSH_DIR` env 尊重）
   - 行数 ≥ 500,000（完全営業日の実績は 1.1M〜3.0M 行）
   - distinct codes が `scripts/board_recorder_universe.txt` の 90% 以上をカバー
   - `heartbeat_kabu_<T>.log` 最終行の時刻 ≥ 15:25（大引けまで録画した証拠）
   - どれか NG → 理由を表示して **EXIT 3**（何も書かない・前日 champion 継続）
2. **AS-OF RETRAIN**
   - 学習窓 = 「T で終わる直近 2 日」（2 = 凍結レシピ `CAL_TRAIN_DAYS` と同数）。
     候補は `loader.available_days()` の実録画日のうち、**OOS 封印日（07-14）を除き**、
     行数床を満たし読める日のみ。
   - T 自身が OOS 封印日、または窓が組めない → **EXIT 3**（封印ガードは弱めない。
     `assert_days_role` の許可集合は凍結日付固定のため、スライド窓には
     `daily_model.assert_daily_train_days`＝「OOS に触れない」で同じ意味論を守る）
   - レシピは `train_calibration_model.py` と同じ凍結定数
     （`LGBM_PARAMS`／`CAL_HORIZON_S`／`CAL_MULT`／`training_arrays`）を import して共有。
     frozen shadow の学習経路・成果物には触れない。
3. **OUTPUT + TECHNICAL GATE**
   - challenger を `artifacts/calibration/daily/<YYYYMMDD>/{model.txt, meta.json}` へ書く。
     meta は既存スキーマ + `train_days`（スライド窓）+ `data_hashes` + `git_sha`。
   - gate: model.txt が lightgbm で再ロードできる／meta の
     `feature_schema_hash`・`config_hash` が現行コードと一致／サンプル予測が非退化
     （NaN なし・全行定数でない）
4. **PROMOTE / 継続**
   - gate 合格 → `artifacts/calibration/champion.json` を原子的更新
     （`{model_dir, model_sha256, promoted_at, train_days, previous}`）→ **EXIT 0**
   - gate 不合格 → champion pointer は触らず **EXIT 3**
   - 旧 daily モデルは新しい順に 10 世代保持（champion / previous の指す先は保護）

## exit codes

| code | 意味 | 呼び出し側の扱い |
|------|------|------------------|
| 0 | challenger 昇格 | OK |
| 3 | champion 継続（録画不足・封印日・窓不成立・gate 不合格など回復可能な全条件） | WARN・非致命 |
| 1 | ハードエラー（予期しない例外） | ERROR |

## 毎夕の起動（_bellwether daily_ops）

`_bellwether/scripts/daily_ops/daily_evening_pipeline.ps1`（平日 16:40 Task Scheduler）
の **STEP 4 board_retrain** が `<KABU_STOCKTRADING_REPO>\scripts\nightly_retrain.ps1` を
引数なしで呼ぶ（`KABU_STOCKTRADING_REPO` は backcast/.env 経由）。exit 3 は
「録画不足 — 前日 champion 継続」の WARN として非致命扱い、exit 3 以外の非 0 は
パイプライン overall=ERROR になる。板 PUSH 録画ベースのため STEP 1（日足更新）とは独立。

手動実行:

```powershell
cd C:\Users\sasai\Documents\_stocktrading
.\scripts\nightly_retrain.ps1                 # day T = 今日
.\scripts\nightly_retrain.ps1 -Date 2026-07-16  # 日付指定
```

注意: 場中（ランタイム稼働中）は当日の duckdb が writer lock で読めないため
recording check が EXIT 3 になる。**引け後（15:35 のランタイム自己終了後）に走らせる**こと。

## ランタイム opt-in（`SCALP_DAILY_MODEL=1`）

- 未設定（既定）: 従来と bit-identical。frozen shadow_h5_m30 をロードする。
- `SCALP_DAILY_MODEL=1`: `champion.json` を解決して daily champion をロード
  （sha256・meta の horizon/mult/tau/config_hash/feature_schema_hash を全数検査）。
  **pointer 欠落・hash 不一致・meta 不一致などあらゆる失敗で frozen shadow へ
  フォールバック**（警告ログ）。08:45 の起動をモデル起因で殺さない。

```powershell
$env:SCALP_DAILY_MODEL = '1'
.\scripts\start_runtime.ps1
```

実装: `src/scalp_agent/runtime/daily_model.py`（runner.py は
`load_runtime_model()` を呼ぶだけ）。

**DESIGN 決定 11 は不変**: ランタイム起動は毎朝の手動 kabu 本体ログイン→
`start_runtime.ps1` のまま。nightly retrain は夕方のモデル準備だけを自動化する。

## universe の提案 / 適用（`scripts/build_board_universe.py`）

`board_recorder_universe.txt` ヘッダの規則（liquid = 直近 20 営業日の
median(Close×Volume) 上位 40・must-include 7203・mid tier は共存契約で停止）を再移植。

```powershell
uv run python scripts/build_board_universe.py            # 提案のみ（_next.txt + diff 表示）
uv run python scripts/build_board_universe.py --apply    # 明示適用（原子的置換）
```

- 既定は **提案モード**: `scripts/board_recorder_universe_next.txt`（gitignore）へ書き、
  現行との added/removed を表示するだけ。
- **nightly job からは呼ばない（入替は手動判断）**。理由:
  1. **register 継続性** — universe はランタイムの PUSH register と
     `current_registered.json`（agree_biggap が「解除禁止」として参照する契約ファイル）の
     正本。無断入替は録画カバレッジの断絶と共存契約の破壊を招く。
  2. **50 枠共存契約** — register 50 枠 = 板読み 40 / agree_biggap 10 の配分は
     owner 判断事項（2026-07-16 承認）。mid tier 復活（`--include-mid`）も共存解消時のみ。
- 適用した場合、反映されるのは翌朝のランタイム起動時（register は起動時に行う）。

## git 管理

- `artifacts/calibration/daily/*` と `champion.json` は **gitignore**（機械ローカル。
  pointer はローカル daily/ を指すため、コミットしても他機で解決できない）。
- frozen shadow_h5_m30 の再包含例外（他機起動用にコミット）は従来どおり維持。

## 実装状態（2026-07-17）

- `scripts/nightly_retrain.py` / `scripts/nightly_retrain.ps1` — 本書のフロー実装済み。
- `src/scalp_agent/runtime/daily_model.py` — champion 解決 + フォールバックローダ。
- `scripts/build_board_universe.py` — 提案/適用モードで再移植。
- DESIGN.md は変更していない（決定 12 の本文が正本。本書は OPS 状態の記録）。
