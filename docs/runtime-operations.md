# 統合ランタイム運用手順（2026-07-16 構築）

板 PUSH 録画 + ペーパートレード（仮想約定）の単一プロセスランタイム。
実装: `src/scalp_agent/runtime/`。read-only（発注系エンドポイント不使用、
`tests/test_runtime_readonly.py` が grep 監査）。

## 毎朝の起動（DESIGN 決定 11: 完全手動）

1. kabuステーション本体を起動し、ID/パスワード + 第二パスワードでログイン
2. 08:45 頃までに PowerShell で:

   ```powershell
   cd C:\Users\sasai\Documents\_stocktrading
   .\scripts\start_runtime.ps1
   ```

3. 15:35 に自己終了する（rc=0）。場中に 600 秒無受信が続くと rc=1 で終了する
   （token 喪失など。kabu 本体の状態を確認して再起動する）

禁止事項:
- **稼働中に別プロセス/セッションが `POST /token` を発行しない**（既存 token が
  失効し録画が即死する）。token は `S:/jp/stocks_board_kabu_push/current_token.json`
  を読むこと
- 多重起動しない（start_runtime.ps1 がガードする）

## agree_biggap 実弾トレーダーとの共存契約（2026-07-16 owner 確定）

同一マシン・同一 kabu 本体（18080）で `_bellwether/scripts/kabu_agree_biggap_live_trader.py`
（寄りギャップ実弾・09:01 起動・15:30 引成決済）と同居する。契約は 3 点:

1. **token 発行者は本ランタイムただ一人**。agree_biggap は
   `current_token.json` を読み、401 ではファイル再読で追従する。本ランタイム不在
   （heartbeat 60 秒以上無音）のときだけ agree_biggap が solo 発行する（owner 承認済み）。
   したがって**朝は 08:45 本ランタイム → 09:01 agree_biggap の順**が正だが、
   逆順・再起動でも自己修復する
2. **全銘柄一括解除の禁止**（双方）。解除は自分が登録した銘柄の指定解除
   （`PUT /unregister`）のみ。`tests/test_runtime_readonly.py` が grep 監査で固定
3. **register 50 枠 = 板読み 40 / agree_biggap 10**。universe は mid tier 10 銘柄を
   停止して 40（`scripts/board_recorder_universe.txt` ヘッダ参照・owner 承認済み）。
   register 検証は件数でなく「自 universe が RegistList に全員居るか」で行う
   （RegistList は機械全体の全量が返り agree_biggap 分が混ざるため）
4. **登録銘柄の公開**: 本ランタイムは register 成功時に自分の登録銘柄を
   `current_registered.json` へ書き出す。register は機械全体で参照カウントが無く、
   agree_biggap の候補銘柄が本ランタイムの universe と重複した場合、agree_biggap 側の
   スキャン後解除が本ランタイムの PUSH を silent に止めてしまう。agree_biggap は
   このファイルの銘柄を「解除禁止」として扱う（2026-07-16 実 API 統合確認済み:
   7203/9984 重複スキャン後も 40 銘柄無傷・録画継続）

## 構成（kabusapi SKILL R8: PUSH は最新 1 コネクションのみ）

単一 asyncio プロセスに同居:
- **録画**: 全 PUSH → `S:/jp/stocks_board_kabu_push/<date>.duckdb` `board_push`
  （既存規約: 61 列ワイド・bid_*=買い板/ask_*=売り板正規化・500 行/5 秒 flush）。
  heartbeat は同ディレクトリ `heartbeat_kabu_<date>.log`（20 秒毎 TSV）
- **ペーパートレード**: executable PUSH → 逐次特徴量（`runtime/live_features.py`）
  → 1Hz 決定境界で LightGBM 一括推論 → 仮想約定（`runtime/virtual_execution.py`）
- ws は `ping_interval=None` + 1h recv timeout。場中ストールは StallDetector
  （300s recover / 600s exit）。再接続は指数 backoff + jitter、register 完了まで
  推論・新規 entry 停止
- **場中観測 gap**（切断区間が取引時間と重複）: in-flight の pending-entry /
  open-position / pending-exit は直ちに `unresolved_gap` 化し、復帰後の板で
  fill/exit を捏造しない。場外・昼休みのみの切断は gap に数えない
  （DESIGN「統合ランタイムのWS再接続・日内状態」節が正本）
- 接続監査: `artifacts/runtime/<date>/audit.jsonl` に session_id +
  connection_epoch 付きで disconnect/registered/unresolved_gap/startup を記録。
  クラッシュ後の次回起動は trades.jsonl の未終端 entry を `crash_recovered` として
  unresolved 化する（再起動を暗黙の正常決済として扱わない）
- 14:55 強制クローズは「その時刻より厳密に後の最初の PUSH」で約定
  （オフライン凍結規則と同値。housekeeping からの mark close はしない）

## シグナル源（較正専用・owner 確定 2026-07-16）

`artifacts/calibration/shadow_h5_m30/`（gitignore。再生成:
`uv run python scripts/train_calibration_model.py`、07-09+07-13 キャッシュから学習）。

**h5s × m3.0 × τ0.70 は post-selection の刺激生成器であり、採用戦略ではない。**
- 全出力に `calibration_only=true` + policy/model/config version が付く
- PnL・edge・hit・ratio・セル優劣・閾値調整には使用禁止（G8 honest-N 非消費の条件）
- gen1 再採点・07-14 開封・IS-KILL 撤回は禁止
- fill 較正値を採用する将来サイクルは fresh sealed days で評価する

## 出力（`artifacts/runtime/<date>/`）

- `paper.duckdb` `decisions`: **全 eligible 1Hz 決定行**（busy/flat/τ未満も含む）+
  next-PUSH 遷移（選択バイアス診断・owner 要求）
- `trades.jsonl` / summary 内: 仮想約定と quoted-spread fill の乖離
  （slippage_entry/exit_bps・DESIGN 決定 2 の較正データ）、entry_cancelled、
  unresolved（日末未解決は捏造 fill せず較正から除外）
- `summary.json`: 較正統計のみ。**戦略成績としての解釈・判定利用は禁止**

## ライブ⇔オフライン等価性（repo 規律の担保）

決定グリッド・特徴量・仮想約定の 3 層すべてで、逐次実装がオフライン正本
（`sessions.decision_grid` / `features.build_features_normalized` /
`labels.barrier_outcomes_grid` + `execution.simulate_symbol_day`）と一致することを
合成データ + 実録画日 07-13 で回帰テスト済み:
- `tests/test_runtime_live_features.py` / `test_runtime_decision_clock.py` /
  `test_runtime_virtual_execution.py`（合成・常時実行）
- `tests/test_runtime_replay_recorded.py`（S: 必要・recorded_data マーカー）

既知の構造差 1 点: バッチは日末 EXIT_NONE 決定を遡及的に「取引なし」にできるが、
ライブは因果的に保有し続け unresolved になる（テストは unresolved 以前の区間で突合）。
