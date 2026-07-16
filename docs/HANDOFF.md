# 次セッションへの引き継ぎ（2026-07-16 夕更新）

## 現在地

gen1（LightGBM × トリプルバリア taker）を実装・検証し **IS-KILL** で確定
（`docs/analysis/gen1-lgbm-triplebarrier-is-kill-2026-07-16.md`・台帳記録済み）。
07-13 validation で 120 セル中候補 0（n≥100 帯の中央値: gross 1.67 < friction 3.96 bps）。
**07-14 は未開封のまま封印維持**。プロトコル・実装は再利用可能な状態で main にコミット済み。

- パイプライン: `scripts/gen1_pipeline.py`（build-cache / sweep / oos / post-mortem。
  oos は凍結セル必須 + 1 回ロック。OOS 日はキャッシュ計算すら凍結後まで封印）
- `src/scalp_agent/`: config（凍結格子・config hash）/ sessions（1Hz 決定グリッド）/
  features（正規化 19 特徴）/ labels（トリプルバリア）/ execution（シミュレータ）/
  dataset（parquet キャッシュ + manifest）/ gates（ADR-0001 採点）/ nulls（G2 代替ヌル）
- テスト 47 本。`uv run pytest` 全通過が実装完了の条件（変わらず）
- キャッシュ: `artifacts/cache/gen1/`（07-09: 726MB / 07-13: 950MB・gitignore）

## 次の一手（優先順）

1. **録画の毎営業日再開**（最大ボトルネック、変わらず）: `_bellwether/scripts/
   kabu_board_paper_trader.py`（凍結・参照のみ）の WS 受信/録画コアを
   `src/scalp_agent/runtime/` に移植。PUSH 1 コネクション制約（SKILL R8）。
2. データが積もったら同一プロトコルの次サイクル（同 family・honest-N 加算）。
   格子・閾値・特徴の変更は新 family + 新 sealed データが必要（G8）。
3. PyTorch 小型 NN 比較（DESIGN 決定 5）はエッジの兆候が出てから。

## 環境ファクト

- kabu API 疎通確認済み（2026-07-16 夕）: 本体起動時に 18080/18081 とも応答、
  `POST /token` → `GET /apisoftlimit` OK（v5.43.0.0）。パスワードは
  `C:/Users/sasai/Documents/backcast/.env` の `DEV_/PROD_KABU_API_PASSWORD`（repo に書かない）
- `100368`（信用新規抑止）は未確認のまま（発注系に触れない合意）。解除確認まで
  ライブ発注コードは結線しない
- S: に `2026-07-10.duckdb`（+ .wal、未クリーンクローズ痕跡）が存在。データ台帳
  3 日（07-09/13/14）に含まれない日。使うなら整合性検分が先（プロトコルは 07-10 を含まない）
- 録画 duckdb 読み出しは `SET enable_progress_bar=false` 済み。1 銘柄日 ≈ 7 秒で
  キャッシュ構築、以後は parquet から瞬時
- git: main にコミット済み・**push 未実施**（owner 指示待ち、変わらず）

## 落とし穴（再確認用）

- kabu API 作業前に `backcast/.claude/skills/kabusapi/SKILL.md` を必ず読む
  （R5 流量 / R6 50 銘柄 / R8 PUSH 単一コネクション / ping_interval=None）
- `/token` の新規発行は既存トークンを失効させる（録画プロセス稼働中は再発行しない）
- 判定を出したら vault 台帳（`note/Projects/株価シュミレーション/戦略台帳-data.jsonl`）へ
  1 行追記（2026-07-16 の IS-KILL は記録済み・373 行目）
- config hash `28eb2ba6…` はテストで固定。変更 = 新 family の意図的開始のときのみ
