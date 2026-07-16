# scalp-agent 設計憲章（2026-07-16 インタビュー確定）

超短期スキャルピング ML-Agent。kabuステーションAPI（三菱UFJ eスマート証券）の板 PUSH を
データ源・執行経路とする。本リポは 2026-07-16 に一度廃止された `_stocktrading` を
ML-Agent 専用ホームとして復活させたもの（研究成果の歴史的正本は `_bellwether/boardbook`）。

## 12 の設計決定

| # | 項目 | 決定 |
|---|------|------|
| 1 | ホーム | `_stocktrading` 復活。boardbook から選択的コピーのみ（ADR-0001・WS/録画コアの設計・開発規律）。medallion SQL 全体と maker シミュレータは持ち込まない |
| 2 | 育成パラダイム | ハイブリッド: オフライン学習/検証が主軸 + 常時ペーパートレード（PUSH 駆動仮想執行）で fill モデル較正。ライブは ADR-0001 ゲート通過後のみ |
| 3 | 終了条件 | 無期限（データが語るまで）。ただし各改良サイクルの判定を台帳に記録する義務は維持 |
| 4 | 執行 | taker ファースト。エントリ=指値(20)+未約定取消、返済=成行(10)。現物は SOR (Exchange=9) 必須 |
| 5 | ML | 段階的: 第一世代=教師あり分類器「N 秒後に mid が ±k tick 以上動く確率」+ 閾値執行。LightGBM ベースライン + PyTorch 小型 NN 比較。エッジ実証後に執行層 RL 化 |
| 6 | ユニバース | スキャル特化再設計（spread を超える動きの頻度基準、50 銘柄枠） |
| 7 | 口座 | `100368`（信用新規抑止）解除を目指しデイトレ信用 (MarginTradeType=3・SOR・14:55 強制クローズ) を正式経路に。解除までライブ発注ブロック |
| 8 | ランタイム | PUSH 1 コネクション制約（SKILL R8）→ 録画・特徴量・推論・執行を単一プロセスに統合 |
| 9 | スタック | Python 3.13 + uv + LightGBM + PyTorch（GPU: RTX 3050 6GB） |
| 10 | リスク | 口座資金は全額余剰資産（owner 明言）。金額ガードなし。バグ暴走対策のサーキットブレーカー（連続エラー停止・発注レート上限）のみ |
| 11 | 運用 | 完全手動起動（毎朝 kabu 本体ログイン→ランタイム起動）。起動漏れは欠損受容・heartbeat で事後確認 |
| 12 | 再学習 | 毎営業日 point-in-time 再学習・as-of モデルキャッシュ・IS/OOS 凍結 |

## 既定値（インタビューで委任された項目）

- 予測ホライズン N・閾値 k はオフライン掃引で決定（想定 5 秒〜5 分）
- ロング/ショート両対応（デイトレ信用前提でペーパーもショートをシミュレート）
- 14:55 強制クローズ + セッション境界で全状態リセット（overnight を跨ぐ状態は存在しない）
- 録画データ置き場は `S:/jp/stocks_board_kabu_push/<date>.duckdb` の既存規約を継続
- kabu PUSH の top-level BidPrice=最良**売**気配という命名罠は録画時点で正規化済み
  （bid_*=買い板 / ask_*=売り板。`kabu_board_paper_trader.py` の慣習を継承）

## 系統コードネーム（2026-07-16 命名）

エージェントは**シグナル源**で 2 系統に分け、以下の名前で呼び分ける
（family スラッグ・config hash は凍結済みのため変更しない。名前は文書上の別名）:

| 系統名 | シグナル源 | 実装 | family (世代) |
|--------|-----------|------|---------------|
| **板読み (ITAYOMI)** | 板 PUSH（spread/imbalance/depth/OFI/microprice + 価格） | `src/scalp_agent/` | gen1: `scalp-taker-triplebarrier-lgbm`（IS-KILL） |
| **足読み (ASHIYOMI)** | 日足・分足のみ（歩み値由来 1 分足 + 前日日足。板は不使用） | `src/scalp_agent/bars/` | gen1b: `scalp-bars-triplebarrier-lgbm`（IS-KILL） |

両系統とも執行・ラベル・friction 計上は同一コード（保守的 next-PUSH taker）。
以後の世代は「板読み gen2」「足読み gen2」のように系統名+世代で呼び、
台帳記録には family スラッグと系統名を併記する。

## 第一世代モデル定式化

