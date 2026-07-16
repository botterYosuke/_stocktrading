"""仮想約定エンジン (ライブ逐次版)。オフライン凍結規則と同値:

- エントリ: 決定境界 t の厳密な次 executable PUSH の対向 best
  (latency > ENTRY_MAX_LATENCY_S / 14:55 以後の約定は棄却)
- トリプルバリア: s = entry 時 spread、Δ = s×(mult−1)、entry 時固定
  Long : entry=ask₀、TP: bid ≥ ask₀+Δ、SL: bid ≤ bid₀−Δ
  Short: entry=bid₀、TP: ask ≤ bid₀−Δ、SL: ask ≥ ask₀+Δ
- first-touch は entry 約定 PUSH の次の PUSH から。TP/SL はトリガー PUSH の
  厳密な次 PUSH で約定。timeout / 14:55 は時計イベントで、その時刻より厳密に
  後の最初の PUSH が約定 PUSH (もう 1 PUSH 待たない)。14:55 優先。
- 銘柄単位 1 ポジション。日末までに解決しない取引は unresolved (捏造 fill 禁止)。
- PnL 恒等式 gross − friction = net は execution.trade_pnl_bps を共有。

等価性は tests/test_runtime_virtual_execution.py が labels.barrier_outcomes_grid
+ execution.simulate_symbol_day との突合で固定する。

quoted-spread fill と仮想 fill の乖離記録 (DESIGN 決定 2 の較正データ):
- entry_quote_px: 決定 PUSH (境界 t 以前の最後の PUSH) の対向 best
- exit_quote_px : TP/SL はトリガー PUSH、timeout/14:55 は時計イベント直前の
  最後の PUSH の対向 best
- slippage_*_bps: quote で約定した場合との差 (正 = 仮想 fill が不利)
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from scalp_agent.config import ENTRY_MAX_LATENCY_S, FORCE_CLOSE_TOD
from scalp_agent.execution import trade_pnl_bps
from scalp_agent.labels import EXIT_EOD, EXIT_SL, EXIT_TIMEOUT, EXIT_TP

EXIT_REASON_NAMES = {EXIT_TP: "tp", EXIT_SL: "sl", EXIT_TIMEOUT: "timeout", EXIT_EOD: "eod_1455"}


@dataclass(frozen=True)
class PaperTrade:
    code: str
    day: str
    side: int               # +1 long / -1 short
    decision_ts: float
    entry_ts: float
    exit_ts: float
    entry_px: float
    exit_px: float
    mid_entry: float
    mid_exit: float
    exit_reason: int
    exit_trigger_ts: float  # TP/SL はトリガー PUSH、timeout/EOD は時計時刻
    mae_bps: float
    gross_bps: float
    friction_bps: float
    net_bps: float
    entry_quote_px: float       # 決定 PUSH の対向 best (quoted-spread fill)
    exit_quote_px: float        # トリガー時点の対向 best
    slippage_entry_bps: float   # 正 = 仮想 fill が quote より不利
    slippage_exit_bps: float

    @property
    def decision_to_entry_ms(self) -> float:
        return (self.entry_ts - self.decision_ts) * 1e3

    @property
    def trigger_to_exit_ms(self) -> float:
        return (self.exit_ts - self.exit_trigger_ts) * 1e3


@dataclass(frozen=True)
class EntryFilled:
    """entry 約定の監査イベント (クラッシュ後の未終端 entry 検出にも使う)。"""
    code: str
    side: int
    decision_ts: float
    entry_ts: float
    entry_px: float
    entry_quote_px: float


@dataclass(frozen=True)
class EntryCancelled:
    code: str
    side: int
    decision_ts: float
    reason: str  # "latency" | "after_1455" | "day_end" | "gap"


@dataclass(frozen=True)
class Unresolved:
    """exit が約定しないまま終わった取引 (較正集計から除外・診断記録のみ)。

    cause: "day_end" (日末データ切れ) / "gap" (場中の観測 gap — DESIGN 2026-07-16:
    切断区間が取引時間と重なった in-flight は復帰板で fill を捏造しない) /
    "crash_recovered" (前セッションの未終端 entry を次回起動で検出)。
    """
    code: str
    side: int
    decision_ts: float
    entry_ts: float
    entry_px: float
    phase: str  # "holding" | "pending_exit"
    cause: str = "day_end"


IDLE, PENDING_ENTRY, HOLDING, PENDING_EXIT = "idle", "pending_entry", "holding", "pending_exit"


class VirtualSymbolExecution:
    """1 銘柄 1 営業日の仮想約定状態機械。executable PUSH のみを与える。"""

    def __init__(self, code: str, day: str, horizon_s: float, mult: float,
                 entry_max_latency_s: float = ENTRY_MAX_LATENCY_S,
                 force_close_tod: float = FORCE_CLOSE_TOD):
        if mult <= 1.0:
            raise ValueError(f"mult must be > 1 (got {mult})")
        self.code = code
        self.day = day
        self.horizon_s = horizon_s
        self.mult = mult
        self.entry_max_latency_s = entry_max_latency_s
        self.force_close_tod = force_close_tod
        self.phase = IDLE
        # pending entry
        self._decision_ts = 0.0
        self._side = 0
        self._entry_quote_px = math.nan
        # holding
        self._entry_ts = 0.0
        self._entry_px = math.nan
        self._mid_entry = math.nan
        self._tp_thr = math.nan
        self._sl_thr = math.nan
        self._deadline = math.inf
        self._entry_day_base = 0.0     # entry PUSH の ts - tod (EOD トリガー時刻用)
        self._bid_min = math.inf       # MAE (long): entry+1 以降の bid 最小
        self._ask_max = -math.inf      # MAE (short): entry+1 以降の ask 最大
        self._last_bid = math.nan      # 直近 PUSH の best (時計イベントの quote 用)
        self._last_ask = math.nan
        # pending exit
        self._exit_reason = 0
        self._exit_trigger_ts = math.nan
        self._exit_quote_px = math.nan

    @property
    def busy(self) -> bool:
        return self.phase != IDLE

    def on_decision(self, boundary_ts: float, side: int,
                    decision_bid: float, decision_ask: float) -> None:
        """境界 boundary_ts の発火決定。decision_* は決定 PUSH の best。"""
        assert self.phase == IDLE and side in (1, -1)
        self.phase = PENDING_ENTRY
        self._decision_ts = boundary_ts
        self._side = side
        self._entry_quote_px = decision_ask if side == 1 else decision_bid

    def on_push(self, ts: float, tod: float, bid: float, ask: float):
        """executable PUSH を 1 行処理。PaperTrade / EntryCancelled / None を返す。"""
        if self.phase == PENDING_ENTRY:
            return self._on_push_pending_entry(ts, tod, bid, ask)
        if self.phase == HOLDING:
            return self._on_push_holding(ts, tod, bid, ask)
        if self.phase == PENDING_EXIT:
            return self._on_push_pending_exit(ts, bid, ask)
        return None

    def finalize(self):
        """日末処理。未解決取引の診断レコード (または None) を返す。"""
        return self.abort_inflight("day_end")

    def abort_inflight(self, cause: str):
        """in-flight 状態を評価対象外として破棄する (日末 / 場中観測 gap)。

        DESIGN (2026-07-16): 切断区間が取引時間と重なった pending-entry /
        open-position / pending-exit は first-touch と strict next-PUSH を
        観測できないため unresolved 化し、復帰後の板で fill/exit を捏造しない。
        """
        if self.phase == PENDING_ENTRY:
            ev = EntryCancelled(self.code, self._side, self._decision_ts,
                                "gap" if cause == "gap" else cause)
        elif self.phase in (HOLDING, PENDING_EXIT):
            ev = Unresolved(self.code, self._side, self._decision_ts,
                            self._entry_ts, self._entry_px, self.phase, cause)
        else:
            ev = None
        self.phase = IDLE
        return ev

    # ── 内部遷移 ──────────────────────────────────────────────────────────────

    def _on_push_pending_entry(self, ts, tod, bid, ask):
        if ts <= self._decision_ts:
            return None  # 境界以前の PUSH (到着遅延) — エントリ対象は厳密に次の PUSH
        if (ts - self._decision_ts) > self.entry_max_latency_s:
            ev = EntryCancelled(self.code, self._side, self._decision_ts, "latency")
            self.phase = IDLE
            return ev
        if tod >= self.force_close_tod:
            ev = EntryCancelled(self.code, self._side, self._decision_ts, "after_1455")
            self.phase = IDLE
            return ev
        s = ask - bid
        if not (s > 0):
            # executable 行では起きない (呼び出し側フィルタ) が、バッチと同じ防御
            ev = EntryCancelled(self.code, self._side, self._decision_ts, "bad_quote")
            self.phase = IDLE
            return ev
        delta = s * (self.mult - 1.0)
        if self._side == 1:
            self._entry_px = ask
            self._tp_thr = ask + delta   # bid >= thr
            self._sl_thr = bid - delta   # bid <= thr
        else:
            self._entry_px = bid
            self._tp_thr = bid - delta   # ask <= thr
            self._sl_thr = ask + delta   # ask >= thr
        self._entry_ts = ts
        self._mid_entry = (ask + bid) / 2.0
        self._deadline = ts + self.horizon_s
        self._entry_day_base = ts - tod
        self._bid_min = math.inf
        self._ask_max = -math.inf
        self._last_bid, self._last_ask = bid, ask
        self.phase = HOLDING
        return EntryFilled(self.code, self._side, self._decision_ts,
                           ts, self._entry_px, self._entry_quote_px)

    def _update_mae(self, bid: float, ask: float) -> None:
        if bid < self._bid_min:
            self._bid_min = bid
        if ask > self._ask_max:
            self._ask_max = ask

    def _on_push_holding(self, ts, tod, bid, ask):
        self._update_mae(bid, ask)  # MAE 窓は [entry+1, exit 約定行] — この行も対象
        # 時計イベント (EOD 優先) — その時刻より厳密に後の最初の PUSH が約定 PUSH
        if tod > self.force_close_tod:
            return self._fill_exit(
                ts, bid, ask, EXIT_EOD,
                trigger_ts=self._entry_day_base + self.force_close_tod,
                quote_px=self._last_bid if self._side == 1 else self._last_ask,
            )
        if ts > self._deadline:
            return self._fill_exit(
                ts, bid, ask, EXIT_TIMEOUT,
                trigger_ts=self._deadline,
                quote_px=self._last_bid if self._side == 1 else self._last_ask,
            )
        # TP/SL first-touch (トリガー PUSH では約定しない → pending-exit)
        if self._side == 1:
            touched = EXIT_TP if bid >= self._tp_thr else (EXIT_SL if bid <= self._sl_thr else 0)
            quote = bid
        else:
            touched = EXIT_TP if ask <= self._tp_thr else (EXIT_SL if ask >= self._sl_thr else 0)
            quote = ask
        if touched:
            self.phase = PENDING_EXIT
            self._exit_reason = touched
            self._exit_trigger_ts = ts
            self._exit_quote_px = quote
        self._last_bid, self._last_ask = bid, ask
        return None

    def _on_push_pending_exit(self, ts, bid, ask):
        self._update_mae(bid, ask)  # 約定行も MAE 窓に含む
        return self._fill_exit(ts, bid, ask, self._exit_reason,
                               trigger_ts=self._exit_trigger_ts,
                               quote_px=self._exit_quote_px)

    def _fill_exit(self, ts, bid, ask, reason, *, trigger_ts, quote_px):
        side = self._side
        exit_px = bid if side == 1 else ask
        mid_exit = (ask + bid) / 2.0
        gross, friction, net = trade_pnl_bps(
            side, self._entry_px, exit_px, self._mid_entry, mid_exit)
        mae = ((self._mid_entry - self._bid_min) if side == 1
               else (self._ask_max - self._mid_entry)) / self._mid_entry * 1e4
        k = 1e4 / self._mid_entry
        trade = PaperTrade(
            code=self.code, day=self.day, side=side,
            decision_ts=self._decision_ts,
            entry_ts=self._entry_ts, exit_ts=ts,
            entry_px=self._entry_px, exit_px=exit_px,
            mid_entry=self._mid_entry, mid_exit=mid_exit,
            exit_reason=reason, exit_trigger_ts=trigger_ts,
            mae_bps=mae, gross_bps=gross, friction_bps=friction, net_bps=net,
            entry_quote_px=self._entry_quote_px, exit_quote_px=quote_px,
            slippage_entry_bps=side * (self._entry_px - self._entry_quote_px) * k,
            slippage_exit_bps=side * (quote_px - exit_px) * k,
        )
        self.phase = IDLE
        return trade
