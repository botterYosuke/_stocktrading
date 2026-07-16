"""`S:/jp/stocks_minute/<code>.duckdb` の読み出し。I/O は本モジュールに閉じる。

テーブル `stocks_minute`: Date / Time("09:00") / Code / OHLC / Volume / Value。
バーのラベルは**開始分** (09:00 バー = [09:00,09:01) の約定、寄り成行を含む)。
バー確定時刻 = start + 60s。セッションは 09:00〜15:30 (11:30 は前場引け単発バー)。
"""
from __future__ import annotations

import numpy as np

from scalp_agent_bars.minute.config import MINUTE_DIR

SESSION_MIN_TOD = 9 * 3600.0
SESSION_MAX_TOD = 15 * 3600.0 + 30 * 60.0


def db_path(code: str):
    return MINUTE_DIR / f"{code}.duckdb"


def source_fingerprint(code: str) -> dict:
    st = db_path(code).stat()
    return {"code": code, "size": st.st_size, "mtime": st.st_mtime}


def load_symbol_days(
    code: str, day_min: str, day_max: str
) -> dict[str, dict[str, np.ndarray]]:
    """day → バー配列辞書 {ts, start_tod, open, high, low, close, vol}。

    - ts はバー**開始**の naive epoch 秒 (gen1 の ts 規約と同じ日内基準)
    - 日内で start_tod 昇順・(Date,Time) 一意 (データ検分 2026-07-16 で重複ゼロ)
    - OHLC のいずれかが非正の行は落とす
    """
    import duckdb

    with duckdb.connect(str(db_path(code)), read_only=True) as con:
        con.execute("SET enable_progress_bar=false")
        res = con.execute(
            """
            select cast(Date as varchar) as day, Time,
                   Open, High, Low, Close, coalesce(Volume, 0) as Volume
            from stocks_minute
            where Date >= ? and Date <= ?
              and Open > 0 and High > 0 and Low > 0 and Close > 0
            order by Date, Time
            """,
            [day_min, day_max],
        ).fetchnumpy()
    days = res["day"]
    times = res["Time"]
    tod = np.array(
        [float(t[:2]) * 3600.0 + float(t[3:5]) * 60.0 for t in times], dtype=np.float64
    )
    keep = (tod >= SESSION_MIN_TOD) & (tod <= SESSION_MAX_TOD)
    out: dict[str, dict[str, np.ndarray]] = {}
    day_arr = np.asarray(days)[keep]
    tod = tod[keep]
    cols = {
        "open": np.asarray(res["Open"], dtype=np.float64)[keep],
        "high": np.asarray(res["High"], dtype=np.float64)[keep],
        "low": np.asarray(res["Low"], dtype=np.float64)[keep],
        "close": np.asarray(res["Close"], dtype=np.float64)[keep],
        "vol": np.asarray(res["Volume"], dtype=np.float64)[keep],
    }
    # day_arr は Date 昇順で並んでいるため、unique の first-index も昇順
    uniq, first = np.unique(day_arr, return_index=True)
    bounds = np.concatenate([first, [len(day_arr)]])
    for k in range(len(uniq)):
        day = str(uniq[k])
        lo, hi = int(bounds[k]), int(bounds[k + 1])
        day_base = float(np.datetime64(day).astype("datetime64[s]").astype(np.int64))
        out[day] = {
            "ts": day_base + tod[lo:hi],
            "start_tod": tod[lo:hi],
            **{c: v[lo:hi] for c, v in cols.items()},
        }
    return out
