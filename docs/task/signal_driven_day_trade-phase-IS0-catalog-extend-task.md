# Task: signal_driven_day_trade Phase IS-0 — catalog 拡張スクリプト作成 + smoke

**発行日**: 2026-06-04  
**司令塔判定**: Adopt — catalog gap (20/192) が IS baseline 未達の確定 root cause  
**担当**: 実装者（このファイルを読んで自己完結で実行する）  
**スコープ**: `extend_catalog_signal_universe.py` の作成 + smoke + commit まで  
**重い build (172銘柄 × 3月) と replay は司令塔が main から直接 launch する — 実装者はやらない**

---

## 前提

- TTWR repo: `C:\Users\sasai\Documents\The-Trader-Was-Replaced`
- _stocktrading repo: `C:\Users\sasai\Documents\_stocktrading`
- J-Quants source: `S:\j-quants\` (DEV_J_QUANTS_CACHE で指定)
- catalog: `S:\artifacts\jquants-catalog` (ARTIFACTS_PATH or --catalog で指定)
- branch: `main` 直接 commit（単独実装者）
- 実行環境: PowerShell + `uv run --with pyarrow python`（素の `python` は Windows stub で動かない）

---

## コード変更

### 触るファイル
- `The-Trader-Was-Replaced/scripts/extend_catalog_signal_universe.py` — **新規作成**

### 触らないファイル
- `build_catalog_batch.py` — 参照のみ（関数コピー元）
- `examples/signal_driven_day_trade_smoke.*` — 変更禁止
- `S:\artifacts\jquants-catalog\` 既存 parquet — 上書き禁止

---

## スクリプト設計（grilling Q1-Q9 確定版）

```python
#!/usr/bin/env python
"""
extend_catalog_signal_universe.py
_stocktrading/signals/manifest.json の instruments のうち、
jquants-catalog に MINUTE bar が存在しない銘柄を追記する。

Usage:
    $env:DEV_J_QUANTS_CACHE = "S:\j-quants"
    $env:ARTIFACTS_PATH = "S:\artifacts"   # OR: --catalog S:\artifacts\jquants-catalog
    uv run --with pyarrow python scripts/extend_catalog_signal_universe.py
"""
```

### 必須実装要素

**パス解決**
```python
TTWR_ROOT        = Path(__file__).resolve().parents[1]          # scripts/ の親
DOCUMENTS_ROOT   = TTWR_ROOT.parent                             # C:\Users\sasai\Documents
DEFAULT_MANIFEST = DOCUMENTS_ROOT / "_stocktrading" / "signals" / "manifest.json"
```

**catalog path (Q8 fail-fast)**
```python
if args.catalog:
    catalog_path = Path(args.catalog).resolve()
else:
    if not os.environ.get("ARTIFACTS_PATH"):
        raise SystemExit(
            "Error: --catalog or ARTIFACTS_PATH must be set; "
            "refusing to write to local artifacts/jquants-catalog by default."
        )
    catalog_path = jquants_catalog_path()
```

**J-Quants source (Q9 fail-fast)**
```python
from engine.paths import jquants_cache_dir
base_dir = jquants_cache_dir()
if base_dir is None:
    raise SystemExit("Error: DEV_J_QUANTS_CACHE must be set, e.g. S:\\j-quants")
