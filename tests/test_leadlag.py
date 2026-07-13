"""L1-L15 selftests for the Family 3 Phase A signal study.

Each test maps to one lettered requirement of the frozen task
(`cross-name-lead-lag-scalp-phase-A-task.md` section 6). The numbers below are
hand-checkable on purpose: the boundary fixtures use dyadic rationals (1032,
4.03125, ...) so that "exactly 2x the spread" is exactly representable in IEEE
double and the strict inequality can be tested for real rather than approximately.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import duckdb
import pytest

from stocktrading import leadlag
from stocktrading.config import Settings
from stocktrading.leadlag import (
    ALL_HORIZONS,
    BRIDGE_NO_ENTRY,
    BRIDGE_OK,
    DECISION_HORIZONS,
    MIN_EVENTS_PER_PAIR,
    MIN_SESSIONS,
    PAIRS,
    REASON_NO_M0,
    REASON_OK,
    REPORT_HORIZONS,
    REQUIRED_SYMBOLS,
    Cell,
    RawSnap,
    assert_no_performance_fields,
    build_pair_events,
    build_series,
    compute_bridge_outcome,
    compute_screen_outcome,
    decide,
    evaluate_trigger,
    find_onsets,
    first_at_or_after,
    integrity_check,
    latest_at,
    pair_key,
    run_feasibility,
    write_events_parquet,
)
from stocktrading.maker import CONTINUOUS_SIGN

DAY = date(2026, 7, 13)
BASE = datetime.combine(DAY, time(10, 0, 0))
SPECIAL_QUOTE = "0107"
HALTED = "0120"


def snap(
    offset: float,
    bid: float,
    ask: float,
    *,
    bid_sign: str | None = CONTINUOUS_SIGN,
    ask_sign: str | None = CONTINUOUS_SIGN,
    base: datetime = BASE,
) -> RawSnap:
    return RawSnap(
        ts=base + timedelta(seconds=offset),
        bid=bid,
        ask=ask,
        bid_sign=bid_sign,
        ask_sign=ask_sign,
    )


def series(code: str, raws: list[RawSnap]):
    return build_series(code, DAY, raws)


# --- L1 -------------------------------------------------------------------
# 2-second return vs the strict `> 2 * spread` boundary: below / equal / above.


def test_L1_trigger_strict_boundary():
    # mid_lag2 = 1024, mid_now = 1032 -> ret2 = 129/128 - 1 = 0.0078125 exactly.
    # spread 4.03125 -> spread_ret = 1/256 exactly -> 2 * spread_ret = 0.0078125.
    assert evaluate_trigger(1032.0, 1024.0, 4.03125)[2] == 0.0078125
    assert evaluate_trigger(1032.0, 1024.0, 4.03125)[3] * 2 == 0.0078125

    below, _, _, _ = evaluate_trigger(1032.0, 1024.0, 4.0625)  # threshold above ret2
    equal, _, _, _ = evaluate_trigger(1032.0, 1024.0, 4.03125)  # exactly equal
    above, _, _, _ = evaluate_trigger(1032.0, 1024.0, 4.0)  # threshold below ret2
    assert below is False
    assert equal is False, "equality must NOT trigger: the spec says strictly greater"
    assert above is True

    # Direction is the sign of the mid change, both ways.
    assert evaluate_trigger(1032.0, 1024.0, 4.0)[1] == 1
    assert evaluate_trigger(1016.0, 1024.0, 4.0)[1] == -1

    # And the same boundary survives the full Series path (mid/spread are exact).
    s = series("L", [snap(0, 1023.0, 1025.0), snap(2, 1029.984375, 1034.015625)])
    assert s.mid == (1024.0, 1032.0)
    assert s.spread[1] == 4.03125
    onsets, stats = find_onsets(s)
    assert onsets == []  # equality does not fire
    assert stats.n_evaluable_snaps == 1
    assert stats.raw_trigger_snaps == 0


# --- L2 -------------------------------------------------------------------
# Onset only on False->True; a sustained trigger is one event; re-arm needs a
# False; the lunch break resets the state machine.


def test_L2_onset_continuation_rearm_and_session_reset():
    raws = [snap(0, 999.0, 1001.0)]  # mid 1000, quiet
    raws.append(snap(1, 999.0, 1001.0))
    raws.append(snap(2, 999.0, 1001.0))
    # t=3: mid 1010 vs mid_lag2 (t=1) 1000 -> ret2 1% >> 2*spread_ret 0.2% -> ONSET
    raws.append(snap(3, 1009.0, 1011.0))
    # t=4: mid 1020 vs t=2 mid 1000 -> still True -> continuation, NOT a new event
    raws.append(snap(4, 1019.0, 1021.0))
    # t=5: mid 1020 vs t=3 mid 1010 -> ~1% still True -> continuation
    raws.append(snap(5, 1019.0, 1021.0))
    # t=6: mid 1020 vs t=4 mid 1020 -> ret2 = 0 -> False (disarm)
    raws.append(snap(6, 1019.0, 1021.0))
    raws.append(snap(7, 1019.0, 1021.0))
    # t=8: mid 1040 vs t=6 mid 1020 -> True again -> SECOND onset (re-armed)
    raws.append(snap(8, 1039.0, 1041.0))

    onsets, stats = find_onsets(series("L", raws))
    assert [round(o.t - 36000.0) for o in onsets] == [3, 8]
    assert stats.onset_events == 2
    assert stats.raw_trigger_snaps > stats.onset_events  # inflation is real
    assert stats.event_inflation_ratio == stats.raw_trigger_snaps / 2

    # Lunch: a trigger still True at the morning close must not suppress the
    # afternoon's first onset, and the 2s lookback must not reach across.
    am = datetime.combine(DAY, time(11, 28, 0))
    pm = datetime.combine(DAY, time(12, 31, 0))
    raws = [
        snap(0, 999.0, 1001.0, base=am),
        snap(2, 999.0, 1001.0, base=am),
        snap(4, 1029.0, 1031.0, base=am),  # morning onset, still True at the bell
        snap(0, 1029.0, 1031.0, base=pm),
        snap(2, 1029.0, 1031.0, base=pm),
        snap(4, 1059.0, 1061.0, base=pm),  # afternoon onset, own state machine
    ]
    onsets, _ = find_onsets(series("L", raws))
    assert [o.segment for o in onsets] == [0, 1]
    # The afternoon's own first two seconds have no reference -> undefined, not a
    # trigger, so nothing fires off a stale morning mid.
    assert all(o.leader_mid_lag2 in (1000.0, 1030.0) for o in onsets)


# --- L3 -------------------------------------------------------------------
# The follower reference is never in the future.


def test_L3_no_future_leak_in_m0():
    t = 36000.0 + 10.0  # 10:00:10
    follower = series(
        "F",
        [
            snap(9.5, 999.0, 1001.0),  # mid 1000 -> the only legal m0
            snap(10.001, 1999.0, 2001.0),  # mid 2000, 1ms AFTER t -- a leak if used
            snap(12.0, 1999.0, 2001.0),
        ],
    )
    i0 = latest_at(follower, t, 0)
    assert i0 == 0
    assert follower.mid[i0] == 1000.0
    assert follower.mid[i0] != 2000.0, "m0 must not be taken from a row after t"
    # latest_at can never hand back a row in the future, at any target.
    for target in (t, t - 0.4, t + 1.999):
        i = latest_at(follower, target, 0)
        if i is not None:
            assert follower.secs[i] <= target

    # Same fixture through the event builder: a future row must not become m0.
    leader = series("L", [snap(8, 999.0, 1001.0), snap(10, 1029.0, 1031.0)])
    onsets, _ = find_onsets(leader)
    assert len(onsets) == 1
    rows = build_pair_events(leader, follower, onsets)
    assert rows[0].m0 == 1000.0
    assert rows[0].m0_age_ms == pytest.approx(500.0)


# --- L4 -------------------------------------------------------------------
# +2s / +5s latest-prior lookup, the 2s gap guard, and missing horizons.


def test_L4_horizon_lookup_gap_guard_and_missing():
    t = 36000.0 + 10.0
    follower = series(
        "F",
        [
            snap(10.0, 999.0, 1001.0),  # m0 = 1000 (same tick as t: allowed, <= t)
            snap(11.9, 1009.0, 1011.0),  # m2: latest <= t+2, age 0.1s -> 1010
            snap(20.0, 1019.0, 1021.0),  # far past t+5
        ],
    )
    seg = 0
    assert follower.mid[latest_at(follower, t + 2.0, seg)] == 1010.0
    # t+5: the latest prior row is at t+1.9, i.e. 3.1s stale -> missing, NOT 1010.
    assert latest_at(follower, t + 5.0, seg) is None

    leader = series("L", [snap(8, 999.0, 1001.0), snap(10, 1029.0, 1031.0)])
    onsets, _ = find_onsets(leader)
    rows = build_pair_events(leader, follower, onsets)
    row = rows[0]
    assert row.mh[2.0] == 1010.0
    assert row.mh[5.0] is None
    assert row.screen_edge[5.0] is None
    assert row.bridge_edge[5.0] is None
    assert row.exclusion_reason == REASON_OK  # a missing horizon is not an exclusion


# --- L5 -------------------------------------------------------------------
# Hand-computed drift and half-spread edge, in both directions.


def test_L5_screen_drift_and_edge_by_hand():
    # BUY: +10 bps drift, half-spread cost 10 bps -> edge exactly 0.
    drift, edge = compute_screen_outcome(1, 1000.0, 2.0, 1001.0)
    assert drift == pytest.approx(10.0)
    assert edge == pytest.approx(0.0, abs=1e-9)

    # SELL: price falls, so the SIGNED drift is positive; same cost, same edge.
    drift, edge = compute_screen_outcome(-1, 1000.0, 2.0, 999.0)
    assert drift == pytest.approx(10.0)
    assert edge == pytest.approx(0.0, abs=1e-9)

    # SELL that goes the wrong way: signed drift is negative and the cost is paid.
    drift, edge = compute_screen_outcome(-1, 1000.0, 2.0, 1001.0)
    assert drift == pytest.approx(-10.0)
    assert edge == pytest.approx(-20.0)

    # A wider follower spread makes the same drift unprofitable.
    _, edge = compute_screen_outcome(1, 1000.0, 4.0, 1001.0)
    assert edge == pytest.approx(-10.0)


# --- L6 -------------------------------------------------------------------
# Auction / halt / crossed / locked / zero / out-of-window snapshots are dropped.


def test_L6_hygiene_excludes_non_continuous_books():
    raws = [
        snap(0, 999.0, 1001.0),  # keep
        snap(1, 999.0, 1001.0, bid_sign=SPECIAL_QUOTE),  # special quote
        snap(2, 999.0, 1001.0, ask_sign=HALTED),  # halted
        snap(3, 1002.0, 1001.0),  # crossed
        snap(4, 1000.0, 1000.0),  # locked
        snap(5, 0.0, 1001.0),  # zero book
        snap(6, 999.0, 1001.0, bid_sign=None, ask_sign=None),  # pre-open / no sign
        snap(7, 999.0, 1001.0),  # keep
    ]
    s = series("X", raws)
    assert len(s) == 2
    assert s.n_raw == 8

    lunch = datetime.combine(DAY, time(11, 45, 0))
    close_auction = datetime.combine(DAY, time(15, 25, 0))
    assert len(series("X", [snap(0, 999.0, 1001.0, base=lunch)])) == 0
    assert len(series("X", [snap(0, 999.0, 1001.0, base=close_auction)])) == 0


# --- L7 -------------------------------------------------------------------
# The 32 directed pairs are exactly the canonical enumeration; a missing symbol
# yields a missing observation, never a fabricated pair.


def test_L7_pair_universe_matches_canonical_enumeration():
    expected = {
        *[("1570", f) for f in ("9984", "9983", "8035", "6857", "6920", "6146", "7203", "6758")],
        *[("1568", f) for f in ("8306", "8316", "8411", "7203", "6501", "8058")],
        ("5801", "5802"), ("5802", "5801"),
        ("5801", "5803"), ("5803", "5801"),
        ("5802", "5803"), ("5803", "5802"),
        *[("8306", f) for f in ("8316", "8411", "7186", "7182")],
        ("7011", "7012"), ("7011", "7013"),
        *[("8035", f) for f in ("6857", "6920", "6146", "285A")],
        ("6857", "6920"),
        ("6501", "6503"),
    }
    assert set(PAIRS) == expected
    assert len(PAIRS) == 32, "the enumeration yields 32, not the doc's summary '28'"
    assert len(set(PAIRS)) == len(PAIRS), "no duplicate directed pairs"
    assert len(REQUIRED_SYMBOLS) == 25
    assert set(REQUIRED_SYMBOLS) == {c for pair in PAIRS for c in pair}


def test_L7_missing_follower_is_a_gap_not_a_fabricated_pair():
    leader = series("L", [snap(8, 999.0, 1001.0), snap(10, 1029.0, 1031.0)])
    onsets, _ = find_onsets(leader)
    empty_follower = series("F", [])
    rows = build_pair_events(leader, empty_follower, onsets)
    assert len(rows) == len(onsets)
    assert all(r.exclusion_reason == REASON_NO_M0 for r in rows)
    assert all(r.m0 is None and r.m_entry_05 is None for r in rows)
    assert all(not r.usable for r in rows)


# --- L8 -------------------------------------------------------------------
# Power floor (5 sessions / 200 events) and the 4-of-5 sign rule at the boundary.


def cell(
    pair: str = "1570->9984",
    horizon: float = 2.0,
    statistic: str = "screen",
    *,
    n_events: int = 200,
    n_days: int = 5,
    med: float | None = 1.0,
    sign_days: int = 4,
) -> Cell:
    return Cell(
        pair=pair,
        horizon=horizon,
        statistic=statistic,
        n_events=n_events,
        n_days=n_days,
        overall_median=med,
        daily_medians={},
        sign_days=sign_days,
        required_sign_days=4,
    )


def _cell(n_events: int, n_days: int, med: float, sign_days: int, stat="screen") -> Cell:
    return cell(
        statistic=stat, n_events=n_events, n_days=n_days, med=med, sign_days=sign_days
    )


def test_L8_power_floor_and_sign_consistency_boundaries():
    assert _cell(MIN_EVENTS_PER_PAIR, MIN_SESSIONS, 1.0, 4).powered
    assert not _cell(MIN_EVENTS_PER_PAIR - 1, MIN_SESSIONS, 1.0, 4).powered
    assert not _cell(MIN_EVENTS_PER_PAIR, MIN_SESSIONS - 1, 1.0, 4).powered

    assert _cell(200, 5, 1.0, 4).passes  # 4 of 5: the boundary passes
    assert not _cell(200, 5, 1.0, 3).passes  # 3 of 5: fails
    assert not _cell(200, 5, -1.0, 5).passes  # negative median: fails
    assert not _cell(200, 5, 0.0, 5).passes  # zero median: fails ("<= 0" kills)
    assert not _cell(199, 5, 1.0, 5).passes  # under-powered cannot pass

    # Under-powered everywhere -> WAIT_DATA, never KILL.
    assert decide([_cell(10, 1, 5.0, 1), _cell(10, 1, 5.0, 1, stat="bridge")]) == "WAIT_DATA"
    # Powered but no positive screen cell -> KILL.
    assert decide([_cell(200, 5, -1.0, 5), _cell(200, 5, 9.0, 5, stat="bridge")]) == "KILL"


# --- L9 -------------------------------------------------------------------
# Cross-name arrival order at identical timestamps cannot change the result.


def test_L9_same_timestamp_arrival_order_is_irrelevant():
    leader_raws = [snap(8, 999.0, 1001.0), snap(10, 1029.0, 1031.0)]
    follower_raws = [snap(10, 999.0, 1001.0), snap(11.0, 1004.0, 1006.0), snap(12.0, 1009.0, 1011.0)]

    # The leader's 10.0s row and the follower's 10.0s row share a timestamp, and
    # the recorder does not guarantee which lands first. Build both arrival orders
    # by stable-sorting on time with opposite tie-breaks: each symbol's own rows
    # stay chronological, only the cross-name interleaving flips.
    merged = [("L", r) for r in leader_raws] + [("F", r) for r in follower_raws]
    stream = sorted(merged, key=lambda x: (x[1].ts, x[0]))
    reversed_stream = sorted(merged, key=lambda x: (x[1].ts, x[0]), reverse=True)
    reversed_stream.sort(key=lambda x: x[1].ts)  # stable: ties now flipped
    assert [c for c, _ in stream] != [c for c, _ in reversed_stream]

    def events(rows):
        lead = series("L", [r for code, r in rows if code == "L"])
        foll = series("F", [r for code, r in rows if code == "F"])
        onsets, _ = find_onsets(lead)
        return build_pair_events(lead, foll, onsets)

    a = events(stream)
    b = events(reversed_stream)
    assert len(a) == 1
    assert [(r.m0, r.mh[2.0], r.screen_edge[2.0], r.m_entry_05) for r in a] == [
        (r.m0, r.mh[2.0], r.screen_edge[2.0], r.m_entry_05) for r in b
    ]


# --- L10 ------------------------------------------------------------------
# Static scan: no network, live API, credential or order path in the new code.


def test_L10_no_network_or_order_path_in_new_code():
    # Tokens are assembled rather than written out, so this list cannot flag
    # itself if the scan is ever widened to include this file.
    forbidden = [
        "requests", "urllib", "httpx", "aiohttp", "websocket", "socket",
        "kabusapi", "wandb", "boto3",
        "submit_" + "order", "send_" + "order", "place_" + "order", "cancel_" + "order",
        "api_" + "key", "access_" + "token", "Authorization", "Bearer",
    ]
    for module in (leadlag,):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for tok in forbidden:
            assert tok not in source, f"{tok!r} must not appear in {module.__name__}"


# --- L11 ------------------------------------------------------------------
# BRIDGE entry is the FIRST row at or after t+0.5s; later than t+1.5s is missing;
# m0 and same-tick rows are never substituted for it.


def test_L11_bridge_entry_is_first_row_after_latency():
    t = 36000.0 + 10.0
    leader = series("L", [snap(8, 999.0, 1001.0), snap(10, 1029.0, 1031.0)])
    onsets, _ = find_onsets(leader)

    follower = series(
        "F",
        [
            snap(9.9, 999.0, 1001.0),  # m0 = 1000
            snap(10.0, 1999.0, 2001.0),  # same tick as t -- must NOT be the entry
            snap(10.2, 2999.0, 3001.0),  # before t+0.5 -- must NOT be the entry
            snap(10.6, 1099.0, 1101.0),  # first row >= t+0.5 -> THE entry (mid 1100)
            snap(10.9, 1199.0, 1201.0),
            snap(12.0, 1299.0, 1301.0),
        ],
    )
    assert first_at_or_after(follower, t + 0.5, t + 1.5, 0) == 3
    row = build_pair_events(leader, follower, onsets)[0]
    assert row.m_entry_05 == 1100.0
    assert row.m_entry_05 != row.m0
    assert row.entry_age_ms == pytest.approx(600.0)
    assert row.bridge_reason == BRIDGE_OK

    # Entry later than t+1.5s -> BRIDGE missing, and m0 is NOT used instead.
    late = series("F", [snap(9.9, 999.0, 1001.0), snap(11.8, 1099.0, 1101.0)])
    row = build_pair_events(leader, late, onsets)[0]
    assert row.bridge_reason == BRIDGE_NO_ENTRY
    assert row.m_entry_05 is None
    assert row.m0 == 1000.0  # the screen still has its reference
    assert all(row.bridge_edge[h] is None for h in ALL_HORIZONS)


def test_L11_bridge_exit_may_not_precede_entry():
    t = 36000.0 + 10.0
    leader = series("L", [snap(8, 999.0, 1001.0), snap(10, 1029.0, 1031.0)])
    onsets, _ = find_onsets(leader)
    # Entry lands at t+1.4; the newest row at or before t+2 is that same row, so
    # the BRIDGE is a zero-hold trip (allowed). But m2's row must never be older
    # than the entry -- here we push the entry past t+2 is impossible, so instead
    # check the same-row case is kept and the screen still uses its own m0 row.
    follower = series("F", [snap(9.9, 999.0, 1001.0), snap(11.4, 1009.0, 1011.0)])
    row = build_pair_events(leader, follower, onsets)[0]
    assert row.m_entry_05 == 1010.0
    assert row.mh[2.0] == 1010.0  # same row serves as exit: drift 0, cost a full spread
    assert row.bridge_drift[2.0] == pytest.approx(0.0)
    assert row.bridge_edge[2.0] == pytest.approx(-2.0 / 1010.0 * 1e4)
    # The screen, which entered at m0 = 1000, sees the same move as a gain.
    assert row.screen_drift[2.0] == pytest.approx(100.0, rel=1e-3)


# --- L12 ------------------------------------------------------------------
# BRIDGE full-spread arithmetic, and the frozen decision matrix.


def test_L12_bridge_charges_a_full_round_trip_spread():
    # +20 bps of drift from the entry mid, minus a 20 bps full spread -> 0.
    drift, edge = compute_bridge_outcome(1, 1000.0, 2.0, 1002.0)
    assert drift == pytest.approx(20.0)
    assert edge == pytest.approx(0.0)
    # The screen would have called the same trip a 10 bps winner: the BRIDGE is
    # strictly the more expensive of the two by exactly a half spread.
    _, screen_edge = compute_screen_outcome(1, 1000.0, 2.0, 1002.0)
    assert screen_edge - edge == pytest.approx(10.0)

    drift, edge = compute_bridge_outcome(-1, 1000.0, 2.0, 990.0)
    assert drift == pytest.approx(100.0)
    assert edge == pytest.approx(80.0)


def test_L12_decision_matrix():
    def cells(screen_ok: bool, bridge_ok: bool) -> list[Cell]:
        return [
            _cell(200, 5, 3.0 if screen_ok else -3.0, 5 if screen_ok else 0),
            _cell(200, 5, 3.0 if bridge_ok else -3.0, 5 if bridge_ok else 0, stat="bridge"),
        ]

    assert decide(cells(screen_ok=True, bridge_ok=True)) == "PASS_CANDIDATE"
    assert decide(cells(screen_ok=True, bridge_ok=False)) == "PASS_SCREEN_ONLY"
    assert decide(cells(screen_ok=False, bridge_ok=True)) == "KILL"
    assert decide(cells(screen_ok=False, bridge_ok=False)) == "KILL"

    # A BRIDGE pass on a DIFFERENT cell than the screen pass does not qualify:
    # Phase B needs the same pair AND horizon to clear both.
    mixed = [
        cell("A->B", 2.0, "screen", med=3.0, sign_days=5),
        cell("A->B", 2.0, "bridge", med=-3.0, sign_days=0),
        cell("C->D", 5.0, "screen", med=-3.0, sign_days=0),
        cell("C->D", 5.0, "bridge", med=3.0, sign_days=5),
    ]
    assert decide(mixed) == "PASS_SCREEN_ONLY"


# --- L13 ------------------------------------------------------------------
# +30s / +60s are reported and cannot change the decision.


def test_L13_report_only_horizons_never_decide():
    assert DECISION_HORIZONS == (2.0, 5.0)
    assert REPORT_HORIZONS == (30.0, 60.0)
    assert ALL_HORIZONS == (2.0, 5.0, 30.0, 60.0)

    # 2s and 5s fail; 30s and 60s pass gloriously. The family still dies.
    losing = [
        cell("A->B", h, stat, med=-4.0, sign_days=0)
        for h in DECISION_HORIZONS
        for stat in ("screen", "bridge")
    ]
    rescuing = [
        cell("A->B", h, stat, n_events=5000, med=40.0, sign_days=5)
        for h in REPORT_HORIZONS
        for stat in ("screen", "bridge")
    ]
    assert decide(losing + rescuing) == "KILL"

    # And they cannot manufacture a pass out of an under-powered decision set.
    thin = [
        cell("A->B", h, s, n_events=10, n_days=1, med=-4.0, sign_days=0)
        for h in DECISION_HORIZONS
        for s in ("screen", "bridge")
    ]
    assert decide(thin + rescuing) == "WAIT_DATA"


# --- L14 ------------------------------------------------------------------
# Feasibility mode calls no outcome function and emits no outcome column.


def test_L14_feasibility_never_computes_an_outcome(monkeypatch):
    def boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("feasibility mode called a performance function")

    monkeypatch.setattr(leadlag, "compute_screen_outcome", boom)
    monkeypatch.setattr(leadlag, "compute_bridge_outcome", boom)

    leader_raws = [snap(8, 999.0, 1001.0), snap(10, 1029.0, 1031.0)]
    follower_raws = [snap(9.9, 999.0, 1001.0), snap(10.6, 1009.0, 1011.0), snap(12.0, 1019.0, 1021.0)]

    lead = series("1570", leader_raws)
    foll = series("9984", follower_raws)
    onsets, _ = find_onsets(lead)

    rows = build_pair_events(lead, foll, onsets, feasibility=True)
    assert rows and rows[0].m0 == 1000.0  # counts and availability are still there
    assert rows[0].m_entry_05 == 1010.0
    assert rows[0].mh == {} and rows[0].screen_edge == {} and rows[0].bridge_edge == {}

    # The full path, by contrast, must call them -- otherwise this test is vacuous.
    with pytest.raises(AssertionError):
        build_pair_events(lead, foll, onsets, feasibility=False)

    # End to end: run_feasibility over a synthetic day, with the outcome
    # functions still booby-trapped.
    def fake_load(settings, trade_date, codes=REQUIRED_SYMBOLS):
        out = {c: build_series(c, trade_date, []) for c in codes}
        out["1570"] = build_series("1570", trade_date, leader_raws)
        out["9984"] = build_series("9984", trade_date, follower_raws)
        return out

    monkeypatch.setattr(leadlag, "load_day_series", fake_load)
    doc = run_feasibility(None, DAY)

    assert doc["mode"] == "feasibility"
    assert doc["leaders"]["1570"]["onset_events"] == 1
    key = pair_key("1570", "9984")
    assert doc["pairs"][key]["events_with_follower_reference"] == 1
    assert doc["pairs"][key]["days_to_200_events"] == MIN_EVENTS_PER_PAIR
    assert doc["pairs"][pair_key("1570", "9983")]["status"] == "INERT"
    assert_no_performance_fields(doc)  # schema guard, again


def test_L14_performance_field_guard_rejects_outcome_keys():
    for bad in (
        {"median_edge_bps": 1.0},
        {"pairs": {"a": {"screen_drift_bps": 1.0}}},
        {"cells": [{"m2": 1.0}]},
        {"pair_ranking": ["a"]},
        {"win_rate": 0.5},
    ):
        with pytest.raises(ValueError):
            assert_no_performance_fields(bad)
    assert_no_performance_fields({"onset_events": 3, "trigger_duty_cycle": 0.01})


# --- L15 ------------------------------------------------------------------
# Integrity gate: 07-09 partial, 07-10 empty, 07-13 full, half-session gap fails.


def _settings(root: Path) -> Settings:
    return Settings(
        backcast_root=root,
        board_source_root=root,
        medallion_root=root / "data",
        market_timezone="Asia/Tokyo",
        session_open="09:00:00",
        session_close="15:30:00",
    )


def _write_board_db(
    path: Path,
    day: date,
    codes,
    first: time,
    last: time,
    *,
    skip_afternoon: tuple[str, ...] = (),
    empty: bool = False,
) -> None:
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE TABLE board_push (code VARCHAR, ts_local TIMESTAMP)")
        if empty:
            return
        rows = []
        for code in codes:
            rows.append((code, datetime.combine(day, first)))
            rows.append((code, datetime.combine(day, time(10, 0, 0))))  # morning
            if code in skip_afternoon:
                continue  # this symbol goes dark after the lunch break
            rows.append((code, datetime.combine(day, time(13, 0, 0))))  # afternoon
            rows.append((code, datetime.combine(day, last)))
        con.executemany("INSERT INTO board_push VALUES (?, ?)", rows)
    finally:
        con.close()


def test_L15_integrity_fixtures(tmp_path):
    st = _settings(tmp_path)
    today = date(2026, 7, 14)

    # 2026-07-13: the real full session (08:45:30 -> 15:30:00, all 25 symbols).
    full_day = date(2026, 7, 13)
    _write_board_db(
        tmp_path / "2026-07-13.duckdb", full_day, REQUIRED_SYMBOLS,
        time(8, 45, 30), time(15, 30, 0),
    )
    m = integrity_check(st, full_day, today=today)
    assert m["FULL_SESSION_ELIGIBLE"] is True
    assert m["failures"] == []
    assert m["distinct_codes"] == 25

    # 2026-07-09: partial -- the recorder started at 09:43:52, after the open.
    partial_day = date(2026, 7, 9)
    _write_board_db(
        tmp_path / "2026-07-09.duckdb", partial_day, REQUIRED_SYMBOLS,
        time(9, 43, 52), time(15, 30, 0),
    )
    m = integrity_check(st, partial_day, today=today)
    assert m["FULL_SESSION_ELIGIBLE"] is False
    assert any("late_start" in f for f in m["failures"])

    # 2026-07-10: an empty 12KB recording. Zero rows must fail, not pass quietly.
    empty_day = date(2026, 7, 10)
    _write_board_db(
        tmp_path / "2026-07-10.duckdb", empty_day, REQUIRED_SYMBOLS,
        time(9, 0, 0), time(15, 30, 0), empty=True,
    )
    m = integrity_check(st, empty_day, today=today)
    assert m["FULL_SESSION_ELIGIBLE"] is False
    assert m["row_count"] == 0
    assert any("empty_recording" in f for f in m["failures"])

    # ... and with its live WAL still present, it fails closed without opening.
    (tmp_path / "2026-07-10.duckdb.wal").write_bytes(b"\x00")
    m = integrity_check(st, empty_day, today=today)
    assert m["FULL_SESSION_ELIGIBLE"] is False
    assert m["opened"] is False
    assert "unfinalized_wal_present" in m["failures"]

    # A required symbol with no afternoon rows fails, even though the day covers
    # the open and the close.
    holed = date(2026, 7, 8)
    _write_board_db(
        tmp_path / "2026-07-08.duckdb", holed, REQUIRED_SYMBOLS,
        time(9, 0, 0), time(15, 30, 0), skip_afternoon=("6501",),
    )
    m = integrity_check(st, holed, today=today)
    assert m["FULL_SESSION_ELIGIBLE"] is False
    assert m["required_symbols_missing_afternoon"] == ["6501"]
    assert any("missing_afternoon" in f for f in m["failures"])

    # Today's file is never finalized, whatever it contains.
    m = integrity_check(st, full_day, today=full_day)
    assert m["FULL_SESSION_ELIGIBLE"] is False
    assert any("not_finalized" in f for f in m["failures"])

    # A day the recorder never wrote at all.
    m = integrity_check(st, date(2026, 7, 7), today=today)
    assert m["FULL_SESSION_ELIGIBLE"] is False
    assert m["failures"] == ["db_missing"]


# --- event table ----------------------------------------------------------


def test_event_parquet_roundtrip(tmp_path):
    leader = series("1570", [snap(8, 999.0, 1001.0), snap(10, 1029.0, 1031.0)])
    follower = series(
        "9984",
        [snap(9.9, 999.0, 1001.0), snap(10.6, 1009.0, 1011.0), snap(12.0, 1019.0, 1021.0)],
    )
    onsets, _ = find_onsets(leader)
    rows = build_pair_events(leader, follower, onsets)
    path = tmp_path / "phaseA_events.parquet"
    assert write_events_parquet(rows, path) == len(rows)

    con = duckdb.connect()
    try:
        got = con.execute(
            f"SELECT pair, direction, m0, m_entry_05, screen_edge_bps_2s, "
            f"bridge_edge_bps_2s FROM read_parquet('{path.as_posix()}')"
        ).fetchall()
    finally:
        con.close()
    assert got[0][0] == "1570->9984"
    assert got[0][1] == 1
    assert got[0][2] == 1000.0
    assert got[0][3] == 1010.0
    assert got[0][4] == pytest.approx(rows[0].screen_edge[2.0])
    assert got[0][5] == pytest.approx(rows[0].bridge_edge[2.0])
