# 次期 family 検証設計（事前登録・実装前凍結）

作成: 2026-07-10。**この文書は各 family の実装前に kill criteria を凍結するためのもの**。
実装・結果はまだ無い。結果を見てからこの文書の基準を緩めることは禁止（緩めたくなったら
その事実自体を docs/architecture.md に記録する）。

imbalance-gated passive entry は事前登録プロトコルで FAIL が確定し**凍結済み**
（0/50 銘柄、手数料 0 でも gross 負。docs/architecture.md 参照）。今後のチューニング対象
ではない。`maker_strategies.py` に negative control として残す。

## 共通ルール（全 family に適用、前フェーズから継承）

1. **判定は net/trip（または net/event）**。合計 net だけで判断しない。往復 1〜5 回の
   argmax は完全に無視する。
2. **手数料 0 で gross がプラスになることが必要条件**。0bps で負なら手数料設定の議論は無意味。
3. **無条件ベンチマーク gate**: 実行シミュレーションを含む family は、シグナル無しの
   無条件版が手数料 0 で「儲からない」ことを先に確認する（fill モデルの楽観検出）。
4. **感度**: latency {0.5→1.0s}、attribution {1.0→0.5} のそれぞれ単独でプラスが維持
   できなければ合格にしない。
5. **plateau 規則**: 勝ち設定はグリッド隣接点の過半もプラスであること。孤立ピークは棄却。
6. **段階制**: Phase A（シグナルスタディ、実行モデル無し、安い）→ Phase B（実行
   シミュレーション）。**Phase A の kill を通過するまで Phase B のコードを書かない。**
7. **日付 OOS**: パラメータ fit は最初の K 日、判定は残り日で凍結評価。1 日データでの
   合否確定は禁止（データ要件は family ごとに下記）。
8. **予算**: 各 family、Phase A ≤ 1 セッション、Phase B ≤ 2 セッション。超過したら停止し
   設計に戻る。

## データ蓄積（最優先の前提作業）

- recorder は稼働中。bronze 取り込みは確定日を自動検出して冪等に追いつくため、
  **分析セッションの冒頭で `ingest-bronze` → `build-silver` を回すだけでよい**
  （日次の自動化は不要。ただし S: ドライブの容量と recorder の heartbeat は時々確認する）。
- 2026-07-09 は 09:43 開始で**寄付き auction を含まない**。auction family の寄付き分析は
  07-10 以降のデータのみ使う。
- silver は 10 段板 schema に更新済み。**旧 schema の silver パーティションが残っていると
  `date=*` の maker 読み込みが schema mismatch で落ちる** → 日付が増えたら全日 rebuild する。

必要日数のマイルストーン:
- **3 日**: lead-lag Phase A 開始可（判定は 5 日以上で）
- **5 日**: lead-lag Phase A 判定可 / MM Phase B の fit 開始可
- **6 日**: MM Phase B 判定可（fit 3 日 + 凍結評価 3 日）
- **10 日**: auction Phase A 判定可（イベント数が下記の床に届く）

---

## Family 1: Auction（寄付き・後場寄りの itayose 参加）

### 仮説（誰から何を取るか）

板寄せ前の成行数量（`mo_buy_qty` / `mo_sell_qty`）・OVER/UNDER 数量・気配の偏りは公開
情報だが、auction 価格はそれを完全には織り込まず、**再開直後の連続セッションで系統的な
ドリフト（dislocation の解消）が起こる**。auction で約定すればスプレッドを跨がずに
建てられるため、per-fill 期待値の負（前 family の死因）を回避できる可能性がある。
day-trade 制約により対象は**寄付き（09:00）と後場寄り（12:30）のみ**（引け auction は
翌日持ち越しになるので対象外）。

### Phase A: シグナルスタディ（実行モデル無し）

- 事前登録シグナル（この 3 つだけ。後から足さない）:
  - S1: 成行インバランス `(mo_buy − mo_sell) / (mo_buy + mo_sell)`（T−30s 時点）
  - S2: 気配インバランス（OVER/UNDER 込み）`(Σbid + under − Σask − over) / 総量`
  - S3: 直前 60 秒の indicative price モメンタム
- 目的変数: auction 価格 → 再開後 +60s / +300s の mid リターン
- サンプル床: **≥1,000 イベント**（50 銘柄 × ≥10 日 × 2 auctions）
- **Kill A（実装前に凍結）**: 3 シグナル × 2 ホライズンのすべてで、上位/下位クインタイル間の
  条件付きドリフト差が **1.5 × (半スプレッド + 1.5bps)** 未満 → family を殺す。
  auction fill シミュレータは書かずに終了。

### Phase B: 実行シミュレーション（Kill A 通過時のみ）

- シミュレータ拡張: itayose fill モデル。**保守的凍結**: 買い指値は auction 価格が指値より
  **厳密に下**のときのみ全量約定（同値は時間優先が不可知なので不約定扱い）。発注は T−5s まで
  （latency 込み）。再開後の exit は既存エンジン（passive + taker fallback @+300s）。
- **Kill B**: 手数料 0 で ≥100 約定イベントを取る設定が、dev 銘柄の 2/3 以上で
  net/event > 0 にならない → kill。
- 追加感度: 「同値でも約定」に緩めたときだけ勝つ → 不合格（fill 楽観で勝っているだけ）。

---

## Family 2: 両面 MM + inventory skew

