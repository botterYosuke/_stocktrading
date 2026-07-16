"""正規化特徴量の逐次 (ライブ) 実装。features.build_features_normalized と同値。

features.py docstring の「ライブ側は逐次スナップショットを追記した配列に対して
末尾 1 点だけ評価してもよい」を、全行再計算なしの O(1)〜O(log w) 逐次状態で
実現する。等価性は tests/test_runtime_live_features.py がバッチ正本
(build_features_normalized) との突合で固定する:

- 行ローカル特徴量 (spread/micro/imb/last_delta) と trailing median は完全一致
- 窓集計 (ret/vol/ofi/mlofi) は総和の丸め順序が異なるため許容誤差つき一致
  (バッチは cumsum 差分、ライブは逐次加減算 — どちらも数学的には同じ窓和)

状態は銘柄ごと・営業日ごとに新規生成する (日初リセット / overnight 状態なし)。
入力は executable 部分列 (sessions.executable_mask を通過した行) のみを与える —
バッチ側が exec_subset 後の配列に build_features_normalized を適用するのと対応。
"""
from __future__ import annotations

import math
from bisect import bisect_left, insort
from collections import deque

from scalp_agent.config import MEDIAN_MIN_S, MEDIAN_WINDOW_S
from scalp_agent.features import FEATURE_NAMES, FEATURE_WINDOWS_S

NAN = float("nan")