```

**既存 MINUTE 銘柄の検出 (Q2: endswith filter)**
```python
bar_root = catalog_path / "data" / "bar"
existing_minute = {
    p.name.split("-")[0]
    for p in bar_root.iterdir()
    if p.is_dir() and p.name.endswith("-1-MINUTE-LAST-EXTERNAL")
}
targets = [iid for iid in instruments if iid not in existing_minute]
```

**Code→iid lookup dict (Q3: verbatim from build_catalog_batch.py)**
```python
code_to_iid_map = {iid.split(".", 1)[0] + "0": iid for iid in targets}
target_codes = set(code_to_iid_map)
```

**per-month write (Q4)**
```python
for yyyymm in _iter_yyyymm(args.start, args.end):
    path = base_dir / f"equities_bars_minute_{yyyymm}.csv.gz"
    if not path.exists():
        print(f"  [skip] {path.name} not found")
        continue
    rows_by_iid = {iid: [] for iid in targets}
    with gzip.open(path, mode="rt", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            code = row.get("Code", "")
            if code not in target_codes:
                continue
            row_date = date.fromisoformat(row["Date"])
            if not (start_d <= row_date <= end_d):
                continue
            iid = code_to_iid_map[code]
            h, m = map(int, row["Time"].split(":"))
            ts_ns = int(datetime.combine(row_date, dt_time(h, m, 59, 999999), tzinfo=_JST).timestamp() * 1e9)
            vol = float(row.get("Vo", 0) or 0)
            rows_by_iid[iid].append((ts_ns, float(row["O"]), float(row["H"]), float(row["L"]), float(row["C"]), vol))
    _write_bars(rows_by_iid, "Minute", cat)
    # rows_by_iid は次ループ先頭で再作成 → GC に任せる
```

**verbatim コピー対象** (build_catalog_batch.py から):
- `_JST` (timezone)
- `_iter_yyyymm(start: str, end: str) -> Iterator[str]`
- `_write_bars(bars_by_iid: dict, gran: str, cat) -> None`

**print は ASCII only** (cp932 対策 Q7)

**--force は実装しない** (disjoint 衝突防止 Q4)

### CLI 完成形
```
--manifest  default=str(DEFAULT_MANIFEST)
--catalog   default=None  (ARTIFACTS_PATH or fail-fast)
--start     default="2024-11-01"
--end       default="2025-01-30"
```

---

## Smoke 手順（実装者の担当範囲）

**目的**: script が parse・write_data まで通ることを 1銘柄・1日で確認

```powershell
cd "C:\Users\sasai\Documents\The-Trader-Was-Replaced"
$env:DEV_J_QUANTS_CACHE = "S:\j-quants"
# smoke は scratch catalog に書く（canonical S:\artifacts に書かない）
New-Item -ItemType Directory -Force "C:\tmp\catalog-smoke"
uv run --with pyarrow python scripts/extend_catalog_signal_universe.py `
    --catalog "C:\tmp\catalog-smoke" `
    --start 2024-11-01 --end 2024-11-01
```

**期待出力**:
- `Signal universe: N, already in catalog: 0, to build: N` (scratch なので全 N 銘柄対象)
- `Reading equities_bars_minute_202411.csv.gz ... K rows in X.Xs`
- `[1/N] 1348.TSE (Minute): wrote M bars` など

**smoke 確認コマンド**:
```powershell
# scratch に parquet が生えたか
Get-ChildItem "C:\tmp\catalog-smoke\data\bar" | Measure-Object | Select-Object Count
# → 0 より大きければ OK
```

**smoke が通ったら `C:\tmp\catalog-smoke` は削除してよい**

---

## commit

```powershell
cd "C:\Users\sasai\Documents\The-Trader-Was-Replaced"
git add scripts/extend_catalog_signal_universe.py
git commit -m "feat(scripts): signal universe catalog extender (IS-0 infra)"
```

---

## 完了条件（実装者の責務）

- [ ] `scripts/extend_catalog_signal_universe.py` が存在する
- [ ] smoke が通った（scratch catalog に parquet が生えた）
- [ ] `git log --oneline -1` で commit が確認できる
- [ ] **重い build (172銘柄 × 3月) は起動しない — 司令塔に戻す**

---

## スコープ外（実装者はやらない）

- 重い catalog build (172銘柄 × 2024-11-01〜2025-01-30) → 司令塔が main から launch
- IS baseline replay → 司令塔が main から launch
- `S:\artifacts\jquants-catalog` への書き込み → smoke では scratch 使用
- proposals / log note の更新 → 司令塔が §6 で記録

---

## 落とし穴 (再掲)

| 罠 | 対処 |
|---|---|
| `uv run python` に pyarrow 不在 | `uv run --with pyarrow python ...` |
| `ARTIFACTS_PATH` 未設定でそのまま実行 | fail-fast が止める |
| smoke で canonical catalog に書く | `--catalog C:\tmp\catalog-smoke` 必須 |
| `--force` を実装した場合の disjoint 衝突 | 実装しない |
| 英数字コード `137A0` を int に変換 | dict lookup のみ使用 |
| 重い build を background launch して exit | 禁止 — 司令塔にエスカレーション |
