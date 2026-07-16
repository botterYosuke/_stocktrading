"""板 PUSH の日次 duckdb 録画 + heartbeat (既存規約を継承)。

- 出力: `S:/jp/stocks_board_kabu_push/<date>.duckdb` の board_push テーブル
  (ワイド形式 61 列・1 PUSH msg = 1 行・bid_*=買い板 / ask_*=売り板)
- heartbeat: 同ディレクトリ `heartbeat_kabu_<date>.log`
- 途中再起動時は同ファイルへ追記 (CREATE TABLE IF NOT EXISTS + INSERT)
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import duckdb
import pyarrow as pa

from scalp_agent.runtime.boards import COLUMNS, CREATE_SQL_COLS

_PA_TYPES = {
    "VARCHAR": pa.string(),
    "TIMESTAMP": pa.timestamp("us"),
    "DOUBLE": pa.float64(),
    "BIGINT": pa.int64(),
}
ARROW_SCHEMA = pa.schema(
    [(c.split(" ")[0], _PA_TYPES[c.split(" ")[1]]) for c in CREATE_SQL_COLS]
)

FLUSH_ROWS = 500
FLUSH_INTERVAL_S = 5.0


class BoardRecorder:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.con: duckdb.DuckDBPyConnection | None = None
        self.buffer: list[dict] = []
        self.rows_written = 0
        self.last_flush = time.monotonic()

    def open(self) -> None:
        self.con = duckdb.connect(self.db_path)
        self.con.execute("SET enable_progress_bar=false")
        self.con.execute(
            f"CREATE TABLE IF NOT EXISTS board_push ({', '.join(CREATE_SQL_COLS)})"
        )

    def append(self, row: dict) -> None:
        self.buffer.append(row)

    def should_flush(self, now_mono: float) -> bool:
        return (len(self.buffer) >= FLUSH_ROWS
                or (self.buffer and now_mono - self.last_flush >= FLUSH_INTERVAL_S))

    def flush(self) -> None:
        if not self.buffer or self.con is None:
            return
        tbl = pa.Table.from_pylist(self.buffer, schema=ARROW_SCHEMA)
        self.con.register("buf_tbl", tbl)
        self.con.execute(
            f"INSERT INTO board_push SELECT {', '.join(COLUMNS)} FROM buf_tbl"
        )
        self.con.unregister("buf_tbl")
        self.rows_written += len(self.buffer)
        self.buffer.clear()
        self.last_flush = time.monotonic()

    def close(self) -> None:
        if self.con is not None:
            self.con.close()
            self.con = None


class HeartbeatWriter:
    """参照実装と同じ TSV 形式: ts, msgs, rows(バッファ含む), positions, closed, reconnects。"""

    INTERVAL_S = 20.0

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.last = 0.0

    def maybe_write(self, now_mono: float, msgs: int, rows: int,
                    positions: int, closed: int, reconnects: int) -> None:
        if now_mono - self.last < self.INTERVAL_S:
            return
        with open(self.path, "a", encoding="utf-8") as hb:
            hb.write(f"{datetime.now().isoformat()}\t{msgs}\t{rows}\t"
                     f"{positions}\t{closed}\t{reconnects}\n")
        self.last = now_mono