- LightGBM `objective=multiclass` の単一3クラスモデルを採用する。
- ラベルは `+1 = longの実行可能往復成立`、`-1 = shortの実行可能往復成立`、`0 = no-trade`。単なるmid方向ではなく、凍結したentry/exit・利確・損切り・timeout規則に基づくcomplete-transactionラベルを正本とする。
- 執行は予測クラスがsideクラスのargmaxで、かつ `P(side) >= τ` の場合のみ。`0` がargmax、side確率が閾値未満、またはデータ不備なら見送る。
- `τ` はIS内だけで選び、OOS適用前に凍結する。第一世代はlong/short共通の単一閾値とし、side別閾値・確率差margin・二段分類は追加しない。
- モデル選定はaccuracyではなく、ADR-0001の `net/entry`・G1〜G8・`gross/friction >= 3` で行う。

## 第一世代サンプリング

- 学習・検証・ライブの**新規エントリ判定は銘柄ごとに1Hz**とする。整数秒境界 `t` で `timestamp <= t` の最後のPUSHをas-of取得し、その時点の特徴量を使う。秒内の最終行を秒の開始へ遡及割当してはならない。
- 前回の秒境界以降に新しいPUSHがない銘柄は推論・学習サンプルを生成しない。最新板の単純forward-fillによる同一局面の複製を避ける。
- complete-transactionラベルは、サンプリング後の1秒系列ではなく**元のPUSH全行**から生成する。利確・損切り・timeoutのfirst-touch順序を保持する。
- 執行シミュレータとライブのポジション管理はPUSH全行で駆動する。約定、利確、損切り、timeout、14:55強制決済を処理する一方、ポジション保有中は新規エントリしない。
- 全行学習とイベント駆動サンプリングは第一世代では採用しない。イベント定義の探索は新familyとしてhonest-Nを消費する。

## 第一世代exitポリシー

- TP / SL / timeout のトリプルバリアを採用し、独立したexitパラメータは増やさない。バリアは既存の `(horizon, mult)` 格子へ固定的に紐付ける。
- エントリ時spreadを `s = ask_0 - bid_0`、バリア幅を `Δ = s × (mult - 1)` とする。`mult = 1` は `Δ = 0` となるため対象外とし、第一世代は `mult > 1` のセルだけを評価する。保有中にspreadが変化してもバリアを再計算しない。
- entry約定PUSHの最良気配を `ask_0`, `bid_0` とする。longは `entry = ask_0`、`TP: bid >= ask_0 + Δ`、`SL: bid <= bid_0 - Δ`。shortは `entry = bid_0`、`TP: ask <= bid_0 - Δ`、`SL: ask >= ask_0 + Δ`。SLはクロスしたentry価格ではなく、entry時点の清算可能な対向bestをアンカーにする。これにより初期spread損をSL幅へ二重算入せず、すべての `mult > 1` でentry直後の機械的SLを防ぐ。
- `mult` は対称な実現損益倍率ではなく、entry時の清算可能quoteから測る追加逆行幅、およびbreak-even価格から測る追加順行幅を定める。したがって実現損益は非対称で、TP時は概ね `+Δ`、SL時は概ね `-(s+Δ)` にnext-PUSH slippageが加わる。この非対称性はtakerのspread負担として隠さず評価する。
- first-touch走査はentry約定PUSH自身を含めず、その厳密に後のPUSHから開始する。PUSH全行で最初に触れたバリアをexitトリガーとする。
- timeoutは `entry_time + horizon` 以後の最初のPUSHで成行返済し、longはbid、shortはaskを約定価格とする。14:55強制決済が先ならそちらを優先する。
- 3クラス教師は各sideを仮想評価し、TPがSL/timeoutより先ならそのsideを勝者候補とする。一方だけ勝者ならそのside、双方が勝者になる往復経路ではTP到達が早いside、同一timestampなら `0 = no-trade`。いずれもTP先着でなければ0。
- シミュレータは実際に選択したsideだけを建て、同じトリプルバリアでexitする。TP/SL倍率の独立掃引は第一世代では禁止し、実施する場合は新familyとしてhonest-Nを加算する。

## 第一世代モデル粒度・正規化

- 全銘柄・全学習日をプールしたLightGBM 1本とする。銘柄別モデル、銘柄code、銘柄別固定効果は第一世代に入れない。IS/OOSの分割単位は行ではなく営業日のまま維持する。
- 価格系はすべて時点 `t` のmidを分母にしてbps化する。spread、`microprice - mid`、trailing return、realized volatility、各板価格のmidからの距離に生の円価格を残さない。
- 数量系は無次元化する。imbalanceは比率のまま、depthは同じ既存窓の過去median depthに対する比率、OFI/MLOFIの各窓集計は同じ窓・同じ対象levelの過去median depthで割る。新しい正規化窓は追加しない。
- rolling medianは現在時点を含む過去データだけで計算し、営業日開始時に全状態をリセットする。将来・日全体・OOSを使った平均/分散やcross-sectional正規化は禁止する。分母が0または不正なら欠損として扱い、0へ潰さない。
- 正規化は `features.py` のpure関数として実装し、オフライン学習・シミュレータ・ライブで同じコードを共有する。
- 集計成績に加えて銘柄別の `n`・net/entry・hit rateを必ず併記し、少数銘柄への利益集中はADR-0001 G3でKILLする。

