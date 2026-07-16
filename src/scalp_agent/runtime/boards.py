"""kabu PUSH メッセージ → 正規化 board dict / 録画行。すべて pure。

kabu の命名罠 (kabusapi SKILL R8): top-level BidPrice は最良**売**気配 (=Sell1)。
本モジュールは Buy1..Buy10 / Sell1..Sell10 を正とし、bid=買い板 / ask=売り板の
慣習名へ正規化する (`kabu_board_paper_trader.py` と同一・録画 duckdb 規約を継承)。

時刻規約: 録画の ts_local は naive JST datetime。オフライン loader は
datetime64 → epoch 秒 (naive のまま as-if-UTC) に変換するため、ライブ側の
epoch 秒も `naive_epoch()` で同じ規約に揃える (datetime.timestamp() は TZ を
適用してしまうので使わない)。
"""
from __future__ import annotations

from datetime import datetime

_EPOCH0 = datetime(1970, 1, 1)

_BID_PX = [f"bid_px_{i}" for i in range(1, 11)]
_BID_QTY = [f"bid_qty_{i}" for i in range(1, 11)]
_ASK_PX = [f"ask_px_{i}" for i in range(1, 11)]
_ASK_QTY = [f"ask_qty_{i}" for i in range(1, 11)]

COLUMNS = (
    ["code", "tier", "ts_local", "ts_price", "ts_quote"]
    + _BID_PX + _BID_QTY + _ASK_PX + _ASK_QTY
    + ["under_qty", "over_qty", "mo_sell_qty", "mo_buy_qty",
       "bid_sign", "ask_sign", "px_status", "px_change_status",
       "last_px", "prev_close", "volume", "turnover",
       "day_open", "day_high", "day_low", "vwap"]
)

CREATE_SQL_COLS = (
    ["code VARCHAR", "tier VARCHAR", "ts_local TIMESTAMP",
     "ts_price VARCHAR", "ts_quote VARCHAR"]
    + [f"{c} DOUBLE" for c in _BID_PX] + [f"{c} BIGINT" for c in _BID_QTY]
    + [f"{c} DOUBLE" for c in _ASK_PX] + [f"{c} BIGINT" for c in _ASK_QTY]
    + ["under_qty BIGINT", "over_qty BIGINT", "mo_sell_qty BIGINT", "mo_buy_qty BIGINT",
       "bid_sign VARCHAR", "ask_sign VARCHAR", "px_status VARCHAR",
       "px_change_status VARCHAR", "last_px DOUBLE", "prev_close DOUBLE",
       "volume BIGINT", "turnover DOUBLE",
       "day_open DOUBLE", "day_high DOUBLE", "day_low DOUBLE", "vwap DOUBLE"]
)


def naive_epoch(dt: datetime) -> float:
    """naive JST datetime → オフライン loader と同じ as-if-UTC epoch 秒。"""
    return (dt - _EPOCH0).total_seconds()


def _f(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (ValueError, TypeError):
        return None


def _i(v):
    try:
        if v is None or v == "":
            return None
        return int(float(v))
    except (ValueError, TypeError):
        return None


def parse_board(msg: dict) -> dict:
    """kabu PUSH を bid=Buy / ask=Sell 慣習に正規化した board dict にする。"""
    b = {}
    for i in range(1, 11):
        buy = msg.get(f"Buy{i}") or {}
        sell = msg.get(f"Sell{i}") or {}
        b[f"bid_px_{i}"] = _f(buy.get("Price"))
        b[f"bid_qty_{i}"] = _i(buy.get("Qty"))
        b[f"ask_px_{i}"] = _f(sell.get("Price"))
        b[f"ask_qty_{i}"] = _i(sell.get("Qty"))
    b["under_qty"] = _i(msg.get("UnderBuyQty"))
    b["over_qty"] = _i(msg.get("OverSellQty"))
    b["mo_sell_qty"] = _i(msg.get("MarketOrderSellQty"))
    b["mo_buy_qty"] = _i(msg.get("MarketOrderBuyQty"))
    b["bid_sign"] = (msg.get("Buy1") or {}).get("Sign")
    b["ask_sign"] = (msg.get("Sell1") or {}).get("Sign")
    b["px_status"] = str(msg.get("CurrentPriceStatus")) if msg.get("CurrentPriceStatus") is not None else None
    b["px_change_status"] = msg.get("CurrentPriceChangeStatus")
    b["last_px"] = _f(msg.get("CurrentPrice"))
    b["prev_close"] = _f(msg.get("PreviousClose"))
    b["volume"] = _i(msg.get("TradingVolume"))
    b["turnover"] = _f(msg.get("TradingValue"))
    b["day_open"] = _f(msg.get("OpeningPrice"))
    b["day_high"] = _f(msg.get("HighPrice"))
    b["day_low"] = _f(msg.get("LowPrice"))
    b["vwap"] = _f(msg.get("VWAP"))
    b["ts_price"] = msg.get("CurrentPriceTime")
    b["ts_quote"] = msg.get("BidTime")
    return b


def board_to_row(code: str, tier: str, ts_local: datetime, b: dict) -> dict:
    row = {"code": code, "tier": tier, "ts_local": ts_local,
           "ts_price": b.get("ts_price"), "ts_quote": b.get("ts_quote")}
    for i in range(1, 11):
        row[f"bid_px_{i}"] = b[f"bid_px_{i}"]
        row[f"bid_qty_{i}"] = b[f"bid_qty_{i}"]
        row[f"ask_px_{i}"] = b[f"ask_px_{i}"]
        row[f"ask_qty_{i}"] = b[f"ask_qty_{i}"]
    for k in ("under_qty", "over_qty", "mo_sell_qty", "mo_buy_qty",
              "bid_sign", "ask_sign", "px_status", "px_change_status",
              "last_px", "prev_close", "volume", "turnover",
              "day_open", "day_high", "day_low", "vwap"):
        row[k] = b.get(k)
    return row


def board_to_push_row(b: dict, ts: float) -> dict[str, float] | None:
    """正規化 board dict → ペーパーエンジン入力行 (loader.load_symbol_day と同形)。

    loader の SQL フィルタ (bid_px_1 > 0 and ask_px_1 > 0) と同じ条件で
    欠損 best の行は None を返す。数値は float64 相当・欠損 last_px は NaN。
    """
    b1, a1 = b.get("bid_px_1"), b.get("ask_px_1")
    if not b1 or not a1 or b1 <= 0 or a1 <= 0:
        return None
    row: dict[str, float] = {"ts": ts}
    for i in range(1, 6):
        for k in (f"bid_px_{i}", f"bid_qty_{i}", f"ask_px_{i}", f"ask_qty_{i}"):
            v = b.get(k)
            row[k] = float(v) if v is not None else float("nan")
    lp = b.get("last_px")
    row["last_px"] = float(lp) if lp is not None else float("nan")
    vol = b.get("volume")
    row["volume"] = float(vol) if vol is not None else float("nan")
    return row
