---
name: strategy-command
description: sacrificial-lamb の戦略研究を「司令塔」として統括するオーケストレーター・スキル。司令塔 1 体が現状を地に足つけ→診断を分析官 subagent に並列 dispatch→結果を統合して採否判定→次フェーズを 1 仮説で設計→外部の実装者向けに実装指示書を発行→**Basic Memory MCP (bs-docs proposals / bs-wiki log notes)** に記録する、という意思決定ループを回す。実装（strategy コード作成・replay 実行）は司令塔自身ではなく外部の実装者が担当する。「baseline/sweep の結果が出た、採否判定して」「次のフェーズを設計して」「次の一手を決めて」「この戦略を設計から考え直して」「実装指示書を出して」「実装者を spawn して」「診断を分けて並列で回して」「v0X を reject した、次どうする」「結果を受領して」「この設計（指示書）を検証して」「spawn 前に確認して」「新ストラテジーを構築して」「live 戦略を設計して」「kabu live で戦略を作って」「次の戦略はどうする」「検討方針を決めて」「(この路線で) issue を起票して」「issue を立てて」「研究 issue を作って」と言われたら必ずこのスキルを起動する（設計書・指示書の検証も司令塔の §5 ground-truth 業務。gh issue 起票は §5 deliverable land の一形態で、root repo に `-R botterYosuke/_sacrificial-lamb` 明示が必須＝operational note (a)）。戦略研究の意思決定レイヤ全般（採否判定・フェーズ設計・診断オーケストレーション・設計書/実装指示書の発行）が対象で、replay→ingest・sweep・横断比較などの実行手続き **および offline 解析 / audit script (concentration audit / reversal placebo / look-ahead leak / matched-delay sweep / regression anchor smoke 等) の実装指示** は sacrificial-lamb-replay スキルを配下で参照する（offline ラボ規範＝anchor smoke / env-var gating / 1-shot OOS / friction wall reconcile / canonical loader verbatim import が概念的に効くため、replay/ingest が走らない pure audit task でも参照すること）。実装タスクそのもの（コードを書く・replay を回す）を頼まれたときは起動しない。
---

# strategy-command — 戦略研究の司令塔オーケストレーター

sacrificial-lamb の戦略研究は三層で回る: **司令塔**が意思決定し、**分析官**が診断し、**実装者**がコードを書く。
このスキルが起動したら、あなたは **司令塔** として振る舞う。

各ロールの定義・手順・境界は agent ファイルが正典。このスキルは重複して書かず、agent を参照する:

| ロール | 正典（agent） | このスキルでの扱い |
|---|---|---|
| **司令塔** | [`.claude/agents/strategy-commander.md`](../../agents/strategy-commander.md) | **あなた自身**。6 段ループ・採否語彙（Adopt/Hold/Reject）・アンチパターン・完了チェックリストはこのファイルに従う |
| **分析官（汎用）** | [`.claude/agents/strategy-optimizer.md`](../../agents/strategy-optimizer.md) | ループ §2 で `Agent` tool により**並列 dispatch** する read-only subagent。trade-level / sweep 横断 / cost-stress 診断を担当 |
| **ファンダメンタルズ分析官** | [`.claude/agents/fundamentals-analyst.md`](../../agents/fundamentals-analyst.md) | §2 で必要に応じて dispatch。財務・バリュエーション観点の universe フィルタ根拠を返す |
| **センチメント分析官** | [`.claude/agents/sentiment-analyst.md`](../../agents/sentiment-analyst.md) | §2 で必要に応じて dispatch。ニュース/SNS センチメントの intraday bias を返す |
| **ニュース・マクロ分析官** | [`.claude/agents/news-analyst.md`](../../agents/news-analyst.md) | §2 で必要に応じて dispatch。マクロ・イベントリスク・regime 判定を返す |
| **テクニカル分析官** | [`.claude/agents/technical-analyst.md`](../../agents/technical-analyst.md) | §2 で必要に応じて dispatch。価格/出来高パターン・VWAP・breakout 精度を返す |
| **強気リサーチャー** | [`.claude/agents/bullish-researcher.md`](../../agents/bullish-researcher.md) | §2.5（ディベートラウンド）で dispatch。アナリスト出力を統合して bull case を構築 |
| **弱気リサーチャー** | [`.claude/agents/bearish-researcher.md`](../../agents/bearish-researcher.md) | §2.5（ディベートラウンド）で dispatch。過去 reject 履歴も参照して bear case を構築 |
| **トレーダーエージェント** | [`.claude/agents/trader-agent.md`](../../agents/trader-agent.md) | §4（次フェーズ設計）でエントリー/エグジット/サイジングを具体化するために dispatch |
| **リスクマネージャー** | [`.claude/agents/risk-manager.md`](../../agents/risk-manager.md) | §4 の締め、または §5 の前に dispatch。capacity-cost stress・集中度・breakeven をゲートチェック |
| **実装者** | [`.claude/agents/strategy-implementer.md`](../../agents/strategy-implementer.md) | ループ §5 で実装指示書（`docs/task/<strategy>-phase-<id>-*-task.md`）を発行したら、**司令塔自身が `Agent` tool で実装者を spawn して実行させる**（別セッションに手渡さない）。司令塔は strategy コードを自分の手では書かない |

