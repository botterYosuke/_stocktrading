"""PUSH 正規化 (kabu の Bid/Ask 命名罠) と時刻規約の固定。"""
from datetime import datetime

import numpy as np

from scalp_agent.runtime.boards import (
    COLUMNS,
    board_to_push_row,
    board_to_row,
    naive_epoch,
    parse_board,
)


def _msg():
    m = {
        "Symbol": "8306",
        "CurrentPrice": 2762.5,
        "CurrentPriceTime": "2026-07-16T09:00:01+09:00",
        "BidTime": "2026-07-16T09:00:01+09:00",
        "PreviousClose": 2750.0,
        "TradingVolume": 123456,
        "TradingValue": 3.4e8,
        "OpeningPrice": 2755.0,
        "HighPrice": 2765.0,
        "LowPrice": 2751.0,
        "VWAP": 2758.9,
        "UnderBuyQty": 1000,
        "OverSellQty": 2000,
        "MarketOrderSellQty": 0,
        "MarketOrderBuyQty": 0,
        "CurrentPriceStatus": 1,
        "CurrentPriceChangeStatus": "0058",
    }
    for i in range(1, 11):
        m[f"Buy{i}"] = {"Price": 2762.0 - i, "Qty": 100 * i, "Sign": "0101"}
        m[f"Sell{i}"] = {"Price": 2762.5 + i, "Qty": 200 * i, "Sign": "0101"}
    return m


def test_parse_board_normalizes_buy_to_bid_sell_to_ask():
    b = parse_board(_msg())
    # Buy=買い板 → bid_*、Sell=売り板 → ask_* (top-level BidPrice は使わない)
    assert b["bid_px_1"] == 2761.0 and b["bid_qty_1"] == 100
    assert b["ask_px_1"] == 2763.5 and b["ask_qty_1"] == 200
    assert b["bid_px_10"] == 2752.0 and b["ask_px_10"] == 2772.5
    assert b["bid_sign"] == "0101" and b["ask_sign"] == "0101"
    assert b["last_px"] == 2762.5


def test_board_to_row_covers_all_columns():
    b = parse_board(_msg())
    row = board_to_row("8306", "liquid", datetime(2026, 7, 16, 9, 0, 1), b)
    assert set(row.keys()) == set(COLUMNS)


def test_board_to_push_row_shapes_and_filters():
    b = parse_board(_msg())
    ts = naive_epoch(datetime(2026, 7, 16, 9, 0, 1))
    row = board_to_push_row(b, ts)
    assert row is not None
    assert row["ts"] == ts
    assert row["bid_px_1"] == 2761.0 and row["ask_px_1"] == 2763.5
    # best 欠損は棄却 (loader の SQL フィルタと同値)
    b2 = dict(b)
    b2["bid_px_1"] = None
    assert board_to_push_row(b2, ts) is None
    b3 = dict(b)
    b3["ask_px_1"] = 0.0
    assert board_to_push_row(b3, ts) is None
    # last_px 欠損は NaN (行は生きる)
    b4 = dict(b)
    b4["last_px"] = None
    assert np.isnan(board_to_push_row(b4, ts)["last_px"])


def test_naive_epoch_matches_loader_datetime64_convention():
    # loader: datetime64[us] → int64 / 1e6 (naive as-if-UTC)
    dt = datetime(2026, 7, 16, 9, 0, 1, 500000)
    via_numpy = np.datetime64(dt, "us").astype(np.int64) / 1e6
    assert naive_epoch(dt) == via_numpy
    # tod 換算が JST 現地時刻と一致する
    assert naive_epoch(dt) % 86400.0 == 9 * 3600.0 + 1.5
