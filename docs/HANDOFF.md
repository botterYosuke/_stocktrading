# 次セッションへの引き継ぎ（2026-07-16 夜更新）

## 現在地

**統合ランタイム構築完了**（owner 指示 2026-07-16 夕 → 同日実装・77 テスト全通過）。
板 PUSH を録画しながらペーパートレード（自前の仮想約定エンジン・read-only）を回す
単一プロセスを `src/scalp_agent/runtime/` に実装。運用手順の正本は
**`docs/runtime-operations.md`**、設計確定事項は **`DESIGN.md`** の
「gen1 IS-KILL後のペーパー較正」「統合ランタイムのWS再接続・日内状態」節。明朝から毎営業日:

```powershell
cd C:\Users\sasai\Documents\_stocktrading
.\scripts\start_runtime.ps1        # kabu 本体ログイン後・08:45 目標・15:35 自己終了
```

gen1（LightGBM × トリプルバリア taker）は **IS-KILL** で確定済み
（`docs/analysis/gen1-lgbm-triplebarrier-is-kill-2026-07-16.md`・台帳 373 行目）。
**07-14 は未開封のまま封印維持**（ランタイムのシグナル源にも一切使っていない）。

## このセッション（夜）でやったこと

1. グリルで未決 3 点を owner と確定（DESIGN.md へ反映済み）:
   シグナル源 = 較正専用暫定モデル（詳細下記）/ 18081 発注ミラー = 併走しない /
   再接続 = 1h recv timeout + StallDetector + 場中 gap の unresolved 化
2. `_bellwether/scripts/kabu_board_paper_trader.py`（凍結）から録画・WS・ストール検知を
   移植（`runtime/boards.py, recorder.py, rest.py, stall.py, runner.py`）。
   特徴量・1Hz 決定グリッド・仮想約定は**オフライン正本と同値の逐次実装**を新規作成
   （`runtime/live_features.py, paper_engine.py, virtual_execution.py, trader.py, replay.py`）
3. **等価性の回帰テスト**: 決定グリッド = `sessions.decision_grid`、特徴量 =
   `build_features_normalized`、仮想約定 = `barrier_outcomes_grid` +
   `simulate_symbol_day` と、合成データ + 実録画 07-13 で突合し一致
   （`tests/test_runtime_*.py`・26+4 本追加、`uv run pytest` 77 本全通過）
4. 較正専用モデルを学習・保存（`artifacts/calibration/shadow_h5_m30/`・gitignore・
   再生成 `uv run python scripts/train_calibration_model.py`・n=1.40M 行）。
   replay スモーク（07-13 の 3 銘柄）: 19 取引・51,280 決定行（全行 next-PUSH 遷移補完済み）・
   friction 中央値 3.08bps（gen1 分析と整合）
5. DESIGN の gap/監査仕様を実装: 場中切断で in-flight を `unresolved_gap` 化・
   復帰板で fill 捏造しない / session_id + connection_epoch の audit.jsonl /
   クラッシュ後起動時に前セッション未終端 entry を `crash_recovered` 化

## シグナル源（較正専用・G8 非消費の条件 — DESIGN 正本の要約）

07-09+07-13 学習 LightGBM を h5s×m3.0×τ0.70 で shadow 稼働。**post-selection の
刺激生成器であり採用戦略ではない**。全出力に `calibration_only=true` +
policy/model/config version。PnL・edge・hit・ratio・セル優劣・閾値調整に使用禁止。
gen1 再採点・07-14 開封・IS-KILL 撤回は禁止。fill 較正値を採用する将来サイクルは
fresh sealed days で評価。全 eligible 1Hz 行の next-PUSH 遷移も別 telemetry
（decisions テーブル）として保存済み（選択バイアス診断用）。

## 次の一手（優先順）

1. **明朝の初回実運転**: start_runtime.ps1 → 場中に heartbeat
   （`S:/jp/stocks_board_kabu_push/heartbeat_kabu_<date>.log`）で msgs 増加を確認。
   終了後 `artifacts/runtime/<date>/summary.json`・audit.jsonl・録画行数を確認
2. slippage_entry/exit_bps の蓄積で fill モデル較正（実 fill n=2 と突合 —
   memory `kabu-sor-fill-calibration`）
3. データが積もったら同一プロトコルの次サイクル（同 family・honest-N 加算）。
   格子・閾値・特徴の変更は新 family + 新 sealed データが必要（G8）
4. PyTorch 小型 NN 比較（DESIGN 決定 5）はエッジの兆候が出てから

## 環境ファクト

- 実行: `uv run pytest`（77 テスト。S: 不在時は recorded_data 系が自動 skip）
- kabu API 疎通確認済み（2026-07-16 夕）: 18080/18081 とも応答（v5.43.0.0）。
  パスワードは `C:/Users/sasai/Documents/backcast/.env` の
  `DEV_/PROD_KABU_API_PASSWORD`（repo に書かない。start_runtime.ps1 が読む）
- `100368`（信用新規抑止）未確認のまま — 解除確認までライブ発注コードは結線しない。
  ランタイムは read-only（`tests/test_runtime_readonly.py` が grep 監査）
- S: に台帳外の `2026-07-10.duckdb`（+.wal）— 使うなら整合性検分が先（変わらず）
- git: main にコミット済み・**push 未実施**（owner 指示待ち、変わらず）

## 落とし穴（再確認用）

- kabu API 作業前に `backcast/.claude/skills/kabusapi/SKILL.md` を必ず読む
  （R5 流量 / R6 50 銘柄 / R8 PUSH 単一コネクション / ping_interval=None）
- **ランタイム稼働中に別プロセスが `POST /token` を発行しない**（token 失効で録画即死。
  token は `S:/jp/.../current_token.json` を読む）。多重起動は start_runtime.ps1 がガード
- **paper 出力（summary/trades）を戦略成績として解釈・台帳記録しない**
  （calibration-only。判定に使った瞬間 G8 違反）
- 判定を出したら vault 台帳（`note/Projects/株価シュミレーション/戦略台帳-data.jsonl`）へ
  1 行追記（2026-07-16 の IS-KILL は記録済み・373 行目）
- config hash `28eb2ba6…` はテストで固定。変更 = 新 family の意図的開始のときのみ
- ライブ⇔バッチの既知の構造差 1 点: バッチは日末 EXIT_NONE 決定を遡及的に
  「取引なし」にできるが、ライブは因果的に保有し unresolved になる（テストで文書化済み）