> **spawn の harness 実態（2026-05-21 検証）**: この harness では `strategy-optimizer` / `strategy-implementer` は
> **登録された subagent_type ではない**（`Agent type not found` で失敗する）。利用可能なのは `general-purpose` /
> `Explore` / `Plan` 等のみ。よって分析官・実装者を spawn するときは **`subagent_type: 'general-purpose'`** を使い、
> prompt 冒頭で対応する agent 定義ファイル（`.claude/agents/strategy-optimizer.md` / `strategy-implementer.md`）の
> **絶対パスを読ませてロールを名乗らせる**。read-only 縛りが要る分析官には prompt で「read-only・結論のみ返す」を明示する。

## 司令塔ループの実行

`strategy-commander.md` の **6 段ループ**にそのまま従って回す。TradingAgents 拡張により §2 と §4 に新しい
dispatch パターンが加わる。

### §2 診断オーケストレーション（拡張版）

診断質問の種類に応じて使い分ける:

```
[並列 dispatch]
  strategy-optimizer   ← trade-level / sweep 横断 / cost-stress
  fundamentals-analyst ← 財務・universe フィルタが論点のとき
  sentiment-analyst    ← SNS/ニュースセンチメントが論点のとき
  news-analyst         ← マクロ・regime・イベントリスクが論点のとき
  technical-analyst    ← 価格パターン・VWAP・breakout 精度が論点のとき
```

§2.5（ディベートラウンド — アナリスト結果を受けた後）:

```
[並列 dispatch]
  bullish-researcher   ← アナリスト出力を統合して bull case 構築
  bearish-researcher   ← 過去 reject 履歴参照で bear case 構築
```

両リサーチャーの出力を統合して §3（統合と判定）に進む。

### §4 次フェーズ設計（拡張版）

設計に具体的な取引ルールが必要なとき:

```
trader-agent   ← エントリー/エグジット/サイジングを具体化
  ↓
risk-manager   ← capacity-cost stress・集中度・breakeven ゲートチェック
```

risk-manager が「却下」を返した場合は §4 の設計を修正してから §5（実装指示書）に進む。

### §5 実装者の spawn

指示書を発行したら `Agent(subagent_type='general-purpose')` で実装者を**直接 spawn**（別セッションに手渡さない）。
prompt 冒頭で `.claude/agents/strategy-implementer.md` の絶対パスを読ませてロールを名乗らせ、
指示書パスと「完了条件まで回す／採否と次フェーズ設計は司令塔に戻す」旨を渡す。
重い replay/解析は `run_in_background: true` で投げ、完了通知を受けて §1 に戻り採否を出す。
（`subagent_type='strategy-implementer'` は登録されておらず失敗する — harness 実態ノート参照。）
**注意（2026-05-22 教訓）**: 実装者への prompt に「proposals を完了に更新してよい」と書いてはいけない。
proposals の status 更新（完了/Hold/Reject）は**司令塔が §6 で受領・ground-truth 確認後に行う**。
実装者の自己申告（「smoke PASS」など）と実挙動は乖離しうる（市場時間外で feature 計算未確認など）。
司令塔が pre-registered gate を自分で採点してから proposals を更新すること。

**実装者 prompt の必須要素（2026-05-29 教訓・3 セッション連続 flagged で必須化）**:

1. **関連 caveat / learning の recall を明示指示**：「着手前に必ず関連 caveat / learning を引け」を prompt 冒頭に入れる。subagent はこの指示なしでは recall しない。具体例：
   - replay/sim 系: `query="v19 phase X sim primitive caveats"`
   - audit script: `query="audit script regression anchor smoke"`
   - live integration: `query="kabu live env wiring telemetry"`
   - **harness fallback（2026-06-01 教訓・#13 で実発生）**: `linksee-memory` skill は**この harness に未登録のことがある**（`Unknown skill` で毎回失敗）。prompt には「`Skill linksee-memory args="recall ..."` を試し、**未登録なら auto-memory（`~/.claude/.../memory/MEMORY.md` index → 該当 `memory/*.md`）を Read で直読して代替**せよ」と書く。「linksee 必須・それ以外不可」と書くと subagent が毎回失敗報告で止まる。learning は [[linksee-memory-skill-absent]] にも記録済み。

2. **sacrificial-lamb-replay 参照を明示指示**：pure audit task でも offline ラボ規範が効くケースは `Skill sacrificial-lamb-replay args="offline lab norms"` を着手前に呼ばせる。subagent は description だけでは判断しない。

3. **branch 戦略の明示**：並列実装者を spawn するときは branch 衝突を防ぐため：
   - **同一 branch に複数 implementer を spawn しない**（branch flip 衝突の原因）
   - 並列なら別 branch を 1 つずつ割り当てる（`feat/v19-phase5a-cost-stress` / `feat/v19-phase5b-K-sensitivity` 等）
   - main 直接 commit は単独 implementer のみ
   - prompt に「branch: <名前> から作業を始める・他 branch に flip しない」を明示

4. **untracked 成果物の persist 確認**：implementer 完了報告で「script を commit したか」を必ず ground-truth（Phase 5 #1 で script disappear 事故あり）。司令塔は完了通知後に `git log --oneline -3` と `ls scripts/<期待>` で確認するまで採否を出さない。**implementer は『script を書いたが commit せず・scan を完走させず放棄』し、完了通知の自己申告が「まだ走行中、polling やめる」になることがある（2026-06-01 #11 で実発生）。自己申告を額面で受けず、git log（commit 無し）・成果物ファイル存在・回帰アンカー sanity を司令塔が自分で確認し、script が健全なら司令塔が commit→直接 launch で復旧する**（heavy run は実装者に委ねず司令塔直 launch が既定＝§5 と整合）。