## 第一世代約定モデル

- 判定時刻 `t` と同じPUSHでは約定させない。その銘柄について `timestamp > t` を満たす**厳密な次PUSH**の対向bestでentry約定する。longはask_1、shortはbid_1。entry時刻・entry spread・timeout起算点はこの約定PUSHで確定する。
- TP/SLはPUSH上のfirst-touchでexitをトリガーするが、そのトリガーPUSHでは約定させない。さらに厳密に後の次PUSHで、longはbid_1、shortはask_1により成行返済する。トリガー後はpending-exitとし、反対側バリアを再判定しない。
- timeoutと14:55強制決済は時計イベントであり、その時刻より厳密に後の最初のPUSHを約定PUSHとする。
- 次PUSHが存在しない、best価格が0・欠損・crossedなど不正、または取引時間外へ出る場合は未約定/評価不能とする。同一PUSHや直前板へのbackfill、さらに先の正常PUSHへの都合のよいskipは禁止する。
- ラベル生成とシミュレータは同じentry/exit next-PUSH関数を共有する。ラベルのTP/SL判定はトリガー順、実現netは次PUSHの実約定価格で計算し、TP後のslippageも隠さず残す。
- 各取引についてdecision→entry、exit-trigger→exitの実時間ミリ秒とPUSH間隔を記録する。固定250ms等への変更はpaper/liveの実測遅延が蓄積してから別familyとして行う。

## 第一世代の格子掃引・凍結

- `2026-07-09` で学習し、`2026-07-13` をIS validation日として使う。格子は `horizon={5,15,30,60,120,300}s × mult={1.5,2.0,2.5,3.0} × τ={0.40,0.50,0.60,0.70,0.80}` の120セルに事前固定する。LightGBMハイパーパラメータは掃引しない。
- validationで `n>=100`、`net/entry>0`、`gross/friction>=3` を満たすセルだけを候補とし、`net/entry` 最大の1セルを選ぶ。同値は `nが多い → horizonが短い → multが小さい → τが高い` の順で機械的に決める。候補0ならOOSを開けずIS-KILLとする。
- 選択セルで07-09+07-13を再学習して全要素を凍結し、07-14へ1回だけ適用する。OOS全格子は凍結セルの結果確定後にpost-mortem診断としてのみ計算し、再選択や第二射に使わない。
- 3営業日ではADR-0001 G4の `D>=20` を満たせない。07-14が良くても判定は `EVALUATION-INCOMPLETE`（pipeline/provisional OOS diagnostic）であり、PASSやedge確認とは呼ばない。

## 第一世代OOS探針のG2代替ヌル

- 正式な日選択G2は `D>=20` 到達後に実施する。07-14の1日探針では、タイミング価値と方向価値を分離する診断ヌルを各200回、固定seed `20260714` で作る。p値は正式判定ではなく記述診断とする。
- **日内時刻ヌル**: 同じ銘柄・同じ30分帯のeligible 1Hz decision行から、実取引と同数を重複なしで抽出し、実際のlong/short構成を維持する。凍結セル、next-PUSH約定、トリプルバリア、ポジション重複禁止をそのまま適用し、200本の有効な再標本を得るまで無効標本を引き直す。
- **サイドヌル**: 実エントリ時刻を固定し、銘柄内でsideラベルをシャッフルしてlong/short数を保つ。同一銘柄のsideが単一で置換不能なら、その銘柄を含むside-nullは退化と明記し、恣意的な50/50生成へ切り替えない。
- 各ヌルについてactual `net/entry`、null median・p05・p95、`actual-null median`、上側empirical p値 `(1 + count(null>=actual))/(B+1)` を報告する。1日内エントリは独立でないため、有意差やG2 PASSとは表現しない。
- OOSヌルを見てセル、時刻帯、side、閾値を変更した場合は新familyとしてhonest-Nを加算し、新しいsealed日が貯まるまで再判定しない。

## 実装開始サイン（2026-07-16）

**GO — 第一世代の実装着手を承認。** ただし07-13 validationを開く前に、以下をコード・設定・テストで固定する。

