"""トリプルバリアの単体テスト (2026-07-16 確定仕様の固定)。

- エントリ = 決定 PUSH の厳密な次 PUSH
- TP/SL 約定 = トリガーの厳密な次 PUSH
- Timeout/EOD = 時計時刻より後の最初の PUSH (もう 1 PUSH 待たない)
- SL は entry 時の清算可能な対向 best 基準 (mult≤2 即時 SL 退化の回帰テスト)
- ラベルはスリッページで変えない
"""
import numpy as np
import pytest

from scalp_agent.labels import (
    EXIT_EOD,
    EXIT_NONE,
    EXIT_SL,
    EXIT_TIMEOUT,
    EXIT_TP,
    barrier_outcomes_grid,
    labels_from_outcomes,
)

MORNING = 9 * 3600.0  # tod 09:00 (naive epoch 秒の日内換算)
FORCE = 14 * 3600.0 + 55 * 60.0


def run_grid(ts, bid, ask, didx, dts, mults=(3.0,), horizons=(10.0,)):
    ts = np.asarray(ts, dtype=np.float64)
    return barrier_outcomes_grid(
        ts, np.mod(ts, 86400.0),
        np.asarray(bid, dtype=np.float64), np.asarray(ask, dtype=np.float64),
        np.asarray(didx, dtype=np.int64), np.asarray(dts, dtype=np.float64),
        mults=mults, horizons_s=horizons,
        entry_max_latency_s=2.0, force_close_tod=FORCE,
    )


def test_long_tp_first_touch_and_next_push_fill():
    b = MORNING + 60.0
    ts = [b + x for x in (0.0, 0.5, 1.0, 1.5, 2.0, 2.5)]
    #        dec    entry  --     TPtrig fill   --
    bid = [100.0, 100.0, 100.2, 101.6, 101.4, 101.4]
    ask = [100.5, 100.5, 100.7, 102.1, 101.9, 101.9]
    out = run_grid(ts, bid, ask, didx=[0], dts=[b + 0.2])
    lo = out[(10.0, 3.0)]["long"]
    # s=0.5, Δ=1.0 → TP: bid≥101.5 (trigger 行 3)、約定は次 PUSH (行 4)
    assert lo["reason"][0] == EXIT_TP
    assert lo["entry_idx"][0] == 1          # 決定 PUSH の厳密な次
    assert lo["exit_idx"][0] == 4           # トリガーの厳密な次
    assert lo["tp_trigger_ts"][0] == ts[3]
    y, valid = labels_from_outcomes(lo, out[(10.0, 3.0)]["short"])
    assert valid[0] and y[0] == 1


def test_long_sl_when_bid_breaks_entry_bid_minus_delta():
    b = MORNING + 60.0
    ts = [b + x for x in (0.0, 0.5, 1.0, 1.5, 2.0, 2.5)]
    bid = [100.0, 100.0, 99.5, 98.9, 98.5, 98.5]
    ask = [100.5, 100.5, 100.0, 99.4, 99.2, 99.2]
    out = run_grid(ts, bid, ask, didx=[0], dts=[b + 0.2])
    lo = out[(10.0, 3.0)]["long"]
    # Δ=1.0 → SL: bid ≤ bid0−Δ = 99.0 (trigger 行 3)、約定 行 4
    assert lo["reason"][0] == EXIT_SL
    assert lo["exit_idx"][0] == 4


def test_timeout_fills_at_first_push_after_deadline_not_one_more():
    b = MORNING + 60.0
    n = 20
    ts = [b + 0.5 * i for i in range(n)]
    bid = [100.0] * n
    ask = [100.5] * n
    out = run_grid(ts, bid, ask, didx=[0], dts=[b + 0.2], horizons=(3.0,))
    lo = out[(3.0, 3.0)]["long"]
    # entry 行 1 (ts=b+0.5)、deadline=b+3.5 → 最初の ts>deadline は行 8 (b+4.0)
    assert lo["reason"][0] == EXIT_TIMEOUT
    assert lo["exit_idx"][0] == 8


@pytest.mark.parametrize("mult", [1.5, 2.0])
def test_no_instant_sl_for_small_mult_regression(mult):
    """SL アンカー修正の回帰テスト: 動かない板で mult≤2 が即時 SL にならない。"""
    b = MORNING + 60.0
    n = 20
    ts = [b + 0.5 * i for i in range(n)]
    bid = [100.0] * n
    ask = [100.5] * n
    out = run_grid(ts, bid, ask, didx=[0], dts=[b + 0.2], mults=(mult,), horizons=(3.0,))
    for side in ("long", "short"):
        assert out[(3.0, mult)][side]["reason"][0] == EXIT_TIMEOUT


def test_eod_force_close_takes_priority_over_timeout():
    b = FORCE - 10.0  # 14:54:50
    ts = [b + 2.0 * i for i in range(8)]  # 行 5 = 14:55:00, 行 6 = 14:55:02
    bid = [100.0] * 8
    ask = [100.5] * 8
    out = run_grid(ts, bid, ask, didx=[0], dts=[b + 1.0], horizons=(60.0,))
    lo = out[(60.0, 3.0)]["long"]
    # 最初の tod > 14:55 は行 6 (14:55:02)。timeout (b+2+60) より先 → EOD 優先
    assert lo["reason"][0] == EXIT_EOD
    assert lo["exit_idx"][0] == 6