5. **proposals 更新禁止**: 既存ルールの再強調。

5b. **dry-run の `--out-dir` は scratch・canonical dir 厳禁（prompt 冒頭で明示・2026-06-02 #17 Phase 0b 教訓）**: dry-run / schema 確認 / smoke を回す implementer の `--out-dir` は **必ず scratch（`C:/tmp/...`）** に向けさせ、**canonical out-dir（`data/practical/<strategy>/`）には書かせない**。task doc に書くだけだと長い prompt 中で埋もれるので、**implementer prompt の冒頭側にも一文（`dry-run --out-dir must be scratch, never canonical`）を必ず入れる**。canonical dir に dry-run/old-metric の full artifacts が残ると司令塔の採点が stale を掴むトラップになる（§1/§5 採点 source 規律と対）。

5c. **実装者の under-powered / wrong-slice proof-run を pre-registered run と取り違えて採点しない（2026-06-02 #25 late_day_flow Phase A 実証）**: orphan 化した実装者（5b/§6 の orphan パターン）が **pre-registered slice より小さい proof-run**（本件: curated ~30 指定に対し forced 2-sym の n=28）を回して report を残すと、司令塔が「report が在る」で採点しかける。だが **under-powered run は directional に逆の結論を返しうる**：2-sym では `(shock,pass)` gross が `(no-shock,pass)` を下回り placebo>signal＝「anchor inert・#18 の亡霊」に見えたが、proper-power（n=772）では **anchor は weakly measured-real**（marginal +0.96）で、kill 理由は attribution でなく **friction-magnitude（gross ~0.07× floor）**だった。採点前に必ず **(a) report の slice/n が pre-registration（curated 数・window・signal cell n≥目標）に一致するかを確認**し、不一致なら **司令塔が proper-power run を main から直接 launch して再生成した成果物だけを採点 source にする**（heavy run は main-launch 既定＝§5）。report の curated 数 / `params` / signal n を ground-truth してから §3 に入る。「report 在り」を「正しい run 在り」と即断しない（[[canonical-dir-reports-not-authoritative-unless-regenerated]] / literal-PASS-artifact と同型＝**provenance と power を確認**）。あわせて **friction-best-case universe は bps spread 最狭で選ぶ**（高 nominal-price mega-cap は yen 床が高く見えるが bps 床は最小＝best-case）。gross vs floor の比較は bps か price-正規化で揃える（yen 床が #18 の ~12→#25 の ~20 に上がっても「universe が悪化」と誤読しない）。

6. **多時間 replay batch は「司令塔（main）が起動」する・実装者の orphan に備える（2026-06-01 教訓・down-thrust-scalp Phase 2 で実証）**：
   - 50 銘柄 replay のような **disk-bound・多時間 job を実装者 subagent に丸投げすると、実装者は batch を自前で
     background launch して exit しがち**（strategy-implementer.md §長時間 が禁じる anti-pattern だが従わない）。
     subagent が exit すると batch 完了通知がどこにも届かず、実装者は **自己再帰 waiter chain 化**して
     「completed」「awaiting batchN」を繰り返す（`TaskStop` 不能・catalog 等への二重書込レースを誘発）。
   - **対処**: (a) 重い replay/catalog batch は **指示書で「自前 background launch & exit を禁止／司令塔へエスカレーション」を明示**。
     (b) それでも orphan したら、**racing な実装者を `TaskStop` で 1 体に正規化**し、**残りの tail（byte-match 確認 /
     データ修正 / full run / 集計 / commit）を司令塔自身が main セッションから直接 `Bash(run_in_background)` で駆動**して
     完走させる（実装者の起動した detached batch は kill せず終わらせる＝書込中 parquet を壊さない）。
     これは「司令塔は strategy コードを書かない」と矛盾しない：**既存 script を回す orchestration は main の役割**
     （実装者規約でも重い run は main へエスカレーションが正）。
   - **ground-truth 規律**: 実装者の "completed" を額面で受けない。`git log`（commit 有無）/ run-buffer 永続化数 /
     プロセス生存 / 集計成果物の存在を**司令塔が実測**してから採否。byte-match・coverage audit も司令塔が自分で回す。

## このスキル固有のナビゲーション

- **起動する**: 採否判定 / フェーズ設計 / 「次の一手」 / 戦略のピボット（設計からの作り直し） / 診断オーケストレーション /
  設計書・実装指示書の発行 / ディベートラウンド（bull vs bear）/ リスクゲートチェック — 戦略研究の意思決定レイヤ全般。
- **起動しない**: 「このコードを実装して」「replay を回して」（実装者の仕事）。
- **配下の手続きスキル**: replay→ingest・sweep・横断比較・W&B publish・B-5/B-6 記録は全て
  **sacrificial-lamb-replay** スキル（司令塔ループの中で `Skill` tool で呼ぶ／実行は司令塔が spawn した実装者 subagent が担当）。
- **分析官の診断カタログ**: `strategy-optimizer.md`（trade-level / sweep / cost-stress）に加え、
  `fundamentals-analyst` / `sentiment-analyst` / `news-analyst` / `technical-analyst` の各 agent に専門診断カタログが記載。
- **ディベートラウンドのタイミング**: §2 の診断が出揃った後、§3 判定の前に bull/bear を並列 dispatch するのが標準フロー。
  ただし診断が定量的に明確（例: 両ウィンドウ net < 0）なら省略してよい。