- 約定のoff-by-one: TP/SLは「トリガーPUSHの厳密な次PUSH」で約定する。timeout/14:55はPUSHトリガーではなく時計イベントなので、**指定時刻より厳密に後の最初のPUSHが約定PUSH**であり、そこからさらに1PUSH待たない。
- τは連続区間ではなく `{0.40,0.50,0.60,0.70,0.80}` の5点。120セル以外を生成したらテスト失敗とする。
- PnL分解: entry/exit約定PUSHのmidを `m0,m1`、sideをlong=`+1` / short=`-1` とし、`gross_bps = side × (m1-m0)/m0 × 1e4`、`net_bps = side × (exit_fill-entry_fill)/m0 × 1e4`、`friction_bps = gross_bps-net_bps`、`ratio = mean(gross_bps)/mean(friction_bps)` に固定する。各取引で `gross-friction=net` を照合する。
- parquet cacheは `(date, code, feature_schema, label_spec, source fingerprint)` をmanifestへ持つ。trainは07-09のみ、validationは07-13のみというdate allowlistをassertし、凍結完了前に07-14のラベル・成績をロードしない。
- LightGBMの全ハイパーパラメータはvalidation実行前に単一configへ明記し、config hashを成果物へ保存する。クラス重みなしは採用するが、出力は未較正のsoftmax scoreであり、τを統計的に較正済み確率とは呼ばない。
- 最低限の回帰テスト: 同一PUSH約定禁止、厳密な次PUSH、entry行をfirst-touch対象外、`mult={1.5,2.0}` で即時SLしない、long/shortのquoteアンカー対称性、TP/SL trigger→next、timeout clock→first-after、next-PUSH不在、`gross-friction=net`、120セル完全一致、train/val/OOS日付漏洩禁止。

## gen1 IS-KILL後のペーパー較正（2026-07-16）

- 07-13 validationで候補セルが0だったため、gen1は **IS-KILL**。07-14はキャッシュ・ラベル・成績を計算せず封印を維持する。
- 録画と統合ランタイムの経路検証・fill較正に限り、07-09+07-13で学習したLightGBMを `horizon=5s, mult=3.0, τ=0.70` 相当でshadow稼働させる。これはvalidation結果を見た後に選んだ **post-selectionの刺激生成器**であり、凍結セル、採用戦略、OOS探針ではない。
- 全出力・取引・成果物へ `calibration_only=true`、固定のcalibration policy version、model/config hashを付ける。実注文は送らず、仮想約定だけを行う。
- このstreamからPnL、hit rate、ratio、edge、有意性、セル優劣を判定・台帳採点しない。閾値・horizon・mult・特徴量の調整にも使わない。gen1の再採点、07-14の開封、IS-KILLの撤回は禁止する。
- fill/slippage/latencyの較正値を将来の評価へ採用する場合は、その値を事前凍結した新サイクルまたは新familyを、当該較正期間より後のfresh sealed daysで評価する。同じ較正データで性能判定まで行わない。
- シグナル選択バイアスの診断用に、発火取引だけでなく全eligible 1Hz decision行のdecision→next-PUSH遷移も別telemetryとして保存する。これは仮想ポジションや戦略成績には数えない。

## 統合ランタイムのWS再接続・日内状態（2026-07-16）

- kabu PUSHは単一WebSocketだけで受信し、`ping_interval=None`、`recv timeout=3600s` とする。90秒timeoutと寄り・昼休みcarve-outは採用しない。
- 場中の無受信はhousekeeping側の `StallDetector` が、最後の受信または場中入りから300秒でrecover、600秒でexitを発火する。recoverはフラグ設定だけで終えず、現在のWebSocketをclose/cancelして待機中の`recv()`を解放し、再接続へ進める。600秒では非ゼロ終了し、外部watchdogの再起動対象とする。
- 再接続は指数backoff+jitterで行い、接続後に固定universeを再registerする。register完了までは録画・推論・新規仮想entryを再開しない。register失敗も接続失敗として扱う。
- transport再接続だけでは日内の特徴量状態・decision clock・仮想ポジション台帳を全リセットしない。ただし切断区間が取引時間と重なったpending-entry / open-position / pending-exitは、first-touchとstrict next-PUSHを観測できないため直ちに `unresolved_gap` として評価対象外へ移し、復帰後の板でfillやexitを捏造しない。予定された場外・昼休みだけの切断はgap汚染に数えない。
- 各接続へ `session_id` と単調増加する `connection_epoch` を付け、disconnect/reconnect/register/unresolvedを監査ログへ残す。再接続後は新しいeligible decisionから仮想entryを再開できる。
- プロセス再起動と営業日境界では全in-memory状態をリセットする。ただし終了前に未解決状態をunresolvedとして永続ログへ書き、クラッシュ後の次回起動でも前sessionの未終端entryを検出してunresolved化する。再起動を暗黙の正常決済として扱わない。