### 仮説（誰から何を取るか）

前 family の分解で最大の損失は **taker 退出**（半スプレッド支払い）だった。両面提示なら
exit も maker fill になり、taker_edge バケツを構造的に消せる。inventory skew（在庫と
同方向の quote を退げる/引く）と imbalance によるトキシック側 pull で adverse selection と
inventory drift を制御する。**対象は spread ≥ 8bps の銘柄のみ**（6834, 6368, 3110 が dev。
narrow 銘柄は算術的に不成立と確定済み）。

### Phase A: 構造的必要条件チェック（追加実装ほぼゼロ）

既存の無条件ベンチマーク結果から銘柄別に算出する:
- capture/株 = spread_capture ÷ maker 約定株数
- adverse/株 = adverse_selection ÷ maker 約定株数
- **Kill A**: wide 銘柄（spread ≥ 8bps、データ全日）のすべてで
  `capture/株 + adverse/株 ≤ 0` → 両面化しても per-fill 期待値が負のまま → family を殺す。
  （両面化が変えるのは exit の taker→maker 置換と在庫制御であって、per-fill の毒性は
  変わらない。入口で既に負なら出口の改善では救えない。）

### Phase B: 実行シミュレーション（Kill A 通過時のみ）

- エンジン拡張は不要（既存エンジンは複数注文を扱える）。戦略実装のみ:
  両面 touch/improve 提示、在庫 q に応じた skew（ticks/unit）、|q| 上限、
  imbalance がトキシック側に振れたら該当側を pull。
- **事前登録 ablation**: skew off / pull off の 2 つを必ず同時に回す。skew・pull が
  効いていることを分解（inventory drift・adverse の減少）で示せなければ、勝っても
  「たまたま」と扱う。
- **Kill B**: 手数料 0、≥100 trips/銘柄日、wide dev 3 銘柄（6834, 6368, 3110）の 2/3 で
  net/trip > 0 が存在しない → kill。判定はデータ ≥6 日（fit 3 日 / 凍結評価 3 日）。
- 必須指標: 通常セットに加え、在庫分布（max |q|・time-weighted |q|）と
  「taker_edge が実際に消えたか」。

---

## Family 3: 銘柄間 lead-lag

### 仮説（誰から何を取るか）

指数 ETF・同業リーダーの mid 変化は、フォロワーの板が更新される前に観測できる。
リーダー起点のシグナルなら**フォロワーの fill は「シグナル失敗の瞬間」と同時ではない**
（フォロワーの反対側 flow はリーダーを見ていない遅い流れ）ため、前 family を殺した
fill-conditional な逆選択を構造的に回避できる可能性がある。

### Phase A: ペア相関スタディ（実行モデル無し）

- **事前登録ペアのみ**（結果を見てからペアを足すのは禁止。50×50 の全探索は多重検定で無意味）:
  - ETF→大型: 1570 → {9984, 9983, 8035, 6857, 6920, 6146, 7203, 6758}、
    1568 → {8306, 8316, 8411, 7203, 6501, 8058}
  - 電線: 5801↔5802↔5803（3 ペア）
  - 銀行: 8306 → {8316, 8411, 7186, 7182}
  - 重工: 7011 → {7012, 7013}
  - 半導体装置: 8035 → {6857, 6920, 6146, 285A}、6857 → 6920
  - 電機: 6501 → 6503
  （計 28 ペア。双方向を見るのは電線のみ）
- トリガー・ホライズン凍結: リーダーの過去 2s mid リターンが **リーダーspread の 2 倍**を
  超えたらイベント。フォロワーの +2s / +5s mid ドリフトを測る（この 2 ホライズンのみ）。
- サンプル床: ペアあたり ≥200 イベント、**≥5 日**。
- **Kill A**: どのペアも「条件付きドリフト − フォロワー半スプレッド」の中央値がプラスに
  ならない、または最良ペアでも日次符号一貫性（≥4/5 日で同符号）が無い → family を殺す。

### Phase B: 実行シミュレーション（Kill A 通過時のみ）

- 既存 maker エンジンをそのまま使用（シグナルがリーダー由来になるだけ）。**taker 実行も
  並走で比較する**: Phase A のドリフトがフォロワーspread を超えるなら taker でも勝てるはず
  で、maker でしか勝てない場合は fill モデル楽観を疑う。
- リーダー snap とフォロワー snap の時刻整合は `ts_local` 基準 + latency（0.5s）を挟む。
  **リーダーの同時刻 snap を見て同 tick でフォロワーに発注するのは leakage**（受信順序が
  保証されない）。
- **Kill B**: 手数料 0、≥100 trips/ペア、事前登録ペアの上位 5 つのうち 3 つ以上で
  net/trip > 0 が無い → kill。日付 OOS 必須（fit/凍結の分割は MM と同じ）。

---

## 実施順序と停止規則

1. **データ蓄積が最優先**（全 family の前提。セッション冒頭に ingest → silver rebuild）
2. auction（Phase A は 10 日貯まり次第）
3. 両面 MM + inventory skew（Phase A は既存結果の再集計なので即実施可、Phase B は 6 日以降）
4. lead-lag（Phase A は 5 日以降)

3 family すべてが kill された場合: 「retail latency で板情報のみから日中エッジを取る」
という前提自体を docs/architecture.md で棄却判定にかける（latency 短縮・歩み値・
板以外のデータという前提変更の検討に移る）。