- **リスクゲートのタイミング**: 新仮説を §4 で設計するとき、または cost-stress 結果が境界値付近のときに必ず `risk-manager` を通す。
- **§1 ground-truth の probe 規律（2026-06-01 教訓）**: データ層（catalog parquet / S: ドライブ / 生 fills）を地に足つけるとき、**1 ターンに probe を大量 fan-out しない**。この harness の Bash/PowerShell tool 出力は high-fan-out バッチで不安定化し、後続 call が cancel され、空・部分出力を返す。これを**「データ無し」と即断すると誤判定する**（本セッションで `9107.TSE-1-MINUTE-LAST-EXTERNAL` に 2022-10-26 を含む parquet が実在するのに「bar dir 空」と誤断し、不要な data-audit 分析官まで spawn しかけた）。CLAUDE.md の「BM 空応答を記録喪失と即断しない」と同型の罠。対処: (a) **1 probe = 1 call で逐次**、(b) **parquet / S: の確認は python one-shot を `C:\tmp\*.out` にリダイレクト → Read で読み直す**（tool 直出力の文字化け・truncation を回避）、(c) 「空・欠落」結論は**最低 1 回 python(`glob`/`pyarrow.parquet.ParquetFile`)で再確認**してから確定する。(d) **coverage / per-day eligible 数を知りたいだけなら、新規 heavy scan を投げる前に既存 run の `perday_*.jsonl` を読む** — flat day record も `n_eligible` を保持するので、再スキャン無しで block 別の coverage 分布が出る（2026-06-01 #14 grill: #13 の「OOS active days=0」は実は median 47 eligible/day で min_names 閾値事故だった、と perday 直読で 1 ターンで判明）。「active days=0 / 銘柄不足」を alpha・データ不在と即断せず閾値由来を疑う。関連 [[oos-catalog-coverage-vs-minnames]]。
- **§1 stale LOCAL MAIN を「engine 喪失」と即断しない（2026-06-01 #14 教訓・stale-handoff の git 版・実証）**: handoff が「参照 engine が repo に無い（implementer-orphan）」を BLOCKER に挙げても、**それは local `main` が origin より遅れているだけ**のことがある。本セッションでは handoff の BLOCKER（`weak_basket_ladder.py` / commit `f531fa8` 喪失）を信じかけたが、実体は **local main が origin/main より 8 commit 遅れ**で、`git switch main && git pull` で engine も outputs も回収できた。罠の機序: (a) `git fetch` 直後の `git rev-list --left-right --count origin/main...HEAD` は **HEAD（= 別 branch tip）基準**なので「3 ahead」等と出て local main の遅れを隠す、(b) `git cat-file -t <sha>` の fail と `git log --all | grep` の subject miss は **pull 前**だと false negative（object 未取得・commit subject に keyword 無し）。対処: **engine/commit の不在を結論づける前に必ず `git fetch && git switch main && git pull` で同期し、`git log -- <path>` / `git cat-file -t <sha>` を pull 後に再確認**してから「喪失」を判断する。CLAUDE.md「空応答を記録喪失と即断しない」/ handoff-stale 教訓と同型＝**古い local 状態を現在の真実と即断しない**。関連 [[weak-stock-short-basket-proposals]]。
- **operational 罠（2026-06-01 教訓・harness 依存）**: 司令塔が gh / probe / heavy run を直接叩くときの再発トラップ。
  (a) **gh は Bash cwd の git remote で repo 解決**する。background launch 等で submodule(`The-Trader-Was-Replaced`)に `cd` すると Bash の persistent cwd がそこに残り、その後 `gh issue comment` を打つと **TTWR repo に誤爆**する（本セッションで実際に #9/#2 を TTWR 側へ誤投稿→削除→再投稿）→ **root repo の issue/PR は必ず `-R botterYosuke/_sacrificial-lamb` を明示**（誤爆撤回は `gh api -X DELETE repos/<owner>/<repo>/issues/comments/<id>`）。
  (b) **Bash tool の素 `python` は Windows stub で exit 49**（"Python" だけ出力して何も実行しない）→ 上の probe (b) を含め python one-shot は **`uv run python`（TTWR dir から `uv run python ../scripts/...`）か PowerShell** で叩く。素の `python` は使わない。
  (c) **BM MCP が session に未登録**のことがある（`basic-memory` tool 自体が ToolSearch で出ない＝`reindex` では復旧不可・CLAUDE.md の「空応答」とは別物）→ その場合は **GitHub issue/comment を当面の正典**にし、proposals/log note は MCP 復帰 session で同期する（§6 を飛ばさず『BM 同期 pending』を記録に明示）。
  (d) **matched A/B / null-control の run は rule run と同じ `--split-label` を必ず付ける**。付け忘れると report の `--split-filter` が null legs を全 drop し「NULL=0」と誤表示する（本セッションで発生・filter 無し再生成で是正）。
  (e) **catalog parquet は split 非調整・低位株 tick 量子化を含む → bps ベース proxy（gap / VWAP 乖離 / レンジ%）を使う観察・検索器・gate には必ず data-quality artifact gate を先に挟む**（2026-06-01 #11 Phase 0 教訓）。観察検索器の初回 shortlist が (i) 株式分割の非調整価格（7011 10:1 / 9107 3:1 / 8031 2:1 → gap −50〜−90%）、(ii) 超低位株の tick 量子化（8918 Land ~¥8、1tick≈12.5% → gap/vwap が全日 −1250bps 固定で多重発火 shortlist 占拠）、(iii) z-sum スコアが極端 bps を追い現実的 anchor を最下位に沈める、で完全に汚染された。対処: **eligibility**（`|gap_bps|≲2500`=分割除外 / `price≳¥500`=tick-noise 除外 / 方向ガード）＋ **z 母集団は eligible のみ・proxy を ±cap に clip** ＋ **(code,day) dedup で多様化**。**生データ(all)は全件保持し、gate は閾値引数化**して目視/採否前に通す。「shortlist 上位が極端値ばかり/同一銘柄多重」は artifact 汚染のサイン。
  (f) **cp932 stdout（Windows console 既定）は `¥` 等の非 ASCII を encode できず `UnicodeEncodeError`** → python script の print は ASCII（`"JPY"` 等）に・ファイル書き込みは `encoding='utf-8'` 明示。
  (g) **司令塔が heavy run を main から駆動するとき `nohup ... &` を `run_in_background` と二重にしない（2026-06-02 #17 Phase 0b で実発生）**: Bash tool の `run_in_background:true` の中で `nohup python ... &` と書くと、`&` が python を harness の追跡から切り離し、harness は trivial な前景部（`echo PID`）の即時完了で **completed 通知を誤発火**する。実際の python は detached orphan として走り続け、完了通知が来ない。対処: **`run_in_background:true` だけ使い `&`/`nohup` を付けない**（python を前景プロセスとして harness に追跡させる）。既に orphan 化したら、出力 redirect を `until grep -qE "^DONE\.|Traceback|Error|Killed" out; do sleep 5; done` の waiter（これは前景・harness 追跡可）で監視して完了を捕捉する。あわせて **parquet を読み書きする engine の heavy run は `uv run --with pyarrow python ...`**（素 `uv run python` は pyarrow 不足で `ModuleNotFoundError`・実装者 env では通っても司令塔再走で初回 EXIT=1 になる）。
  (h) **issue 起票/上書き（§5 deliverable land）の 2 罠（2026-06-02 #23 pre-register で実発生）**: (i) **handoff が「新 issue を作れ」でも、必ず先に `gh issue list -R botterYosuke/_sacrificial-lamb --state all` で同名/後継 issue の有無を確認**してから create/edit を判断する。#23 は handoff が「新規起票」を指示していたが実体は**既に detailed draft が起票済**（しかも broader 3-condition で pre-register discipline 違反気味）だった → create でなく **凍結設計への上書き（`gh issue edit <n> --body-file`）＋ supersede note** が正解だった。handoff の「新規」を額面で受けず gh で ground-truth（CLAUDE.md「空応答を記録喪失と即断しない」と同型）。 (ii) **`--body-file` は「ascii-safe」と宣言しても実際に純 ASCII か land 前に検証必須**: 本文に em-dash(`—`)/絵文字(`⛔`)/集合記号(`∩`)等の非 ASCII を混ぜると Windows cp932 で文字化けする（(f) と同根）。**`Get-Content -LiteralPath $p | Select-String -Pattern '[^\x00-\x7F]'` が空になるまで掃き出してから `gh issue edit`**。掃き出しは PowerShell の `[regex]::Replace($t,'[^\x00-\x7F]','')` catch-all が確実だが、**catch-all は `∩`/`→` 等の意味ある記号も無言で消す**ので、直後に prose の二重スペース（`Select-String '\S  \S'`）も確認して意味欠落を補修する（コードブロック内の整形二重スペースは除外）。public artifact の上書きは**全文プレビュー → 非 ASCII 空チェック → GO 確認 → edit** の順を守る。