## 兄弟エージェント gen1b — 板非依存・分足/日足（2026-07-16 追加）

owner 指示「板情報を使わずに日足・分足を用いてスキャルピングする兄弟 ML-Agent」。
**新 family**（honest-N +1・G8）。実装は `src/scalp_agent/bars/`、パイプラインは
`scripts/gen1b_pipeline.py`、キャッシュは `artifacts/cache/gen1b`。

- **板の扱いの境界**: 「板を使わない」のは**シグナル（特徴量）**の話。執行・ラベル・
  friction 計上は実板に対して行うため、gen1 の保守的 next-PUSH 約定・トリプルバリア・
  ゲート・ヌルの**コードをそのまま共有**する（spread アンカーのバリア幅も執行側の値）。
- **データ源**: 同じ録画 `board_push` の `last_px`（歩み値）と `volume`（累積出来高）
  から 1 分足を導出。外部バーデータ・新規収集は導入しない。日足コンテキストは
  **前録画日**の歩み値 OHLC（`PREV_DAY` 凍結マップ。台帳外の 07-10 等は暗黙に拾わない）。
- **決定グリッド**: 取引のあった 1 分バーの確定境界のみ（gen1 の 1Hz + 新着なし
  非推論に対応）。特徴量は境界以前に確定したバーのみ。as-of PUSH = 境界以下の最後の
  PUSH、エントリ = その厳密な次 PUSH（= 境界より後の最初の PUSH）、stale ガード 2 秒は同一。
- **特徴量（18 個・全て無次元/bps・分母欠損は NaN）**: k∈{1,3,5,15} 本リターン、
  {5,15} 本レンジ・close 位置、出来高比（直前 30 本 median 比・現在バー除く・
  10 本未満 NaN）、セッション VWAP 乖離、寄り/日中高値/日中安値からの距離、
  時刻（分）、gap・前日リターン・前日レンジ。板由来（spread/imbalance/depth/OFI/
  microprice）は名前ごと禁止（テストで監査）。
- **凍結格子**: `horizon={60,120,180,300,600,900}s × mult={1.5,2.0,2.5,3.0} ×
  τ={0.40,0.50,0.60,0.70,0.80}` の 120 セル。LightGBM ハイパラ・候補条件
  （n≥100・net>0・ratio≥3）・タイブレーク・日割り（train=07-09 / val=07-13 /
  OOS=07-14 封印）は gen1 と同一。config hash `2d25ce07…` をテストで固定。
- **既知の逆風（台帳より）**: 分足/日足ベースの日中ファミリーは過去に大量 KILL
  （寄り ORB・日中モメンタム・daily VWAP reclaim・daily cross-sectional 各種）。
  gen1b は「同じ friction 正直会計の下で bars-only 情報にエッジが残るか」の検証であり、
  死亡済みファミリーの再導出（イベント定義の付け替え等）は新 family 扱いとする。

## データ現況（2026-07-16 時点）

`S:/jp/stocks_board_kabu_push/`:

| 日付 | 行数 | 銘柄数 | 時間帯 |
|------|------|--------|--------|
| 2026-07-09 | 2,285,142 | 50 | 09:43〜15:30 |
| 2026-07-13 | 2,983,603 | 50 | 08:45〜15:30 |
| 2026-07-14 | 2,577,124 | 63 | 08:34〜15:00 |

スキーマ: `board_push` ワイド形式 61 列（10 段 bid/ask 価格・数量、under/over、寄前成行数量、
last_px、volume、OHLC 等）。流動株の平均スプレッドは 2〜4bps — これが taker の friction floor。

## friction 前提

デイトレ信用（一般信用デイトレ・SOR・当日完結）: 手数料 0・金利 0・逆日歩なし →
**friction = spread のみ**。SOR の価格改善（実測 +5.8bps 事例あり）は追い風だが較正モデルでは
保守的に無視する。日跨ぎは強制決済 ¥2,200/注文 + 年 1.8% — 14:55 クローズを設計で保証。

## 過去の死因（必読）

先行の全戦略ファミリーは「friction floor を net で越えるエッジ無し」で KILL。
評価は `docs/adr/ADR-0001-evaluation-standard.md`（(G,S) 単位・net per entry・G1-G8 ガードレール・
friction 比 >= 3）に従う。台帳: vault `C:/Users/sasai/Documents/note/Projects/株価シュミレーション/戦略台帳.md`。
