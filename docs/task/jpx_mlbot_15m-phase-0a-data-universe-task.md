# Task: jpx_mlbot_15m Phase 0a — データ層 + ユニバース (C1–C3)

**正本**: GitHub Issue #7 (`botterYosuke/_stocktrading#7`)。本 doc は司令塔が発行した実装指示。
**ゴール (Issue #7)**: `make` で **1 日分の 15 分パネル**が出る smoke まで。採点ゲート
(friction floor / p 平均) は Phase 0c。本フェーズで OOS は消費しない。

## 司令塔が凍結した決定 (grill 2026-06-04・上書き不可)
1. `universe_top_n` 初期値 = **100** (config 駆動)。
2. 流動性ランキング = **日次 Va 直近 20 営業日 中央値**・point-in-time (as_of 以前のみ)。
3. 既存 LSTM = **並存・撤去しない**。→ **C1 は tensorflow/keras を残す**。`model_manager.py` と
   既存 LSTM 経路には**一切触れない**。
4. コスト前提 (Phase 0b/0c 用・本フェーズでは未使用) = リテール割安・保守的 (JPX 呼値表基準)。

## 環境上の必須事項 (このマシン)
- **素 `python` は Windows stub で動かない** (exit 49)。**`uv run python` か PowerShell** を使う。
- **`DEV_J_QUANTS_CACHE` は未設定**。データ実体は `S:/j-quants/`。新ローダは `data_source.py` 同様
  **明示 `cache_dir` 引数**を受け、env が無くても動くこと。smoke は `--cache-dir S:/j-quants` で渡す。
- `gh` は **`-R botterYosuke/_stocktrading` 明示**。
- **branch: `feat/phase0a-data-universe` を `master` から切って作業**。他 branch に flip しない。
  `vendor/` と未追跡 `example/` には触れない。
- dry-run/smoke が CSV 等を吐くなら **`C:/tmp/` (scratch)・canonical dir 厳禁**。

## 着手前 (必須)
- 関連 caveat/learning を recall: `Skill linksee-memory args="recall jpx mlbot minute data resample universe"` を試す。
  **未登録 (`Unknown skill`) なら** auto-memory (`~/.claude/projects/.../memory/MEMORY.md` index → 該当 `memory/*.md`)
  を Read で直読して代替。「linksee 必須」で止まらないこと。
- `data_source.py` を読み stdlib パターン (csv.gz 直読 / `normalize_code` / first-wins de-dup / PIT) を踏襲する。

## 実装

### C1 — `requirements.txt` (追記のみ)
- 追加: `lightgbm`, `numba`, `scipy`, `ta`。**保持**: `tensorflow`, `keras` (決定#3)。TA-Lib は追加しない。

### C2 — `minute_data_source.py` (新規)
- `data_source` から `jquants_cache_dir`, `normalize_code`, `code_to_symbol`, `parse_date`,
  `_optional_float` を **import 再利用** (再定義禁止)。
- `MINUTE_FILE_GLOB = "equities_bars_minute_*.csv.gz"`。
- `@dataclass(frozen=True) MinuteBar(timestamp: datetime, code: str, open: float, high: float,
  low: float, close: float, volume: int, value: float | None = None)`。
- `iter_minute_bar_files(cache_dir=None) -> list[Path]`。
- `load_minute_bars(*, cache_dir=None, start=None, end=None, codes=None) -> dict[str, list[MinuteBar]]`:
  CSV.gz 直読、`Date`(`%Y-%m-%d`)+`Time`(`%H:%M`) を naive `datetime` に合成、`(code, timestamp)` **first-wins
  de-dup** (月次+日次フラグメント重複対策)、start/end は **date 単位**で filter、codes filter、
  各 code の bars を timestamp 昇順 sort。
- `resample_15min(bars_by_code) -> dict[str, list[MinuteBar]]`: **セッション境界アンカー**。
  - 前場アンカー = その日 09:00、後場アンカー = その日 12:30。
  - bar の時刻 T が `09:00 <= T < 11:30` → 前場、`T >= 12:30` → 後場。それ以外 (11:30–12:30 の昼休み等) は無いはずだが在れば drop。
  - bin 開始 = anchor + floor((T - anchor) / 15min) * 15min。**前場と後場で別アンカー**なので 11:15 bin と
    12:30 bin が混ざらない。
  - 集約: open=最初の bar の open, high=max(high), low=min(low), close=最後の bar の close,
    volume=sum(volume), value=sum(value)。MinuteBar.timestamp = bin 開始時刻。
  - **欠損 bin は作らない** (合成しない)。後場終了時刻はハードコードしない (TSE は 2024-11-05 に
    15:00→15:30 延長。データに在る bar をそのまま bin 化すれば両方扱える)。

### C3 — `universe.py` (新規)
- `select_universe(*, as_of, top_n=100, price_band=(700.0, 6000.0), va_window=20, cache_dir=None) -> list[str]`:
  - `data_source.load_daily_bars(cache_dir=cache_dir, end=as_of)` で日次 bar 取得。
  - 各 code の **as_of 以前の直近 `va_window` 営業日の Va 中央値** (`statistics.median`)。
    Va 欠損 (None) の bar は除外。`va_window` 本に満たない code は除外。
  - 価格帯: `data_source.newest_close_as_of(bars, code, as_of)` で `price_band[0] < close < price_band[1]`。
  - Va 中央値 降順で top_n。tie-break は code 昇順で決定的に。返り値は normalized code の list。

### Smoke — `panel_smoke.py` (新規・最小) + `make panel-smoke`
- argparse: `--as-of` (必須), `--cache-dir` (default `os.environ.get("DEV_J_QUANTS_CACHE")`),
  `--top-n` (default 100)。
- 流れ: `select_universe(as_of, top_n, cache_dir=cache_dir)` → `load_minute_bars(cache_dir, start=as_of,
  end=as_of, codes=universe)` → `resample_15min` → `(code, timestamp)` 行を平坦化。
- **print のみ**: 選定銘柄数 / パネル総行数 / ユニークな 15分 bin 数 (前場/後場の内訳) / 先頭 10 行。
  canonical dir に書かない。
- `Makefile` に追加 (タブ字下げ厳守):
  ```
  panel-smoke:
  	python panel_smoke.py --as-of $(AS_OF)
  ```
  (実行者は `uv run python panel_smoke.py --as-of 2024-01-31 --cache-dir S:/j-quants` でも可。)

### テスト (unittest・tempfile+gzip・S: ドライブ不要)
- `tests/test_minute_data_source.py`:
  - helper で fake 分足 CSV.gz を tempdir に生成 (header `Date,Time,Code,O,H,L,C,Vo,Va`)。
  - **de-dup**: 月次ファイルと日次ファイルに同一 (code, timestamp) を入れ first-wins を検証。
  - **resample**: 1 銘柄の 09:00–09:29 を 2 bin に集約 (OHLC=first/max/min/last, Vo/Va=sum) を検証。
  - **境界**: 11:15 台と 12:30 台が別 bin になること。**欠損**: 抜けた分で bin が合成されないこと。
- `tests/test_universe.py`:
  - fake 日次 CSV.gz (`data_source` の test helper schema 準拠 = `Date,Code,O,H,L,C,UL,LL,Vo,Va,AdjFactor`)。
  - 直近20日Va中央値ランキング・価格帯 filter・PIT (as_of 後の行を無視)・top_n・tie-break を検証。

## 完了条件 (実装者が満たすべきこと)
1. `uv run python -m unittest tests.test_minute_data_source tests.test_universe` (または pytest) が緑。
2. `uv run python panel_smoke.py --as-of 2024-01-31 --cache-dir S:/j-quants` が銘柄数≈100 の
   15 分パネル要約を print して **exit 0**。
3. `git add` → branch `feat/phase0a-data-universe` に **commit** (script/test を必ず commit。未 commit 放置禁止)。
   `model_manager.py`/LSTM 経路に diff が無いこと。
4. 完了報告に: 作成/編集ファイル一覧、`git log --oneline -5`、unittest 出力、smoke の print 結果
   (銘柄数・行数・bin 数) を**実出力ごと**貼る。**自己申告 PASS だけで終えない**。
5. 採否・proposals 更新・Issue コメントは**しない** (司令塔が ground-truth 後に行う)。