class CausalMedian1Hz:
    """features.trailing_median_1hz の逐次版 (1 銘柄 1 系列・日初リセット)。

    - 各完了秒の最終値を dense 系列とし、欠測秒は直前値で前方補完
    - 秒 S の行が参照するのは秒 S-1 までの直近 window_s 秒分の median
    - 完了秒数 < min_s は NaN。NaN 値の秒は「欠測」として前方補完し、
      先頭からの NaN (補完元なし) は窓に NaN として残る (バッチと同じ)
    """

    def __init__(self, window_s: float = MEDIAN_WINDOW_S, min_s: int = MEDIAN_MIN_S):
        self.w = int(window_s)
        self.min_s = int(min_s)
        self.s0: int | None = None
        self.cur_sec: int | None = None
        self.cur_val: float = NAN
        self.count = 0                    # 完了秒数
        self.order: deque[float] = deque()  # 窓内の値 (時系列順・NaN 含む)
        self.sorted_vals: list[float] = []  # 窓内の非 NaN 値 (昇順)
        self.nan_in_window = 0
        self.last_filled: float | None = None  # 前方補完の元 (直近の非 NaN dense 値)

    def _append_dense(self, v: float) -> None:
        if math.isnan(v):
            v = self.last_filled if self.last_filled is not None else NAN
        else:
            self.last_filled = v
        self.order.append(v)
        if math.isnan(v):
            self.nan_in_window += 1
        else:
            insort(self.sorted_vals, v)
        if len(self.order) > self.w:
            old = self.order.popleft()
            if math.isnan(old):
                self.nan_in_window -= 1
            else:
                self.sorted_vals.pop(bisect_left(self.sorted_vals, old))
        self.count += 1

    def update(self, ts: float, x: float) -> float:
        """行 (ts, x) を取り込み、その行が参照すべき median を返す。"""
        sec = int(math.floor(ts))
        if self.s0 is None:
            self.s0 = sec
            self.cur_sec = sec
            self.cur_val = x
            return NAN  # q = -1
        if sec > self.cur_sec:
            for _ in range(sec - self.cur_sec):  # cur_sec .. sec-1 を確定 (前方補完)
                self._append_dense(self.cur_val)
            self.cur_sec = sec
            self.cur_val = x
        else:
            self.cur_val = x  # 同一秒は後勝ち
        if self.count < self.min_s:
            return NAN
        if self.nan_in_window > 0:
            return NAN
        s = self.sorted_vals
        n = len(s)
        if n % 2 == 1:
            return s[n // 2]
        return (s[n // 2 - 1] + s[n // 2]) / 2.0


def _ofi_side(px: float, qty: float, prev_px: float, prev_qty: float, is_bid: bool) -> float:
    """features.ofi_l1 の 1 行分 (bid 側 / ask 側)。NaN は伝播。"""
    if is_bid:
        if px > prev_px:
            return qty
        if px < prev_px:
            return -prev_qty
        return qty - prev_qty
    # ask 側: 価格下落→+qty、上昇→-前qty、同値→Δqty
    if px < prev_px:
        return qty
    if px > prev_px:
        return -prev_qty
    return qty - prev_qty


class _WindowSums:
    """1 つの窓幅 w の (ofi1, mlofi, |Δmid|) 逐次窓和 + ret 用アンカー。

    バッチ (features.trailing_sum / trailing_return) と同じ境界規約:
    - 和の窓は ts >= t-w (境界含む)・自分含む
    - ret の参照は ts <= t-w の最後の点 (無ければ 0.0)
    """

    def __init__(self, w: float):
        self.w = w
        self.items: deque[tuple[float, float, float, float, float]] = deque()
        # (ts, ofi1, ml, dm, mid)
        self.sum_ofi = 0.0
        self.sum_ml = 0.0
        self.sum_dm = 0.0
        self.anchor_mid: float | None = None  # ts <= t-w の最後の mid

    def update(self, ts: float, ofi1: float, ml: float, dm: float, mid: float) -> None:
        self.items.append((ts, ofi1, ml, dm, mid))
        self.sum_ofi += ofi1
        self.sum_ml += ml
        self.sum_dm += dm
        lo = ts - self.w
        items = self.items
        # 和の窓: ts >= t-w を残す (ts < t-w を除去)。除去された点は ret アンカー候補
        while items and items[0][0] < lo:
            t0, o0, m0, d0, mid0 = items.popleft()
            self.sum_ofi -= o0
            self.sum_ml -= m0
            self.sum_dm -= d0
            self.anchor_mid = mid0
        # 境界 ts == t-w は和の窓に残りつつ ret アンカーにもなる (ts <= t-w)
        for it in items:
            if it[0] <= lo:
                self.anchor_mid = it[4]
            else:
                break

    def ret(self, mid_now: float) -> float:
        if self.anchor_mid is None:
            return 0.0
        return mid_now - self.anchor_mid


class LiveFeatureEngine:
    """1 銘柄 1 営業日の逐次特徴量エンジン。

    update(row) は executable 行を 1 行取り込み、その行の FEATURE_NAMES 順
    特徴量ベクトル (list[float]) を返す。row は loader.load_symbol_day と同じ
    キー (ts, bid_px_1..5, bid_qty_1..5, ask_px_1..5, ask_qty_1..5, last_px)。
    """

    def __init__(self):
        self.prev_row: dict[str, float] | None = None
        self.prev_mid: float | None = None
        self.med_bid = CausalMedian1Hz()
        self.med_ask = CausalMedian1Hz()
        self.med_total = CausalMedian1Hz()
        self.windows = {w: _WindowSums(w) for w in FEATURE_WINDOWS_S}
        # バッチの cumsum は NaN 以降すべて NaN — 同じ挙動を flag で再現する
        self.ofi_nan_ever = False
        self.ml_nan_ever = False

    def update(self, row: dict[str, float]) -> list[float]:
        ts = row["ts"]
        b1, a1 = row["bid_px_1"], row["ask_px_1"]
        bq1, aq1 = row["bid_qty_1"], row["ask_qty_1"]
        m = (b1 + a1) / 2.0

        bid5 = row["bid_qty_1"] + row["bid_qty_2"] + row["bid_qty_3"] + row["bid_qty_4"] + row["bid_qty_5"]
        ask5 = row["ask_qty_1"] + row["ask_qty_2"] + row["ask_qty_3"] + row["ask_qty_4"] + row["ask_qty_5"]

        # OFI (前 executable 行との差分)
        prev = self.prev_row
        if prev is None:
            ofi1 = 0.0
            ml = 0.0
        else:
            ofi1 = (_ofi_side(b1, bq1, prev["bid_px_1"], prev["bid_qty_1"], True)
                    - _ofi_side(a1, aq1, prev["ask_px_1"], prev["ask_qty_1"], False))
            ml = 0.0
            for i in range(1, 6):
                ml += (_ofi_side(row[f"bid_px_{i}"], row[f"bid_qty_{i}"],
                                 prev[f"bid_px_{i}"], prev[f"bid_qty_{i}"], True)
                       - _ofi_side(row[f"ask_px_{i}"], row[f"ask_qty_{i}"],
                                   prev[f"ask_px_{i}"], prev[f"ask_qty_{i}"], False))
        if math.isnan(ofi1):
            self.ofi_nan_ever = True
        if math.isnan(ml):
            self.ml_nan_ever = True

        dm = 0.0 if self.prev_mid is None else abs(m - self.prev_mid)

        mb = self.med_bid.update(ts, bid5)
        ma = self.med_ask.update(ts, ask5)
        mt = self.med_total.update(ts, bid5 + ask5)

        for ws in self.windows.values():
            ws.update(ts, ofi1, ml, dm, m)

        feats = {
            "spread_bps": (a1 - b1) / m * 1e4,
            "micro_delta_bps": (_micro(b1, a1, bq1, aq1, m) - m) / m * 1e4,
            "imb_l1": _imb(bq1, aq1),
            "imb_d5": _imb(bid5, ask5),
            "last_delta_bps": (row["last_px"] - m) / m * 1e4,
            "depth_bid_ratio": _safe_ratio(bid5, mb),
            "depth_ask_ratio": _safe_ratio(ask5, ma),
        }
        for w in FEATURE_WINDOWS_S:
            ws = self.windows[w]
            feats[f"ret_{w:g}s_bps"] = ws.ret(m) / m * 1e4
            feats[f"vol_{w:g}s_bps"] = ws.sum_dm / m * 1e4
            feats[f"ofi1_{w:g}s_norm"] = _safe_ratio(
                NAN if self.ofi_nan_ever else ws.sum_ofi, mt)
            feats[f"mlofi_{w:g}s_norm"] = _safe_ratio(
                NAN if self.ml_nan_ever else ws.sum_ml, mt)

        self.prev_row = row
        self.prev_mid = m
        return [feats[k] for k in FEATURE_NAMES]


def _micro(b1: float, a1: float, bq1: float, aq1: float, mid: float) -> float:
    denom = bq1 + aq1
    if denom > 0:  # NaN 比較は False → mid フォールバック (バッチと同じ)
        return (b1 * aq1 + a1 * bq1) / denom
    return mid


def _imb(bq: float, aq: float) -> float:
    denom = bq + aq
    if denom > 0:
        return (bq - aq) / denom
    return 0.0


def _safe_ratio(num: float, den: float) -> float:
    if math.isfinite(den) and den > 0:
        return num / den
    return NAN