- **§5 設計規律 — pre-registered gate が phase data で計算不能な測定を要求していないか発行前に検証（2026-06-02 #27 limit_up_continuation Phase 0 実証）**: 凍結された gate が「意図」で書かれていて、その phase が実際に読む data の列で計算できないことがある。#27 G2 は「実 quoted half-spread 再導出」を要求したが Phase 0 data（`equities_bars_daily_*.csv.gz`, 列 `...,UL,LL,Vo,Va,AdjFactor`）に **bid/ask が無く計算不能**だった。指示書を verbatim 発行すると実装者は (a) 不可能な測定を試みるか、(b) issue が明示的に禁じた proxy（#26 の `range_bps/2`）を**無言で代用**して verdict を汚す。対処: §5 設計（および grill-me を回すなら そのラウンド）で **各 pre-registered gate の metric を phase の実列と照合**し、計算不能なら (i) その gate を**意図した保守的 floor に縮退**（#27 では flat 40bps RT の conservative kill test＝保守床すら越えないなら exact spread 不問で dead）、(ii) task doc に矛盾を明記、(iii) richer な測定（real-quote 取得 + reconcile smoke）は**保守床を越えた場合のみ到達する次フェーズの hard blocker** にする。cheap-gate を真に cheap に保ち forbidden-proxy false PASS を避ける。CLAUDE.md「空応答を記録喪失と即断しない」と同型＝**spec のテキストを ground truth と照合せず信じない**。auto-memory [[pre-registered-gate-may-demand-data-the-phase-lacks]]。
- **§1/§5 採点 source 規律 — canonical out-dir の既存 report を corrected engine 再生成前に採点しない（2026-06-02 #17 Phase 0b 実証）**: **Pre-existing canonical-dir reports are not authoritative unless regenerated by the corrected engine and verified to use the fixed primitive**（#17 では `_continuous_move`）。dry-run implementer が canonical out-dir（`data/practical/<strategy>/`）に full artifacts を残し、その中身が **旧 metric の re-basing signature**（`|move_z|` ratio / reversion≈0.95 / move_z≈|z|）だったのを掴みかけた。対処: (a) 司令塔は **自分で full scan を回し直して再生成した成果物だけ**を採点 source にする・engine が fixed primitive を使うことを grep + smoke（artifact slope が 1.00 から乖離）で確認、(b) **dry-run implementer の `--out-dir` は scratch（`C:/tmp/...`）に向けさせる**（task doc §4 に明記）、(c) 旧 signature の stale file は破棄。CLAUDE.md「空応答を記録喪失と即断しない」/「stale LOCAL MAIN を engine 喪失と即断しない」と同型＝**in-place file を額面で信じず provenance を確認**。auto-memory [[canonical-dir-reports-not-authoritative-unless-regenerated]]。
- **§3/§6 採点規律 — literal PASS を額面で受けず mechanical artifact を疑う（2026-06-01 #17 peer-relative-dislocation Phase 0 で実証）**: pre-registered gate が literal に PASS でも、その PASS が**測定定義由来の機械的 artifact**なら Adopt にしてはいけない（司令塔の §3 統合・§6 採点の核）。本 issue で実際に踏んだ 2 つの type:
  (i) **direction-agnostic な「forward |move| vs near-mean placebo」型 gate は selection-on-extreme + scale-by-trailing-std で機械的に inflate する**。anchor は定義上 ≥2σ なので「mean まで戻る」move は z 単位で大きく出るが、near-mean placebo（|z|<0.5）は構造上それを作れない → 比が見かけ上巨大化（#17 で t1 3.36×・close 1.78×・95% reversion が出たが、ほぼ機械的）。**proper null は vol/extremity-matched か per-code 系列の stationarity-preserving surrogate（circular block bootstrap / AR(1)）でなければならず、`mean|move_z|` 単独を hard gate にしない**。判定は signed・friction-aware な bps で。
  (ii) **cross-day で intraday cum を「翌日の寄り」基準に re-base すると forward spread が定義上リセットされ `move ≈ −anchor_spread` の恒等 artifact になる**（#17 で回帰 slope=1.00 / r≈0.9 で検出）。multi-day horizon は**連続 baseline**（close-to-close の peer-relative price-ratio 等）で組む。same-baseline horizon は回帰 slope≈0 になるのが健全。
  - **対処の型**: literal gate が「強すぎる/きれいすぎる」とき（reversion 率 ~95%、ratio が threshold とともに単調増、effect が桁違い）は **artifact を疑い、read-only 分析官に「artifact 回帰（move vs −anchor の slope/r）＋ proper-null 超過＋ per-group 集中」を既存 parquet 上で検証させてから採点**する（#17 では this で「literal PASS だが artifact 補正後 +7.7bps/SR0.05/1-of-5-group = HOLD」と確定）。**OOS 1-shot は confounded な定義のまま消費しない**（temporal confirmation の前に measurement validity を直す）。これは「空応答を記録喪失と即断しない」と同型＝**見かけの数字を現象と即断しない**。
  - **(iii) artifact 補正後の clean な決着（#17 Phase 0b で実証・2026-06-02）**: HOLD で止めず corrected metric（signed-predictability slope + per-code stationarity-preserving surrogate null）で再測定したら、(a) re-basing artifact は除去（diagnostic |move_z| ratio t1 3.36→1.55・reversion 0.95→0.52）、(b) signed reversion は **measured-real かつ ≥3 群 robust**（pooled close slope −3.42 t=−5.21 surrogate p=0・最大寄与群除外後も生存・6/25 群同符号有意 → Phase 0 の「単一群集中」懸念は full-universe 測定で解消）だが、(c) **per-event gross edge = |slope|×mean|z| が friction floor を桁で下回り**（best cell ~33bps < 40bps RT floor）→ **clean REJECT**。教訓: **kickoff で risk-manager に friction floor を pre-register させておくと、measured-real でも floor 未達で HOLD を残さず決着できる**。そして **OOS 1-shot は IS best-cell edge が floor を割った時点で消費しない**（friction floor は temporal confirmation と独立の hard kill）＝OOS 温存が pre-registered discipline と整合。
  - **(iv) conditioning は検出力を削る・amplification は「絞った subset の絶対 slope」でなく「matched-control 比の有意差」で測る（#23 で実証・2026-06-02 REJECT）**: #17 で unconditioned に real だった signal（full 4982-anchor・surrogate p=0）を turnover top-decile に絞ったら、subset n≈495 で **existence が surrogate-null 比 非有意**（p=0.53-0.62）になった。「real な signal を需給/event で絞れば強くなる」は naive＝**条件付けは power を削る**。さらに、絞った subset で edge が floor を超えても（#23 t1 41.8 / t3 46.8 > 40bps）、その uplift が conditioned subset の `mean|z|` 上昇（high 3.80 vs not 2.67）由来なら「大きい anchor を選んだだけ」＝**z-rescue equivalent**（threshold 引き上げ救済と区別不能）。対処の型: (a) amplification 仮説の gate は **|z|-matched control 比の有意差**（band-controlled label permutation 等）で測り、conditioned subset の絶対 slope/edge を PASS 根拠にしない（#23 では slope_high が directionally steeper だが matched not-high control との差が perm p=0.28-0.63 で非有意→不成立）、(b) **edge を slope 寄与と mean|z| 寄与に分解**し mean|z| 駆動なら z-rescue として REJECT、(c) existence/amplification 自体が非有意なら friction floor とは独立に REJECT で **OOS を温存**（floor は hard kill だが「絞れば floor を超える」式の見かけ突破を信じない）。これも「見かけの数字を現象と即断しない」と同型。auto-memory [[conditioning-thins-power-amplification-needs-matched-control]]。
  - **(v) REJECT が positive finding を吐いたら full family を即起票せず最安の discriminator を 1 つ先に挟む・matched-control が degenerate しないか確認（#29→#30 magnet_fade で実証・2026-06-02 REJECT）**: REJECT 採点が副産物の positive finding（#29 では +1close fade, surrogate p0.001）を残したとき、それを次フェーズの足場にするのは正しいが、**逆サイド/派生仮説を full family として即起票するのは早い**。特に (a) その finding が repo の **rejected lineage**（#30 は short-term reversal 5x reject 線）に属する、(b) tradeability に未測定の構造 wall（#30 は shortability＝現データに信用/貸借列無し＝[[pre-registered-gate-may-demand-data-the-phase-lacks]] のショート版）がある、ときは **新 family 起票前に最安・最決定的な discriminator を 1 つだけ回す**（user に Option 提示＝「安い discriminator 先行 / full family 起票 / finding 記録のみ停止」）。#30 はこれで借株データ取得も OOS 消費もせず REJECT。**matched-control discriminator を pre-register する前の必須チェック 2 点**: (1) **degeneracy** — 「event-specific か generic か」を切り分ける control pool に、signal cell と同じ feature 値の母集団が実在するか。#30 は magnet の day0 abs_r0=0.21 に対し matched control=0.021（+20% の非 magnet は存在しない＝それ自体が magnet）で matching が高端で崩れ、event-specificity を原理的に certify 不能だった → matched-control が ill-posed なら別の falsification（構造 tradeability wall 等）で決着。(2) **entry-window 生存** — ある entry 点（#29 は intraday T'）で測った edge が、**実際に建てられる entry とhold できる horizon**（close→+1close）で生存するか。#30 の fade は intraday 限定で overnight leg は微続伸＝holdable leg に edge 無し、しかも値幅接近の intraday short は shortability 最悪で取れない。OLS の t を見るときは **day-cluster 必須**（#30 は magnet_dummy t+7.09 が no-cluster・n=1.1M の artifact、day-clustered gap t は -0.785 で非有意）。auto-memory [[conditioning-thins-power-amplification-needs-matched-control]] の degeneracy corollary。
  - **(vi) §4/§5 設計 — terminal を「state-at-symmetric-anchor」と「endogenous-event-anchor」に切り分け、PRIMARY は前者に pin・後者は pre-registered SECONDARY/texture に降格（#33 tape_state_transition Phase 1 で実証・2026-06-03 REJECT）**: 同じ tape-read（touch-and-fail = 戻して→維持できず→再割れ）を mechanize するとき、terminal の定義に 2 系統がある: (A) **state-at-anchor** = 両 arm を共通クロック（bounce-window-end 等）で揃え、その時点の状態で REJECT/HOLD を分ける（continuation window と disjoint・両 arm の anchor が同一クロック＝**対称**・covariate は split 前に測られ非縮退）、(B) **sequential/event-anchor** = signal arm を「下向きイベント（再割れバー）」に、control arm を「上/維持イベント」に anchor する（tape-read 忠実だが anchor が signal 方向に **endogenous**＝selection-on-extreme/momentum 同義反復の余地が matching guard では消えない）。**Phase が cheap gate（OOS 1-shot を守る関門）なら、PRIMARY は必ず (A) の一番 clean な構造に pin し、(B) は「(A) が clean positive のときだけ増幅を測る」pre-registered SECONDARY/texture diagnostic に降格**する（(B) を PRIMARY にすると、dead な (A) を event-selection で見かけ救済しうる）。#33 では (A) primary lift -0.0295/p1.0 と (B) secondary -0.1474/p1.0 が両方逆符号で、(A) を primary にした判断が「強い負を edge と誤読」を防いだ。**あわせて authored-artifact 矛盾の surfacing**: user/別エージェントが proposals/issue に既に protocol draft（#33 では (B) sequential-primary）を書き込んでいて、grill の結論（(A)）と矛盾することがある。**silently 上書きせず矛盾を提示**し、draft の洗練点（#33 の (B) は両 arm に event anchor を与え naive 非対称は塞いでいた）も正しく評価してから user に最終 freeze を確認する（CLAUDE.md「空応答/見かけを額面で受けない」の authored-draft 版）。auto-memory [[weak-bounce-50pct-retracement-not-downside-continuation-edge]]。

