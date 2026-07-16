"""統合ランタイム本体: 板 PUSH 録画 + ペーパートレード (単一プロセス・asyncio)。

usage (毎朝 owner が kabu 本体ログイン後に手動起動 — DESIGN 決定 11):
  KABU_API_PASSWORD=<pw> uv run python -m scalp_agent.runtime.runner
  (scripts/start_runtime.ps1 が backcast/.env から供給するラッパー)

設計 (kabusapi SKILL R8 / DESIGN 決定 8):
- PUSH は最新 1 コネクションのみ配信 → 録画・特徴量・推論・仮想執行を本プロセスに同居
- ws は ping_interval=None + 1h recv timeout (Issue #40)。場中の実ストールは
  StallDetector (300s recover / 600s exit) が housekeeping 側で検知する
- token は起動都度発行し共有ファイルへ書く。稼働中に他プロセスが /token を
  再発行してはならない (失効の副作用)
- 再接続時は register やり直し (サーバ側は登録を保持しない前提)
- 14:55 強制クローズは時計イベントとして「その時刻より厳密に後の最初の PUSH」で
  約定する (housekeeping からの強制 mark はしない — オフライン規則と同値を維持)
- read-only: 発注・取消系エンドポイントは一切呼ばない (パッケージ docstring 参照)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import random
import sys
import time
from datetime import date, datetime, time as dtime

import websockets

from scalp_agent.runtime import calibration
from scalp_agent.runtime.boards import board_to_push_row, board_to_row, naive_epoch, parse_board
from scalp_agent.runtime.recorder import BoardRecorder, HeartbeatWriter
from scalp_agent.runtime.rest import BOARD_DIR, TokenManager
from scalp_agent.runtime.stall import (
    STALL_EXIT_S,
    STALL_RECOVER_S,
    StallDetector,
    in_market_hours,
    market_overlap_seconds,
)
from scalp_agent.runtime.trader import PaperTrader, booster_scorer

try:  # Windows console 既定 cp932 で日本語ログが化けるのを回避
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
    stream=sys.stdout,
)
log = logging.getLogger("scalp_runtime")

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
DEFAULT_UNIVERSE = os.path.join(_REPO, "scripts", "board_recorder_universe.txt")
RUNTIME_OUT_DIR = os.path.join(_REPO, "artifacts", "runtime")

# 2026 年 日本の祝日 + 東証休場 (参照実装から流用)
JP_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-02", "2026-01-03",
    "2026-01-12", "2026-02-11", "2026-02-23", "2026-03-20",
    "2026-04-29", "2026-05-03", "2026-05-04", "2026-05-05", "2026-05-06",
    "2026-07-20", "2026-08-11", "2026-09-21", "2026-09-22", "2026-09-23",
    "2026-10-12", "2026-11-03", "2026-11-23", "2026-12-31",
}

RECV_TIMEOUT_S = 3600.0  # SKILL R8 / Issue #40: 1h recv timeout (carve-out 不要)


def load_universe(path: str) -> list[tuple[str, str]]:
    codes = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.replace(",", "\t").split("\t")
            code = parts[0].strip()
            tier = parts[1].strip() if len(parts) > 1 else "unknown"
            if code:
                codes.append((code.upper(), tier))
    return codes


def is_business_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    return d.strftime("%Y-%m-%d") not in JP_HOLIDAYS_2026


class RuntimeApp:
    def __init__(self, args):
        self.args = args
        self.base = f"http://localhost:{args.port}/kabusapi"
        self.ws_url = f"ws://localhost:{args.port}/kabusapi/websocket"
        self.universe = load_universe(args.universe)
        self.code_tier = {c: t for c, t in self.universe}
        self.day_str = date.today().strftime("%Y-%m-%d")

        os.makedirs(BOARD_DIR, exist_ok=True)
        self.recorder = BoardRecorder(os.path.join(BOARD_DIR, f"{self.day_str}.duckdb"))
        self.heartbeat = HeartbeatWriter(
            os.path.join(BOARD_DIR, f"heartbeat_kabu_{self.day_str}.log"))

        self.trader: PaperTrader | None = None
        self.outputs = None
        if not args.record_only:
            from scalp_agent.runtime.outputs import PaperOutputs
            booster = calibration.load_booster()
            meta = {**calibration.model_meta(), "model_version": calibration.model_version()}
            self.outputs = PaperOutputs(
                os.path.join(RUNTIME_OUT_DIR, self.day_str), self.day_str, meta)
            self.trader = PaperTrader(
                self.day_str, [c for c, _ in self.universe],
                calibration.CAL_HORIZON_S, calibration.CAL_MULT, calibration.CAL_TAU,
                booster_scorer(booster), self.outputs)

        self.tokens = TokenManager(self.base, args.api_password, log)
        self.stall = StallDetector()
        self.stop_event: asyncio.Event | None = None
        self.msgs_total = 0
        self.reconnects = 0
        self.recover_requested = False
        self.exit_rc = 0
        self._ws = None
        self._last_boundary = 0
        # 接続監査 (DESIGN 2026-07-16): session_id + 単調増加 connection_epoch
        self.session_id = f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"
        self.connection_epoch = 0
        self._disconnected_at: float | None = None
        self.deadline = None
        if args.duration_min is not None:
            self.deadline = time.monotonic() + args.duration_min * 60.0

    def _audit(self, event: str, **fields) -> None:
        if self.outputs is not None:
            try:
                self.outputs.add_audit(event, session_id=self.session_id,
                                       connection_epoch=self.connection_epoch, **fields)
            except Exception as e:
                log.warning(f"audit 書き込み失敗 (継続): {e}")

    def _on_disconnected(self) -> None:
        """切断検知: 時刻を記録し、場中なら直ちに in-flight を gap 破棄する。"""
        if self._disconnected_at is None:
            self._disconnected_at = naive_epoch(datetime.now())
            self._audit("disconnect")
        if self.trader is not None and in_market_hours(datetime.now().time()):
            n = self.trader.on_gap()
            if n:
                log.warning(f"場中切断 — in-flight {n} 件を unresolved_gap 化")
                self._audit("unresolved_gap", n=n)
        if self.trader is not None:
            self.trader.suspended = True  # register 完了まで推論・entry 停止

    def _on_registered(self) -> None:
        """再接続 + register 完了: gap 判定を締めて推論を再開する。"""
        if self._disconnected_at is not None:
            now = naive_epoch(datetime.now())
            overlap = market_overlap_seconds(self._disconnected_at, now)
            if overlap > 0 and self.trader is not None:
                n = self.trader.on_gap()  # 冪等 (切断時に破棄済みなら 0)
                if n:
                    log.warning(f"切断区間が取引時間と {overlap:.0f}s 重複 — "
                                f"in-flight {n} 件を unresolved_gap 化")
                self._audit("unresolved_gap", n=n, overlap_s=round(overlap, 1))
            self._disconnected_at = None
        if self.trader is not None:
            self.trader.suspended = False
        self._audit("registered")

    # ── PUSH 受信 ────────────────────────────────────────────────────────────
    def _handle_message(self, raw) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        code = str(msg.get("Symbol") or "").upper()
        if not code:
            return
        self.msgs_total += 1
        self.stall.on_msg(time.monotonic())
        ts_local = datetime.now()
        b = parse_board(msg)
        self.recorder.append(board_to_row(code, self.code_tier.get(code, "unknown"), ts_local, b))
        if len(self.recorder.buffer) >= 500:
            try:
                self.recorder.flush()
            except Exception as e:
                log.error(f"録画 flush 失敗: {e}")
        if self.trader is not None:
            row = board_to_push_row(b, naive_epoch(ts_local))
            if row is not None:
                try:
                    self.trader.on_push(code, row)
                except Exception as e:
                    log.error(f"{code} paper 評価失敗: {e}")

    async def recv_forever(self):
        backoff = 5
        first_connect = True
        while not self.stop_event.is_set():
            try:
                loop = asyncio.get_event_loop()
                force = self.recover_requested
                self.recover_requested = False
                # register 完了までは録画・推論・新規 entry を再開しない
                # (register 失敗は接続失敗として扱い backoff 再試行)
                await loop.run_in_executor(
                    None, self.tokens.ensure_registered,
                    [c for c, _ in self.universe], force)
                async with websockets.connect(
                    self.ws_url, ping_interval=None, max_size=None,
                    open_timeout=15, close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self.connection_epoch += 1
                    log.info(f"WebSocket 接続確立: {self.ws_url} "
                             f"(epoch={self.connection_epoch})")
                    if first_connect:
                        first_connect = False
                        self._audit("registered")
                        if self.trader is not None:
                            self.trader.suspended = False
                    else:
                        self._on_registered()
                    backoff = 5
                    while not self.stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_S)
                        except asyncio.TimeoutError:
                            log.warning(f"{RECV_TIMEOUT_S:.0f}s 無受信 — 切断して再接続します")
                            break
                        self._handle_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self.stop_event.is_set():
                    break
                self.reconnects += 1
                self._on_disconnected()
                sleep_s = backoff + random.uniform(0.0, backoff * 0.5)  # 指数 backoff + jitter
                log.warning(f"WS 例外 → {sleep_s:.1f}s 後再接続 (reconnects={self.reconnects}): {e}")
                await asyncio.sleep(sleep_s)
                backoff = min(60, backoff * 2)
                continue
            if not self.stop_event.is_set():
                self.reconnects += 1
                self._on_disconnected()
                await asyncio.sleep(backoff + random.uniform(0.0, backoff * 0.5))
                backoff = min(60, backoff * 2)

    async def _close_ws(self):
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    # ── 1Hz 決定境界 ─────────────────────────────────────────────────────────
    async def boundary_loop(self):
        """整数秒境界を順に処理する。遅延時は取りこぼさず追いつく。"""
        if self.trader is None:
            return
        self._last_boundary = int(naive_epoch(datetime.now()))
        while not self.stop_event.is_set():
            now = naive_epoch(datetime.now())
            next_b = self._last_boundary + 1
            if now < next_b:
                await asyncio.sleep(min(next_b - now, 0.25))
                continue
            for t in range(next_b, int(math.floor(now)) + 1):
                try:
                    self.trader.on_boundary(float(t))
                except Exception as e:
                    log.error(f"境界 {t} 評価失敗: {e}")
                self._last_boundary = t

    # ── housekeeping ─────────────────────────────────────────────────────────
    async def housekeeping(self):
        while not self.stop_event.is_set():
            await asyncio.sleep(2.0)
            now = time.monotonic()
            try:
                if self.recorder.should_flush(now):
                    self.recorder.flush()
                if self.trader is not None:
                    self.trader.drain_decisions()
                    if self.outputs.should_flush(now):
                        self.outputs.flush()
            except Exception as e:
                log.error(f"flush 失敗: {e}")
            self.heartbeat.maybe_write(
                now, self.msgs_total,
                self.recorder.rows_written + len(self.recorder.buffer),
                self.trader.n_open_positions if self.trader else 0,
                self.trader.n_trades if self.trader else 0,
                self.reconnects)
            # 場中 0-msg ストール検知 → 回復 / rc=1 終了 (2026-07-10 事故対応)
            action = self.stall.check(now, in_market_hours(datetime.now().time()))
            if action == "recover":
                log.warning(f"場中 {STALL_RECOVER_S:.0f}s+ 無受信 — "
                            "token 再取得 + 再登録 + WS 再接続を実施")
                self.recover_requested = True
                await self._close_ws()
            elif action == "exit":
                log.error(f"場中 {STALL_EXIT_S:.0f}s+ 無受信 — 回復失敗。rc=1 で終了")
                self.exit_rc = 1
                self.stop_event.set()
                await self._close_ws()
                return
            # 稼働窓終了 / duration
            if not self.args.ignore_window:
                eh, em = (int(x) for x in self.args.end_hhmm.split(":"))
                if datetime.now().time() >= dtime(eh, em):
                    log.info(f"稼働窓終了 ({self.args.end_hhmm})。停止します。")
                    self.stop_event.set()
                    await self._close_ws()
                    return
            if self.deadline is not None and now >= self.deadline:
                log.info("指定 duration に到達。停止します。")
                self.stop_event.set()
                await self._close_ws()
                return

    async def run(self):
        self.stop_event = asyncio.Event()
        self.recorder.open()
        if self.outputs is not None:
            self.outputs.open()
            # クラッシュした前セッションの未終端 entry を unresolved 化
            n_crash = self.outputs.reconcile_previous_session()
            if n_crash:
                log.warning(f"前セッションの未終端 entry {n_crash} 件を unresolved 化 (crash_recovered)")
            self._audit("startup", day=self.day_str)
        if self.trader is not None:
            self.trader.suspended = True  # 初回 register 完了まで推論しない
        hk = asyncio.create_task(self.housekeeping())
        bl = asyncio.create_task(self.boundary_loop())
        try:
            await self.recv_forever()
        finally:
            self.stop_event.set()
            for task in (hk, bl):
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            try:
                self.recorder.flush()
            except Exception as e:
                log.error(f"最終録画 flush 失敗: {e}")
            self.recorder.close()
            if self.trader is not None:
                self.trader.finalize()
                self.outputs.write_summary({
                    "push_msgs_total": self.msgs_total,
                    "rows_recorded": self.recorder.rows_written,
                    "reconnects": self.reconnects,
                })
                self.outputs.close()
                log.info(f"paper summary → {self.outputs.summary_path} "
                         f"(trades={self.trader.n_trades})")
            log.info(f"録画終了: rows={self.recorder.rows_written} msgs={self.msgs_total}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="scalp-agent 統合ランタイム (板 PUSH 録画 + ペーパートレード・read-only)")
    ap.add_argument("--port", type=int, default=18080,
                    help="kabuステーション本体ポート (既定 18080=本番。PUSH は本番のみ実データ)")
    ap.add_argument("--universe", default=DEFAULT_UNIVERSE)
    ap.add_argument("--record-only", action="store_true",
                    help="録画のみ (ペーパートレード無効・モデル不要)")
    ap.add_argument("--duration-min", type=float, default=None, help="N 分で自己終了 (smoke 用)")
    ap.add_argument("--ignore-window", action="store_true", help="営業日/稼働窓ガードを無視 (smoke)")
    ap.add_argument("--end-hhmm", default="15:35", help="稼働窓終了時刻")
    args = ap.parse_args()

    args.api_password = os.environ.get("KABU_API_PASSWORD")
    if not args.api_password:
        log.error("env KABU_API_PASSWORD が未設定です (ハードコード禁止・env から供給)")
        return 2

    today = date.today()
    if not args.ignore_window and not is_business_day(today):
        log.info(f"{today} は非営業日 (土日祝/東証休場)。何もせず終了します。")
        return 0

    app = RuntimeApp(args)
    if not app.universe:
        log.error(f"universe が空です: {args.universe}")
        return 1
    log.info(f"universe: {len(app.universe)} 銘柄 / 録画 db={app.recorder.db_path} / "
             f"port={args.port} / paper={'OFF' if args.record_only else 'ON (calibration-only)'}")
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt — finally で summary/flush 済み")
    return app.exit_rc


if __name__ == "__main__":
    raise SystemExit(main())
