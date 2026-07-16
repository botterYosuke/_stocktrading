"""runtime テスト用の合成板データ生成 (executable 部分列を直接作る)。"""
from datetime import datetime

import numpy as np
import pytest

DAY_BASE = (datetime(2026, 7, 16) - datetime(1970, 1, 1)).total_seconds()


def _synth_block(rng: np.random.Generator, start_tod: float, end_tod: float,
                 mid0: float = 2000.0) -> dict[str, np.ndarray]:
    """1 セッション分の executable 行 (ask > bid・ts 昇順) を合成する。"""
    ts = []
    t = DAY_BASE + start_tod
    end = DAY_BASE + end_tod
    while t < end:
        u = rng.random()
        if u < 0.55:
            dt = rng.uniform(0.03, 0.5)
        elif u < 0.80:
            dt = 0.0 if rng.random() < 0.5 else rng.uniform(0.5, 1.5)  # 同一秒バースト
        elif u < 0.97:
            dt = rng.uniform(2.0, 20.0)
        else:
            dt = rng.uniform(60.0, 420.0)  # median 窓を跨ぐ大ギャップ
        t += max(dt, 0.0001)
        if t < end:
            ts.append(t)
    n = len(ts)
    ts = np.asarray(ts)
    tick = 0.5
    mid = mid0 + np.cumsum(rng.choice([-tick, 0.0, 0.0, tick], size=n))
    spread = rng.choice([tick, 2 * tick, 3 * tick], size=n)
    bid1 = np.round((mid - spread / 2) / tick) * tick
    ask1 = bid1 + spread
    snap: dict[str, np.ndarray] = {"ts": ts, "bid_px_1": bid1, "ask_px_1": ask1}
    for i in range(2, 6):
        snap[f"bid_px_{i}"] = bid1 - (i - 1) * tick
        snap[f"ask_px_{i}"] = ask1 + (i - 1) * tick
    for i in range(1, 6):
        snap[f"bid_qty_{i}"] = rng.integers(100, 10000, size=n).astype(np.float64)
        snap[f"ask_qty_{i}"] = rng.integers(100, 10000, size=n).astype(np.float64)
    last = mid + rng.choice([-tick, 0.0, tick], size=n)
    last[rng.random(n) < 0.02] = np.nan  # last_px 欠損
    snap["last_px"] = last
    snap["volume"] = np.cumsum(rng.integers(0, 500, size=n)).astype(np.float64)
    return snap


def synth_exec_day(seed: int = 7, morning: tuple[float, float] = (9 * 3600 + 1800, 10 * 3600),
                   afternoon: tuple[float, float] | None = (14 * 3600 + 3000, 15 * 3600 + 600),
                   ) -> dict[str, np.ndarray]:
    """午前 + (14:55 を跨ぐ) 午後の executable 部分列。"""
    rng = np.random.default_rng(seed)
    blocks = [_synth_block(rng, *morning)]
    if afternoon is not None:
        blocks.append(_synth_block(rng, *afternoon, mid0=2000.0))
    return {k: np.concatenate([b[k] for b in blocks]) for k in blocks[0]}


def iter_rows(snap: dict[str, np.ndarray]):
    keys = list(snap.keys())
    for i in range(len(snap["ts"])):
        yield {k: float(snap[k][i]) for k in keys}


@pytest.fixture
def synth_snap():
    return synth_exec_day()