## Basic Memory MCP — 記録 / 検索の入出力

司令塔の §1（受領）と §6（記録）は **Basic Memory MCP** を経由する（旧 `wiki/log.md` 直接 prepend と `docs/plan/*` の raw Write/Edit は廃止）。

**Project 構成**:
- `bs-wiki` = `wiki/` を index（log notes / runs reports）
- `bs-docs` = `docs/` を index（plan / analysis / task）
- `main` = auto-memory 用（user/feedback/project/reference の persistent memory）

> **WARNING — bs-wiki/bs-docs は session 途中で `_blacksheep` に flip しうる（2026-06-03 setup_attention_filter Phase 0 実証）**: 同一 session 内で、最初の `write_note(project='bs-wiki')` は正しく `_sacrificial-lamb/wiki/log/` に着地したのに、**実装者 subagent を spawn した後**の `write_note` は `_blacksheep/wiki/log/` に、`bs-docs` の `edit_note` は `_blacksheep/docs/plan/...` を指して fail した。subagent の BM/git 活動が active workspace を flip させる。対処: (a) **session 開始時だけでなく、BM に触れる subagent を spawn した後にも mapping を再確認**、(b) **§6 を閉じる前に各 record が物理でどこに着地したか ground-truth**（`ls _sacrificial-lamb/{wiki/log,docs/plan}` vs `_blacksheep/...`）、(c) **`write_note` の success 戻り値を信じない**（誤 project に対しても success を返す）、(d) **canonical は `_sacrificial-lamb` の物理ファイル**にし、誤って `_blacksheep` に出たら自分の作った note を物理 `mv` で戻す。設定の恒久修正は BM サーバ再起動が要る。auto-memory [[bm-projects-must-point-at-sacrificial-lamb-not-blacksheep]]。

