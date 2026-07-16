"""場中 0-msg ストール検知の純ロジック状態機械 (参照実装から移植・実績あり)。

2026-07-10 の token 喪失で終日 0-msg の録画欠損が実発生した事故への対処:
  - 場中 recover_after 秒 0-msg → "recover" (token 再取得 + 再登録 + WS 再接続)
  - 場中 exit_after 秒 0-msg → "exit" (rc=1 終了・再起動で新 token)
"""
from __future__ import annotations

from datetime import time as dtime

STALL_RECOVER_S = 300.0
STALL_EXIT_S = 600.0
STALL_RETRY_EVERY_S = 60.0


def in_market_hours(t: dtime) -> bool:
    """東証 継続セッション内か (09:00-11:30 / 12:30-15:30 JST・端点は半開区間)。"""
    return (dtime(9, 0) <= t < dtime(11, 30)) or (dtime(12, 30) <= t < dtime(15, 30))


_MARKET_WINDOWS_TOD = ((9 * 3600.0, 11 * 3600.0 + 1800.0),
                       (12 * 3600.0 + 1800.0, 15 * 3600.0 + 1800.0))


def market_overlap_seconds(t0: float, t1: float) -> float:
    """naive epoch 区間 [t0, t1] と取引時間の重なり秒数。

    切断区間が取引時間と重なったかの判定に使う (重なり 0 = 場外/昼休みのみの
    切断で、gap 汚染に数えない — DESIGN 2026-07-16)。
    """
    if t1 <= t0:
        return 0.0
    day_base = (t0 // 86400.0) * 86400.0
    total = 0.0
    # 日跨ぎはランタイム上あり得ないが、翌日分まで見て取りこぼさない
    for d in (day_base, day_base + 86400.0):
        for lo, hi in _MARKET_WINDOWS_TOD:
            total += max(0.0, min(t1, d + hi) - max(t0, d + lo))
    return total


class StallDetector:
    """check(mono, in_hours) の戻り値: None | "recover" | "exit"。

    - 場外: 常に None (アンカーをリセット)
    - 場中: 最終受信 (または場中入り) から recover_after 秒 0-msg → "recover"
      (以後 retry_every 秒ごとに再発火)、exit_after 秒 0-msg → "exit"
    """

    def __init__(self, recover_after: float = STALL_RECOVER_S,
                 exit_after: float = STALL_EXIT_S,
                 retry_every: float = STALL_RETRY_EVERY_S):
        self.recover_after = recover_after
        self.exit_after = exit_after
        self.retry_every = retry_every
        self.anchor = None        # monotonic: 最終受信 or 場中入り
        self.last_recover = None  # monotonic: 直近 recover 発火

    def on_msg(self, mono: float) -> None:
        self.anchor = mono
        self.last_recover = None

    def check(self, mono: float, in_hours: bool):
        if not in_hours:
            self.anchor = None
            self.last_recover = None
            return None
        if self.anchor is None:
            self.anchor = mono
            return None
        stalled = mono - self.anchor
        if stalled >= self.exit_after:
            return "exit"
        if stalled >= self.recover_after:
            if self.last_recover is None or (mono - self.last_recover) >= self.retry_every:
                self.last_recover = mono
                return "recover"
        return None
