"""StallDetector (参照実装 selftest の移植・2026-07-10 事故形の固定)。"""
from datetime import time as dtime

from scalp_agent.runtime.stall import StallDetector, in_market_hours


def test_msgs_flowing_no_action():
    d = StallDetector()
    t = 1000.0
    for _ in range(20):
        d.on_msg(t)
        t += 30.0
        assert d.check(t, True) is None


def test_recover_fires_at_300s_and_refires_after_retry_every():
    d = StallDetector()
    t0 = 2000.0
    d.on_msg(t0)
    assert d.check(t0 + 299.0, True) is None
    assert d.check(t0 + 300.0, True) == "recover"
    assert d.check(t0 + 330.0, True) is None  # retry_every=60 未満
    assert d.check(t0 + 361.0, True) == "recover"
    assert d.check(t0 + 600.0, True) == "exit"


def test_msg_resets_stall():
    d = StallDetector()
    d.on_msg(0.0)
    assert d.check(310.0, True) == "recover"
    d.on_msg(320.0)
    assert d.check(600.0, True) is None
    assert d.check(620.0, True) == "recover"


def test_out_of_hours_ignored_and_anchor_resets():
    d = StallDetector()
    d.on_msg(0.0)
    assert d.check(10000.0, False) is None
    assert d.check(10001.0, True) is None  # 場中復帰でアンカー再設定
    assert d.check(10301.0, True) == "recover"


def test_dead_feed_from_start_reaches_exit():
    d = StallDetector()
    d.check(0.0, True)
    assert d.check(600.0, True) == "exit"


def test_market_hours_boundaries():
    assert in_market_hours(dtime(9, 0)) and not in_market_hours(dtime(8, 59))
    assert in_market_hours(dtime(11, 29, 59)) and not in_market_hours(dtime(11, 30))
    assert in_market_hours(dtime(12, 30)) and not in_market_hours(dtime(12, 29, 59))
    assert in_market_hours(dtime(15, 29, 59)) and not in_market_hours(dtime(15, 30))