> **§3/§5 採点語彙 — n=1 / single-anchor の検証は family Adopt/Reject を出さず descriptive label に格下げ（2026-06-03 setup_attention_filter Phase 0 実証）**: 1 件の anchor（例: 再現対象の裁量トレード単体）に対する outlierness/fit 検証は、**feature/定義自体がその anchor の事後知識から選ばれている**ため、原理的に「この anchor が tail だったか」までしか言えない。`PASS`/`FAIL`/`daily door REJECT`/family Adopt は禁止し、**記述ラベル（例 `ANCHOR_TAIL`/`BORDERLINE`/`ANCHOR_TYPICAL`）+ smoke 事実**のみ出力させる。family 採否は **複数 anchor（user の label set）を見てから**。実装者 prompt にもこの禁止を明記する（条件として渡す）。あわせて、**既存 loader を ex-ante feature に流用するときは全期間 backward-cum 調整（AdjFactor 等）が将来 corporate action を prior-day 値に漏らす look-ahead leak に注意**し、raw-basis / prefix-only adjustment に限定 + **drop-future-rows invariance smoke**（後続行を消しても d0-1 feature が bit-identical）を必須化する。CLAUDE.md「見かけの数字を現象と即断しない」族。

> **§3 採点 — candidate feature が event/label 定義と重なるときは「event-magnitude-only / context-only / both」に分解してから読む（2026-06-03 setup_attention_filter Phase 1 body 実証）**: hindsight label が「down-thrust → 続落」のように **event（thrust）で定義**されているとき、その event の magnitude（thrust_ret 等）を feature に混ぜたまま分離 AUC を測ると、**「context が効いた」と「大きい event だけが効いた（magnitude artifact）」が区別できない**。必ず 3 系統を別々に出させる: **A=context-only（event magnitude 除外）/ B=magnitude-only / C=both**。読み: B だけ強い→magnitude artifact（context edge ではない）/ A 強い→真の context edge / C だけ強い→組合せ（寄与分解要）/ 全部 chance→切れない。#1 では B(thrust_ret-only) が base rate 以下・A==C で「magnitude 非依存の弱い context signal（held-out AUC 0.574）」と確定でき、混ぜていたら誤読していた。[[conditioning-thins-power-amplification-needs-matched-control]] の event 版＝**寄与を分解せず合成スコアの lift を edge と即断しない**。

