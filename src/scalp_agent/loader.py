"""板 PUSH 録画 duckdb の読み出し。

`S:/jp/stocks_board_kabu_push/<date>.duckdb` の `board_push` テーブル
(ワイド形式・1 PUSH msg = 1 行・bid_*=買い板 / ask_*=売り板 に正規化済み) を
銘柄×日単位の numpy 配列辞書として返す。ここより下流 (features / labels) は
すべて pure 関数で、I/O は本モジュールに閉じる。
"""
from __future__ import annotations

import os
from pathlib import Path

import duckdb
import numpy as np

SNAPSHOT_DIR = Path(os.environ.get("BOARD_PUSH_DIR", r"S:/jp/stocks_board_kabu_push"))

# 下流が使う列。深さは 5 段まで (10 段録画のうち)。
_COLUMNS = (
    ["ts_local", "last_px", "volume"]
    + [f"bid_px_{i}" for i in range(1, 6)]
    + [f"bid_qty_{i}" for i in range(1, 6)]
    + [f"ask_px_{i}" for i in range(1, 6)]
    + [f"ask_qty_{i}" for i in range(1, 6)]
)


def db_path(day: str) -> Path:
    """day='2026-07-14' → duckdb ファイルパス。"""
    return SNAPSHOT_DIR / f"{day}.duckdb"


def available_days() -> list[str]:
    if not SNAPSHOT_DIR.exists():
        return []
    return sorted(p.stem for p in SNAPSHOT_DIR.glob("*.duckdb"))


def list_codes(day: str) -> list[str]:
    with duckdb.connect(str(db_path(day)), read_only=True) as con:
        con.execute("SET enable_progress_bar=false")
        rows = con.execute(
            "select distinct code from board_push order by code"
        ).fetchall()
    return [r[0] for r in rows]


def load_symbol_day(day: str, code: str) -> dict[str, np.ndarray]:
    """1 銘柄 1 日分のスナップショット列を ts_local 昇順で返す。

    戻り値の "ts" は epoch 秒 (float64)。best 気配が欠損 (0 or NULL) の行は
    mid が定義できないため除外する。
    """
    cols = ", ".join(_COLUMNS)
    with duckdb.connect(str(db_path(day)), read_only=True) as con:
        con.execute("SET enable_progress_bar=false")
        result = con.execute(
            f"""
            select {cols} from board_push
            where code = ? and bid_px_1 > 0 and ask_px_1 > 0
            order by ts_local
            """,
            [code],
        ).fetchnumpy()
    out: dict[str, np.ndarray] = {}
    ts = result.pop("ts_local")
    out["ts"] = ts.astype("datetime64[us]").astype(np.int64) / 1e6
    for k, v in result.items():
        out[k] = np.asarray(v, dtype=np.float64)
    return out
