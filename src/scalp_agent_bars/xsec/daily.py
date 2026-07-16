"""gen4 日次パネル。stocks_daily (1 銘柄 1 ファイル) → 単一 parquet に集約する I/O。

列: code, day, close, adj_open, adj_close, turnover, upper, lower。
- gap・前日/5日リターン・ATR は Adjustment* 系列で計算する (分割を跨いで整合)。
- 値幅制限 (upper/lower) と流動性 (turnover) は生値。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from scalp_agent_bars.xsec.config import ART, DAILY_DIR

DAILY_PANEL_PATH = ART / "daily_panel.parquet"

_COLS = ("code", "day", "close", "adj_open", "adj_high", "adj_low", "adj_close",
         "turnover", "upper", "lower")


def build_daily_panel(day_min: str, day_max: str, progress=None) -> None:
    """全銘柄の stocks_daily を走査して 1 parquet へ。数分かかる。"""
    import duckdb

    files = sorted(DAILY_DIR.glob("*.duckdb"))
    ART.mkdir(parents=True, exist_ok=True)
    batches: list[pa.RecordBatch] = []
    schema = pa.schema([
        ("code", pa.string()), ("day", pa.string()),
        ("close", pa.float64()), ("adj_open", pa.float64()),
        ("adj_high", pa.float64()), ("adj_low", pa.float64()),
        ("adj_close", pa.float64()), ("turnover", pa.float64()),
        ("upper", pa.float64()), ("lower", pa.float64()),
    ])
    for i, f in enumerate(files):
        code = f.stem
        try:
            with duckdb.connect(str(f), read_only=True) as con:
                con.execute("SET enable_progress_bar=false")
                res = con.execute(
                    """
                    select cast(cast(Date as date) as varchar) as day,
                           Close, AdjustmentOpen, AdjustmentHigh, AdjustmentLow,
                           AdjustmentClose, TurnoverValue, UpperLimit, LowerLimit
                    from stocks_daily
                    where cast(Date as date) >= ? and cast(Date as date) <= ?
                      and Close > 0 and AdjustmentClose > 0
                    order by Date
                    """,
                    [day_min, day_max],
                ).fetchnumpy()
        except Exception:
            continue
        n = len(res["day"])
        if n == 0:
            continue
        batches.append(pa.record_batch([
            pa.array([code] * n), pa.array(res["day"].astype(str)),
            pa.array(np.asarray(res["Close"], dtype=np.float64)),
            pa.array(np.asarray(res["AdjustmentOpen"], dtype=np.float64)),
            pa.array(np.asarray(res["AdjustmentHigh"], dtype=np.float64)),
            pa.array(np.asarray(res["AdjustmentLow"], dtype=np.float64)),
            pa.array(np.asarray(res["AdjustmentClose"], dtype=np.float64)),
            pa.array(np.asarray(res["TurnoverValue"], dtype=np.float64)),
            pa.array(np.asarray(res["UpperLimit"], dtype=np.float64)),
            pa.array(np.asarray(res["LowerLimit"], dtype=np.float64)),
        ], schema=schema))
        if progress and (i + 1) % 500 == 0:
            progress(f"daily {i + 1}/{len(files)}")
    table = pa.Table.from_batches(batches, schema=schema)
    pq.write_table(table, DAILY_PANEL_PATH)
    if progress:
        progress(f"daily panel rows={table.num_rows} → {DAILY_PANEL_PATH}")


def load_daily_panel() -> dict[str, np.ndarray]:
    t = pq.read_table(DAILY_PANEL_PATH)
    return {c: t[c].to_numpy(zero_copy_only=False) for c in _COLS}


def load_sector_map() -> tuple[dict[str, str], set[str]]:
    """listed_info 静的スナップショット → (code→Sector33Code, プライム code 集合)。

    近似であることは config.py 冒頭に文書化済み。
    """
    import duckdb

    from scalp_agent_bars.xsec.config import LISTED_INFO_DB, PRIME_MARKET_CODE

    with duckdb.connect(str(LISTED_INFO_DB), read_only=True) as con:
        rows = con.execute(
            """
            select Code, arg_max(Sector33Code, Date) as sec,
                   arg_max(MarketCode, Date) as mkt
            from listed_info group by Code
            """
        ).fetchall()
    sector = {r[0]: r[1] for r in rows}
    prime = {r[0] for r in rows if r[2] == PRIME_MARKET_CODE}
    return sector, prime