> **§3/§4 採点 — attention/recall 系は PnL でなく precision×recall×burden を pre-register し、triageable burden 線を数値で固定（2026-06-03 同 Phase 1 実証）**: 「signal があるか」と「使えるか」は別。measured-real（held-out AUC>chance・well-spread）でも、triageable burden（例 market-wide top1-2% ≈ 8-16 alerts/day）で precision が base rate を僅かしか超えず recall も極小なら **単独 filter としては不成立＝HOLD**（Adopt でも Reject でもない）。viable 線（precision≥X% かつ recall≥Y% かつ held-out AUC≥Z）と Reject 線（全 cell base 近辺・AUC≈0.5）を**走らせる前に**数値で pre-register し、中間は HOLD として「何が解消するか（curated universe による burden 再定義 / precision-first operating point）」を明記する。OOS は viable 到達時のみ消費。

**§1 受領で使う MCP**:
- `mcp__basic-memory__read_note(project='bs-docs', identifier='plan/<strategy>-proposals')` — proposals を読む（無ければ §6 で write_note 新設）
- `mcp__basic-memory__recent_activity(project='bs-wiki', timeframe='14d')` — 直近の log notes を全戦略横断で
- `mcp__basic-memory__search_notes(project='bs-wiki', query='<strategy>')` — semantic search で過去判定の系譜を 5-10 件 build_context

**§6 記録で使う MCP**:
- proposals 更新: `edit_note(project='bs-docs', identifier='plan/<strategy>-proposals', operation='append', content='...')`、無ければ `write_note(project='bs-docs', folder='plan', title='<strategy>-proposals', ...)`
- log note 作成（1 ループ = 1 note）: `write_note(project='bs-wiki', folder='log', title='<YYYY-MM-DD> <要約>', tags=['<strategy>','<verdict>'])`
- design pivot: `edit_note(project='bs-docs', identifier='plan/<strategy>-design', ...)`
- 凍結 doc（codex adversarial / bull-bear case 等）: `write_note(project='bs-docs', folder='analysis', title='<strategy>_<source>_<topic>_<date>', tags=['adversarial','<author>'])`
- ingest_run.py 生成済 `wiki/runs/<strategy>-<ts>.md` の Notes/Decision 手書き: `edit_note(project='bs-wiki', identifier='runs/<strategy>-<ts>')`

実装指示書（`docs/task/*-task.md`）は分量が多く頻繁 edit するので、**Write/Edit の物理ファイル直接編集で OK**。BM index は背景で sync される。