def test_label_not_changed_by_slippage_after_tp():
    """TP トリガー後に次 PUSH が崩れて net が悪化してもラベルは +1 のまま。"""
    b = MORNING + 60.0
    ts = [b + x for x in (0.0, 0.5, 1.0, 1.5, 2.0)]
    bid = [100.0, 100.0, 101.6, 99.0, 99.0]   # 行 2 TP trigger → 行 3 fill が暴落
    ask = [100.5, 100.5, 102.1, 99.5, 99.5]
    out = run_grid(ts, bid, ask, didx=[0], dts=[b + 0.2])
    cell = out[(10.0, 3.0)]
    y, valid = labels_from_outcomes(cell["long"], cell["short"])
    assert valid[0] and y[0] == 1
    assert cell["long"]["exit_idx"][0] == 3
    # 実現 exit は崩れた bid=99.0 → net 負。ラベルは変更しない
    assert bid[int(cell["long"]["exit_idx"][0])] < 100.5


def test_entry_skipped_when_next_push_is_stale():
    b = MORNING + 60.0
    ts = [b, b + 5.0, b + 5.5]  # 次 PUSH が 5 秒後 (> 2s ガード)
    bid = [100.0, 100.0, 100.0]
    ask = [100.5, 100.5, 100.5]
    out = run_grid(ts, bid, ask, didx=[0], dts=[b])
    cell = out[(10.0, 3.0)]
    assert cell["long"]["reason"][0] == EXIT_NONE
    y, valid = labels_from_outcomes(cell["long"], cell["short"])
    assert not valid[0]


def test_mult_of_one_is_rejected():
    b = MORNING + 60.0
    ts = np.array([b, b + 0.5])
    with pytest.raises(ValueError):
        run_grid(ts, [100.0, 100.0], [100.5, 100.5], [0], [b], mults=(1.0,))


def test_first_touch_scan_excludes_rows_at_or_before_entry():
    """決定行のスパイクはバリア判定に使わない (走査は entry 約定 PUSH の次から)。"""
    b = MORNING + 60.0
    n = 12
    ts = [b + 0.5 * i for i in range(n)]
    bid = [200.0] + [100.0] * (n - 1)   # 決定行 (行 0) だけ TP 閾値超え
    ask = [200.5] + [100.5] * (n - 1)
    out = run_grid(ts, bid, ask, didx=[0], dts=[b + 0.2], horizons=(3.0,))
    lo = out[(3.0, 3.0)]["long"]
    assert lo["reason"][0] == EXIT_TIMEOUT   # 行 0 のスパイクで TP しない


def test_long_short_quote_anchor_symmetry():
    """価格系列を定数 C を軸に鏡映すると long と short の結果が入れ替わる。"""
    b = MORNING + 60.0
    ts = [b + x for x in (0.0, 0.5, 1.0, 1.5, 2.0, 2.5)]
    bid = [100.0, 100.0, 100.2, 101.6, 101.4, 101.4]
    ask = [100.5, 100.5, 100.7, 102.1, 101.9, 101.9]
    c = 300.0
    bid_m = [c - a for a in ask]   # 鏡映: bid' = C − ask, ask' = C − bid
    ask_m = [c - x for x in bid]
    out = run_grid(ts, bid, ask, didx=[0], dts=[b + 0.2])
    out_m = run_grid(ts, bid_m, ask_m, didx=[0], dts=[b + 0.2])
    lo, sh_m = out[(10.0, 3.0)]["long"], out_m[(10.0, 3.0)]["short"]
    assert lo["reason"][0] == EXIT_TP and sh_m["reason"][0] == EXIT_TP
    assert lo["exit_idx"][0] == sh_m["exit_idx"][0]
    assert lo["entry_idx"][0] == sh_m["entry_idx"][0]


def test_label_tie_both_tp_same_timestamp_is_zero():
    """双方 TP 先着・同一 timestamp → 0 (labels_from_outcomes の直接単体)。"""
    def mk(reason, tp_ts):
        return {
            "reason": np.array([reason], dtype=np.int8),
            "tp_trigger_ts": np.array([tp_ts]),
        }
    y, valid = labels_from_outcomes(mk(EXIT_TP, 100.0), mk(EXIT_TP, 100.0))
    assert valid[0] and y[0] == 0
    y, _ = labels_from_outcomes(mk(EXIT_TP, 99.0), mk(EXIT_TP, 100.0))
    assert y[0] == 1
    y, _ = labels_from_outcomes(mk(EXIT_TP, 100.0), mk(EXIT_TP, 99.0))
    assert y[0] == -1
    y, valid = labels_from_outcomes(mk(EXIT_NONE, np.nan), mk(EXIT_TP, 99.0))
    assert not valid[0] and y[0] == 0
