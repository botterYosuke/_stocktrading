"""Family 3 -- cross-name lead-lag, Phase A (signal study only).

Pre-registration: `docs/family-designs.md` "Family 3: 銘柄間 lead-lag" (frozen
2026-07-10) plus the 2026-07-13 commander amendment (feasibility BRIDGE, the
report-only +30/+60s horizons, the Day-0 feasibility mode and the daily recorder
integrity gate).

Phase A measures ONE thing: when a leader's mid moves more than 2x its own
spread over 2 seconds, does the follower's mid drift in the same direction by
more than the follower's half-spread? There is no execution model here -- no
orders, no fills, no maker/taker simulator. Phase B is not written until Phase A
passes its frozen kill criteria.

Two hygiene rules do all the work and are worth stating up front:

* **No look-ahead.** The follower reference `m0` is the latest snapshot with
  `ts_event <= t`. Leader and follower snapshots carrying the same timestamp
  arrive in an order the recorder does not guarantee, so a same-tick follower row
  may legitimately be used as `m0` (it is not in the future) but never as the
  BRIDGE entry.
* **The BRIDGE is not a fill model.** It re-prices the same event from the first
  follower snapshot at or after `t + 0.5s` and charges a full round-trip spread.
  It is a feasibility floor, not a claim that such a quote was executable.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from statistics import median

import duckdb

from .config import Settings
from .maker import CONTINUOUS_SIGN, MORNING_END, in_continuous_window

# --------------------------------------------------------------------------
# Frozen spec constants. Changing any of these invalidates the pre-registration.
# --------------------------------------------------------------------------

RET_LOOKBACK_SECS = 2.0  # leader return window
LEADER_SPREAD_MULT = 2.0  # trigger = ret2 > 2 * leader_spread_ret (strict)
GAP_GUARD_SECS = 2.0  # a reference row older than this vs its target is missing
DECISION_HORIZONS: tuple[float, ...] = (2.0, 5.0)  # these decide the family
REPORT_HORIZONS: tuple[float, ...] = (30.0, 60.0)  # report-only, never decide
ALL_HORIZONS: tuple[float, ...] = DECISION_HORIZONS + REPORT_HORIZONS
BRIDGE_LATENCY_SECS = 0.5  # earliest BRIDGE entry
BRIDGE_MAX_ENTRY_SECS = 1.5  # later than this and the BRIDGE is missing
MIN_EVENTS_PER_PAIR = 200
MIN_SESSIONS = 5
SIGN_CONSISTENCY_DAYS = 4  # of 5 daily medians

# 2026-07-13 was the only full session at issue time; it is spent on Day-0
# feasibility (counts only) and is excluded from the performance sample so that
# nothing seen there can fit the five judged days.
DAY0_DATE = date(2026, 7, 13)

BOARD_TABLE = "board_push"
SESSION_MORNING = (time(9, 0, 0), time(11, 30, 0))
SESSION_AFTERNOON = (time(12, 30, 0), time(15, 30, 0))
INTEGRITY_MIN_OPEN = time(9, 0, 30)  # min(ts_local) must be at or before this
INTEGRITY_MAX_CLOSE = time(15, 29, 30)  # max(ts_local) must be at or after this

# 32 directed pairs, verbatim from the pre-registration's explicit enumeration.
# The canonical doc's parenthetical "計 28 ペア" is an arithmetic slip in the
# summary line, not a shorter list: the enumeration itself yields
# ETF 8 + 6 + wire 3x2 + bank 4 + heavy 2 + semicap 4 + 1 + electric 1 = 32.
# Deleting `6857 -> 6920` (or any other) to reach 28 would be a silent
# correction of the authority by its own footnote, so the enumeration wins and
# the discrepancy is echoed into every report as a resolved ambiguity.
PAIRS: tuple[tuple[str, str], ...] = (
    # index ETF -> large caps
    ("1570", "9984"),
    ("1570", "9983"),
    ("1570", "8035"),
    ("1570", "6857"),
    ("1570", "6920"),
    ("1570", "6146"),
    ("1570", "7203"),
    ("1570", "6758"),
    ("1568", "8306"),
    ("1568", "8316"),
    ("1568", "8411"),
    ("1568", "7203"),
    ("1568", "6501"),
    ("1568", "8058"),
    # wire makers: both directions of three undirected pairs
    ("5801", "5802"),
    ("5802", "5801"),
    ("5801", "5803"),
    ("5803", "5801"),
    ("5802", "5803"),
    ("5803", "5802"),
    # banks
    ("8306", "8316"),
    ("8306", "8411"),
    ("8306", "7186"),
    ("8306", "7182"),
    # heavy industry
    ("7011", "7012"),
    ("7011", "7013"),
    # semiconductor equipment
    ("8035", "6857"),
    ("8035", "6920"),
    ("8035", "6146"),
    ("8035", "285A"),
    ("6857", "6920"),
    # electric machinery
    ("6501", "6503"),
)

PAIR_COUNT_AMBIGUITY = (
    "family-designs.md Family 3 says '計 28 ペア' but its explicit enumeration "
    "yields 32 directed pairs (ETF 14 + wire 3 undirected x2 = 6 + bank 4 + "
    "heavy 2 + semicap 5 + electric 1). Commander pre-ruling (made before any "
    "data was read): the enumeration is authoritative, 32 directed pairs, no "
    "silent deletion of 6857->6920 or any other pair to reach 28."
)

LEADERS: tuple[str, ...] = tuple(dict.fromkeys(leader for leader, _ in PAIRS))
REQUIRED_SYMBOLS: tuple[str, ...] = tuple(
    sorted({code for pair in PAIRS for code in pair})
)

ETF_LEADERS = frozenset({"1570", "1568"})

PAIR_GROUPS: dict[str, str] = {}
for _leader, _follower in PAIRS:
    if _leader in ETF_LEADERS:
        _group = "etf"
    elif _leader in {"5801", "5802", "5803"}:
        _group = "wire"
    elif _leader == "8306":
        _group = "bank"
    elif _leader == "7011":
        _group = "heavy"
    elif _leader in {"8035", "6857"}:
        _group = "semicap"
    else:
        _group = "electric"
    PAIR_GROUPS[f"{_leader}->{_follower}"] = _group


def pair_key(leader: str, follower: str) -> str:
    return f"{leader}->{follower}"


# --------------------------------------------------------------------------
# Snapshot series
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RawSnap:
    """One board snapshot as it comes out of silver (before hygiene)."""

    ts: datetime
    bid: float
    ask: float
    bid_sign: str | None
    ask_sign: str | None


@dataclass(frozen=True, slots=True)
class Series:
    """Eligible snapshots of one symbol on one day, ascending in time.

    Everything the engine reads is precomputed into parallel tuples so a lookup
    is a bisect on `secs`. Only snapshots that survived hygiene are here, which
    is what makes "latest row at or before T" mean "latest *usable* row".
    """

    code: str
    trade_date: date
    secs: tuple[float, ...]  # seconds since midnight, JST wall clock
    ts: tuple[datetime, ...]
    bid: tuple[float, ...]
    ask: tuple[float, ...]
    mid: tuple[float, ...]
    spread: tuple[float, ...]
    segment: tuple[int, ...]  # 0 = morning, 1 = afternoon
    n_raw: int = 0

    def __len__(self) -> int:
        return len(self.secs)


def _secs_of_day(ts: datetime) -> float:
    return ts.hour * 3600 + ts.minute * 60 + ts.second + ts.microsecond / 1e6


def _segment_of(ts: datetime) -> int:
    """Morning and afternoon are separate sessions; nothing spans the break."""
    return 0 if ts.time() < MORNING_END else 1


def is_eligible(snap: RawSnap) -> bool:
    """Continuous-trading, two-sided, uncrossed, inside a continuous window.

    `CONTINUOUS_SIGN` (0101) on both sides excludes the special quote (0107) and
    halt (0120) states; `bid < ask` excludes crossed and locked books; the
    window check excludes both itayose auctions and the lunch break.
    """
    return (
        snap.bid_sign == CONTINUOUS_SIGN
        and snap.ask_sign == CONTINUOUS_SIGN
        and snap.bid > 0.0
        and snap.ask > 0.0
        and snap.bid < snap.ask
        and in_continuous_window(snap.ts)
    )


def build_series(code: str, trade_date: date, raws: Sequence[RawSnap]) -> Series:
    """Drop ineligible snapshots and precompute the lookup columns."""
    secs: list[float] = []
    ts: list[datetime] = []
    bid: list[float] = []
    ask: list[float] = []
    mid: list[float] = []
    spread: list[float] = []
    segment: list[int] = []
    for snap in raws:
        if not is_eligible(snap):
            continue
        secs.append(_secs_of_day(snap.ts))
        ts.append(snap.ts)
        bid.append(snap.bid)
        ask.append(snap.ask)
        mid.append((snap.bid + snap.ask) / 2.0)
        spread.append(snap.ask - snap.bid)
        segment.append(_segment_of(snap.ts))
    return Series(
        code=code,
        trade_date=trade_date,
        secs=tuple(secs),
        ts=tuple(ts),
        bid=tuple(bid),
        ask=tuple(ask),
        mid=tuple(mid),
        spread=tuple(spread),
        segment=tuple(segment),
        n_raw=len(raws),
    )


def latest_at(series: Series, target: float, segment: int) -> int | None:
    """Index of the latest snapshot at or before `target`, or None.

    Missing when there is no such row, when the row is staler than the 2s gap
    guard, or when it belongs to the other half of the day. This is the single
    place the no-look-ahead rule lives: `bisect_right` can never return a row
    whose timestamp is greater than `target`.
    """
    i = bisect_right(series.secs, target) - 1
    if i < 0:
        return None
    if target - series.secs[i] > GAP_GUARD_SECS:
        return None
    if series.segment[i] != segment:
        return None
    return i


def first_at_or_after(
    series: Series, earliest: float, latest: float, segment: int
) -> int | None:
    """Index of the FIRST snapshot at or after `earliest`, if it lands by `latest`.

    Used only for the BRIDGE entry: the earliest row a decision made at `t` could
    possibly have acted on. Deliberately not `latest_at`: taking the newest row
    before the target would be exactly the leak the latency is there to prevent.
    """
    i = bisect_left(series.secs, earliest)
    if i >= len(series.secs):
        return None
    if series.secs[i] > latest:
        return None
    if series.segment[i] != segment:
        return None
    return i


# --------------------------------------------------------------------------
# Leader trigger / onset detection
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Onset:
    leader: str
    trade_date: date
    t: float  # seconds of day
    ts: datetime
    segment: int
    direction: int  # +1 / -1
    leader_mid: float
    leader_mid_lag2: float
    leader_spread: float
    leader_spread_ret: float
    ret2: float
    move_spread_ratio: float


@dataclass(frozen=True, slots=True)
class LeaderStats:
    """Counts only. These are safe to read on Day-0; nothing here is an outcome."""

    leader: str
    trade_date: date
    n_eligible_snaps: int
    n_evaluable_snaps: int  # eligible AND a 2s-lagged reference exists
    raw_trigger_snaps: int  # naive count: every snapshot with trigger == True
    onset_events: int  # False->True transitions only

    @property
    def trigger_duty_cycle(self) -> float:
        if not self.n_evaluable_snaps:
            return 0.0
        return self.raw_trigger_snaps / self.n_evaluable_snaps

    @property
    def event_inflation_ratio(self) -> float:
        """How many times a naive per-snapshot count would have counted a shock."""
        if not self.onset_events:
            return 0.0
        return self.raw_trigger_snaps / self.onset_events


def evaluate_trigger(
    mid_now: float, mid_lag2: float, spread_now: float
) -> tuple[bool, int, float, float]:
    """The frozen trigger predicate, isolated so its boundary can be tested exactly.

    `ret2 > 2 * spread_ret` is a STRICT inequality: a move that merely equals
    twice the spread is not a shock. The direction is the sign of the mid change,
    and it is only meaningful when the trigger fires (a flat mid has ret2 == 0,
    which cannot exceed a positive threshold).
    """
    spread_ret = spread_now / mid_now
    ret2 = abs(mid_now / mid_lag2 - 1.0)
    trigger = ret2 > LEADER_SPREAD_MULT * spread_ret
    direction = 1 if mid_now > mid_lag2 else -1
    return trigger, direction, ret2, spread_ret


def find_onsets(series: Series) -> tuple[list[Onset], LeaderStats]:
    """Every False->True transition of the leader's trigger, once per shock.

    The trigger is a level, so a single shock stays True for as many snapshots as
    the leader keeps moving. Counting those would inflate the sample by the
    recorder's snapshot rate rather than by the number of shocks, so only the
    onset fires and the trigger must fall back to False before it can re-arm.

    Snapshots whose 2s reference is missing (a data gap, or the first two seconds
    of a session) are *undefined*, not False: they carry the previous state
    forward. Treating them as False would let a gap in the middle of one shock
    re-arm the trigger and count it twice.
    """
    onsets: list[Onset] = []
    prev_trigger: dict[int, bool] = {}
    n_evaluable = 0
    n_raw = 0

    for i in range(len(series)):
        seg = series.segment[i]
        j = latest_at(series, series.secs[i] - RET_LOOKBACK_SECS, seg)
        if j is None:
            continue  # undefined -- state is carried forward, not reset
        n_evaluable += 1
        mid_now = series.mid[i]
        mid_lag2 = series.mid[j]
        trigger, direction, ret2, spread_ret = evaluate_trigger(
            mid_now, mid_lag2, series.spread[i]
        )
        if trigger:
            n_raw += 1
        if trigger and not prev_trigger.get(seg, False):
            onsets.append(
                Onset(
                    leader=series.code,
                    trade_date=series.trade_date,
                    t=series.secs[i],
                    ts=series.ts[i],
                    segment=seg,
                    direction=direction,
                    leader_mid=mid_now,
                    leader_mid_lag2=mid_lag2,
                    leader_spread=series.spread[i],
                    leader_spread_ret=spread_ret,
                    ret2=ret2,
                    move_spread_ratio=ret2 / spread_ret if spread_ret else math.inf,
                )
            )
        prev_trigger[seg] = trigger

    return onsets, LeaderStats(
        leader=series.code,
        trade_date=series.trade_date,
        n_eligible_snaps=len(series),
        n_evaluable_snaps=n_evaluable,
        raw_trigger_snaps=n_raw,
        onset_events=len(onsets),
    )


# --------------------------------------------------------------------------
# Outcome maths -- the ONLY functions allowed to touch prices after `t`.
# Feasibility mode must never call these; `test_leadlag.py` L14 spies on that.
# --------------------------------------------------------------------------


def compute_screen_outcome(
    direction: int, m0: float, spread0: float, mh: float
) -> tuple[float, float]:
    """Kill-A statistic: signed drift from `m0`, minus the follower half-spread.

    The half-spread is the frozen screen cost: it asks whether the drift would
    have cleared the distance from the mid to the touch, which is the cheapest
    possible way to get on board. It is deliberately optimistic; the BRIDGE below
    is what asks the feasibility question.
    """
    signed_drift_bps = direction * (mh / m0 - 1.0) * 1e4
    edge_bps = signed_drift_bps - 0.5 * spread0 / m0 * 1e4
    return signed_drift_bps, edge_bps


def compute_bridge_outcome(
    direction: int, m_entry: float, spread_entry: float, mh: float
) -> tuple[float, float]:
    """Feasibility statistic: drift from the post-latency entry, minus a full spread.

    The full spread is a round-trip taker floor (half in, half out) charged
    against the entry mid. The horizon is NOT restarted from the entry: the exit
    stays at `t + h`, so the tradable hold is only ~1.5s / ~4.5s. Extending the
    horizon to recover the lost latency would be the same optimism the whole
    BRIDGE exists to price.
    """
    bridge_drift_bps = direction * (mh / m_entry - 1.0) * 1e4
    bridge_edge_bps = bridge_drift_bps - spread_entry / m_entry * 1e4
    return bridge_drift_bps, bridge_edge_bps


# --------------------------------------------------------------------------
# Per-pair event construction
# --------------------------------------------------------------------------

REASON_OK = "ok"
REASON_NO_M0 = "no_follower_m0_within_2s"
BRIDGE_OK = "ok"
BRIDGE_NO_ENTRY = "no_entry_row_by_t_plus_1.5s"
HORIZON_MISSING = "no_row_within_2s_of_target"
BRIDGE_EXIT_BEFORE_ENTRY = "exit_row_precedes_entry_row"


@dataclass
class EventRow:
    trade_date: date
    pair: str
    leader: str
    follower: str
    t_iso: str
    t_secs: float
    segment: int
    direction: int
    leader_mid: float
    leader_mid_lag2: float
    leader_spread: float
    leader_spread_ret: float
    ret2: float
    move_spread_ratio: float
    exclusion_reason: str
    bridge_reason: str
    m0: float | None = None
    spread0: float | None = None
    m0_age_ms: float | None = None
    follower_own_dir: int | None = None  # follower's own 2s direction before t
    entry_ts_iso: str | None = None
    entry_age_ms: float | None = None  # entry row time minus t, in ms
    m_entry_05: float | None = None
    spread_entry_05: float | None = None
    mh: dict[float, float | None] = field(default_factory=dict)
    screen_drift: dict[float, float | None] = field(default_factory=dict)
    screen_edge: dict[float, float | None] = field(default_factory=dict)
    bridge_drift: dict[float, float | None] = field(default_factory=dict)
    bridge_edge: dict[float, float | None] = field(default_factory=dict)
    full_spread_edge: dict[float, float | None] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.exclusion_reason == REASON_OK


def _follower_own_direction(follower: Series, i0: int, segment: int) -> int | None:
    """The follower's OWN move over the 2s before `t` -- the momentum control.

    If the follower was already running in the event direction, the "lead-lag"
    drift may be nothing but its own continuation. Report-only.
    """
    j = latest_at(follower, follower.secs[i0] - RET_LOOKBACK_SECS, segment)
    if j is None:
        return None
    if follower.mid[i0] > follower.mid[j]:
        return 1
    if follower.mid[i0] < follower.mid[j]:
        return -1
    return 0


def build_pair_events(
    leader: Series,
    follower: Series,
    onsets: Sequence[Onset],
    *,
    feasibility: bool = False,
) -> list[EventRow]:
    """Turn each leader onset into one follower observation (or an exclusion).

    In feasibility mode this resolves *which rows exist* and stops. It never
    reads a price outcome, because Day-0 is spent on counts and the five judged
    days must not be contaminated by a peek at 07-13's performance.
    """
    rows: list[EventRow] = []
    pair = pair_key(leader.code, follower.code)

    for onset in onsets:
        t = onset.t
        seg = onset.segment
        row = EventRow(
            trade_date=onset.trade_date,
            pair=pair,
            leader=leader.code,
            follower=follower.code,
            t_iso=onset.ts.isoformat(),
            t_secs=t,
            segment=seg,
            direction=onset.direction,
            leader_mid=onset.leader_mid,
            leader_mid_lag2=onset.leader_mid_lag2,
            leader_spread=onset.leader_spread,
            leader_spread_ret=onset.leader_spread_ret,
            ret2=onset.ret2,
            move_spread_ratio=onset.move_spread_ratio,
            exclusion_reason=REASON_OK,
            bridge_reason=BRIDGE_OK,
        )

        # Follower reference: latest row at or before t, at most 2s stale.
        i0 = latest_at(follower, t, seg)
        if i0 is None:
            row.exclusion_reason = REASON_NO_M0
            row.bridge_reason = BRIDGE_NO_ENTRY
            rows.append(row)
            continue
        row.m0 = follower.mid[i0]
        row.spread0 = follower.spread[i0]
        row.m0_age_ms = (t - follower.secs[i0]) * 1e3
        row.follower_own_dir = _follower_own_direction(follower, i0, seg)

        # BRIDGE entry: first row at or after t + 0.5s, and no later than t + 1.5s.
        ib = first_at_or_after(
            follower, t + BRIDGE_LATENCY_SECS, t + BRIDGE_MAX_ENTRY_SECS, seg
        )
        if ib is None:
            row.bridge_reason = BRIDGE_NO_ENTRY
        else:
            row.entry_ts_iso = follower.ts[ib].isoformat()
            row.entry_age_ms = (follower.secs[ib] - t) * 1e3
            row.m_entry_05 = follower.mid[ib]
            row.spread_entry_05 = follower.spread[ib]

        if feasibility:
            rows.append(row)
            continue

        for h in ALL_HORIZONS:
            ih = latest_at(follower, t + h, seg)
            if ih is None:
                row.mh[h] = None
                row.screen_drift[h] = None
                row.screen_edge[h] = None
                row.bridge_drift[h] = None
                row.bridge_edge[h] = None
                row.full_spread_edge[h] = None
                continue
            mh = follower.mid[ih]
            row.mh[h] = mh
            drift, edge = compute_screen_outcome(row.direction, row.m0, row.spread0, mh)
            row.screen_drift[h] = drift
            row.screen_edge[h] = edge
            # Legacy pessimistic diagnostic: same entry, full spread, no latency.
            row.full_spread_edge[h] = drift - row.spread0 / row.m0 * 1e4

            if ib is None or follower.secs[ih] < follower.secs[ib]:
                # No entry, or the exit observation predates the entry: charging
                # a spread for a trip that ran backwards in time would be fiction.
                row.bridge_drift[h] = None
                row.bridge_edge[h] = None
                continue
            b_drift, b_edge = compute_bridge_outcome(
                row.direction, row.m_entry_05, row.spread_entry_05, mh
            )
            row.bridge_drift[h] = b_drift
            row.bridge_edge[h] = b_edge

        rows.append(row)

    return rows


# --------------------------------------------------------------------------
# Silver loading
# --------------------------------------------------------------------------


def load_day_series(
    settings: Settings, trade_date: date, codes: Sequence[str] = REQUIRED_SYMBOLS
) -> dict[str, Series]:
    """Load one day of silver for the given symbols, one Series per symbol."""
    out: dict[str, Series] = {}
    con = duckdb.connect()
    try:
        for code in codes:
            glob = (
                settings.silver_root
                / f"date={trade_date.isoformat()}"
                / f"code={code}"
                / "*.parquet"
            ).as_posix()
            try:
                rows = con.execute(
                    "SELECT ts_event, bid_px, ask_px, bid_sign, ask_sign "
                    "FROM read_parquet($glob) ORDER BY ts_event",
                    {"glob": glob},
                ).fetchall()
            except duckdb.IOException:
                rows = []
            raws = [
                RawSnap(ts=r[0], bid=r[1], ask=r[2], bid_sign=r[3], ask_sign=r[4])
                for r in rows
            ]
            out[code] = build_series(code, trade_date, raws)
    finally:
        con.close()
    return out


# --------------------------------------------------------------------------
# Recorder integrity gate (section 4.2)
# --------------------------------------------------------------------------


def integrity_check(
    settings: Settings, trade_date: date, today: date | None = None
) -> dict:
    """Fail-closed manifest for one recording day.

    A day is only `FULL_SESSION_ELIGIBLE` if the file is finalized (no live WAL,
    not still being recorded), opens, covers the open and the close, and carries
    both a morning and an afternoon row for every one of the 25 required symbols.
    Counting eligible days off filenames or file sizes is how a 12KB empty
    recording gets mistaken for a trading day.

    `today` is the as-of date. It defaults to the wall clock, which refuses the
    current day outright -- the conservative choice while a recorder may still be
    writing. Once a session has closed, the caller may assert a later as-of date
    to have the day judged on its contents; the WAL check, which is the real test
    of "still being written", is not bypassed by doing so.
    """
    today = today or date.today()
    db_path = settings.board_source_root / f"{trade_date.isoformat()}.duckdb"
    wal_path = db_path.with_name(db_path.name + ".wal")

    manifest: dict = {
        "date": trade_date.isoformat(),
        "as_of": today.isoformat(),
        "db_path": str(db_path),
        "exists": db_path.exists(),
        "file_size_bytes": db_path.stat().st_size if db_path.exists() else None,
        "wal_present": wal_path.exists(),
        "opened": False,
        "row_count": None,
        "min_ts": None,
        "max_ts": None,
        "distinct_codes": None,
        "required_symbols_total": len(REQUIRED_SYMBOLS),
        "required_symbols_missing": None,
        "required_symbols_missing_morning": None,
        "required_symbols_missing_afternoon": None,
        "failures": [],
        "FULL_SESSION_ELIGIBLE": False,
    }
    fail: list[str] = manifest["failures"]

    if not db_path.exists():
        fail.append("db_missing")
        return manifest
    if wal_path.exists():
        # A sibling WAL means the recorder never checkpointed: the committed file
        # is whatever survived, and reading it would silently under-report.
        fail.append("unfinalized_wal_present")
        return manifest
    if trade_date >= today:
        fail.append("recording_day_not_finalized (date >= today)")
        return manifest

    try:
        con = duckdb.connect(str(db_path), read_only=True)
    except Exception as exc:  # noqa: BLE001 - fail closed on any open failure
        fail.append(f"db_open_failed: {type(exc).__name__}")
        return manifest

    try:
        try:
            row = con.execute(
                f"SELECT count(*), min(ts_local), max(ts_local), count(DISTINCT code) "
                f"FROM {BOARD_TABLE}"
            ).fetchone()
        except Exception as exc:  # noqa: BLE001
            fail.append(f"board_push_query_failed: {type(exc).__name__}")
            return manifest
        manifest["opened"] = True
        n_rows, min_ts, max_ts, n_codes = row
        manifest["row_count"] = int(n_rows)
        manifest["min_ts"] = min_ts.isoformat() if min_ts else None
        manifest["max_ts"] = max_ts.isoformat() if max_ts else None
        manifest["distinct_codes"] = int(n_codes)

        if not n_rows or min_ts is None or max_ts is None:
            fail.append("empty_recording (0 rows)")
            manifest["required_symbols_missing"] = list(REQUIRED_SYMBOLS)
            manifest["required_symbols_missing_morning"] = list(REQUIRED_SYMBOLS)
            manifest["required_symbols_missing_afternoon"] = list(REQUIRED_SYMBOLS)
            return manifest

        if min_ts.time() > INTEGRITY_MIN_OPEN:
            fail.append(f"late_start (min_ts {min_ts.time()} > {INTEGRITY_MIN_OPEN})")
        if max_ts.time() < INTEGRITY_MAX_CLOSE:
            fail.append(f"early_stop (max_ts {max_ts.time()} < {INTEGRITY_MAX_CLOSE})")

        placeholders = ", ".join(f"'{code}'" for code in REQUIRED_SYMBOLS)
        present = {
            r[0]
            for r in con.execute(
                f"SELECT DISTINCT code FROM {BOARD_TABLE} WHERE code IN ({placeholders})"
            ).fetchall()
        }
        missing = [c for c in REQUIRED_SYMBOLS if c not in present]
        manifest["required_symbols_missing"] = missing
        if missing:
            fail.append(f"required_symbols_missing ({len(missing)})")

        def _half(lo: time, hi: time) -> set[str]:
            return {
                r[0]
                for r in con.execute(
                    f"SELECT DISTINCT code FROM {BOARD_TABLE} "
                    f"WHERE code IN ({placeholders}) "
                    f"AND ts_local::TIME >= '{lo}'::TIME "
                    f"AND ts_local::TIME <= '{hi}'::TIME"
                ).fetchall()
            }

        morning = _half(*SESSION_MORNING)
        afternoon = _half(*SESSION_AFTERNOON)
        miss_am = [c for c in REQUIRED_SYMBOLS if c not in morning]
        miss_pm = [c for c in REQUIRED_SYMBOLS if c not in afternoon]
        manifest["required_symbols_missing_morning"] = miss_am
        manifest["required_symbols_missing_afternoon"] = miss_pm
        if miss_am:
            fail.append(f"required_symbols_missing_morning ({len(miss_am)})")
        if miss_pm:
            fail.append(f"required_symbols_missing_afternoon ({len(miss_pm)})")
    finally:
        con.close()

    manifest["FULL_SESSION_ELIGIBLE"] = not fail
    return manifest


def eligible_dates_from_manifests(manifest_dir: Path) -> list[date]:
    """Count eligible days off manifests, never off filenames or file sizes."""
    dates: list[date] = []
    for path in sorted(manifest_dir.glob("integrity_*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if doc.get("FULL_SESSION_ELIGIBLE") is True:
            dates.append(date.fromisoformat(doc["date"]))
    return sorted(set(dates))


# --------------------------------------------------------------------------
# Day-0 feasibility (section 4.1) -- counts and duty cycles ONLY
# --------------------------------------------------------------------------

# A feasibility document containing any of these substrings in a key is invalid
# by construction: Day-0 must not be able to leak an outcome even by accident.
FORBIDDEN_FEASIBILITY_KEYS = (
    "m2",
    "m5",
    "m30",
    "m60",
    "drift",
    "edge",
    "pnl",
    "win_rate",
    "ranking",
    "median",
    "outcome",
    "return",
)


def assert_no_performance_fields(doc: object, path: str = "$") -> None:
    """Raise if a feasibility document carries anything that looks like a result."""
    if isinstance(doc, dict):
        for key, value in doc.items():
            low = str(key).lower()
            for token in FORBIDDEN_FEASIBILITY_KEYS:
                if token in low:
                    raise ValueError(
                        f"performance field '{key}' at {path} is forbidden in "
                        "feasibility output"
                    )
            assert_no_performance_fields(value, f"{path}.{key}")
    elif isinstance(doc, list):
        for i, value in enumerate(doc):
            assert_no_performance_fields(value, f"{path}[{i}]")


def run_feasibility(settings: Settings, trade_date: date) -> dict:
    """Day-0: how often does the trigger fire, and would the rows even be there?

    Answers exactly four questions -- how often the threshold binds, how many
    distinct shocks that is, how long it would take to reach the 200-event floor,
    and whether the follower rows a Phase B would need actually exist. It does
    not, and must not, answer whether any of it would have made money.
    """
    series = load_day_series(settings, trade_date)

    leaders: dict[str, dict] = {}
    onsets_by_leader: dict[str, list[Onset]] = {}
    for leader in LEADERS:
        s = series.get(leader)
        if s is None or not len(s):
            onsets_by_leader[leader] = []
            leaders[leader] = {
                "n_raw_snaps": s.n_raw if s else 0,
                "n_eligible_snaps": 0,
                "n_evaluable_snaps": 0,
                "raw_trigger_snaps": 0,
                "trigger_duty_cycle": 0.0,
                "onset_events": 0,
                "event_inflation_ratio": 0.0,
                "is_etf": leader in ETF_LEADERS,
            }
            continue
        onsets, stats = find_onsets(s)
        onsets_by_leader[leader] = onsets
        leaders[leader] = {
            "n_raw_snaps": s.n_raw,
            "n_eligible_snaps": stats.n_eligible_snaps,
            "n_evaluable_snaps": stats.n_evaluable_snaps,
            "raw_trigger_snaps": stats.raw_trigger_snaps,
            "trigger_duty_cycle": stats.trigger_duty_cycle,
            "onset_events": stats.onset_events,
            "event_inflation_ratio": stats.event_inflation_ratio,
            "is_etf": leader in ETF_LEADERS,
        }

    pairs: dict[str, dict] = {}
    inert: list[str] = []
    for leader, follower in PAIRS:
        key = pair_key(leader, follower)
        ls, fs = series.get(leader), series.get(follower)
        onsets = onsets_by_leader.get(leader, [])
        if ls is None or fs is None or not len(fs):
            pairs[key] = {
                "group": PAIR_GROUPS[key],
                "leader": leader,
                "follower": follower,
                "follower_missing": True,
                "onset_events": len(onsets),
                "events_with_follower_reference": 0,
                "events_per_day": 0.0,
                "days_to_200_events": 0,
                "status": "INERT",
            }
            inert.append(key)
            continue

        rows = build_pair_events(ls, fs, onsets, feasibility=True)
        n_onsets = len(rows)
        n_m0 = sum(1 for r in rows if r.exclusion_reason == REASON_OK)
        n_bridge = sum(1 for r in rows if r.bridge_reason == BRIDGE_OK)
        reasons: dict[str, int] = {}
        for r in rows:
            reasons[r.exclusion_reason] = reasons.get(r.exclusion_reason, 0) + 1
        bridge_reasons: dict[str, int] = {}
        for r in rows:
            bridge_reasons[r.bridge_reason] = bridge_reasons.get(r.bridge_reason, 0) + 1

        events_day = float(n_m0)  # one day of data == events per day
        days_to_200 = (
            0 if events_day <= 0 else math.ceil(MIN_EVENTS_PER_PAIR / events_day)
        )
        status = "INERT" if days_to_200 == 0 else "FEASIBLE"
        if status == "INERT":
            inert.append(key)

        ages = [r.m0_age_ms for r in rows if r.m0_age_ms is not None]
        pairs[key] = {
            "group": PAIR_GROUPS[key],
            "leader": leader,
            "follower": follower,
            "follower_missing": False,
            "onset_events": n_onsets,
            "events_with_follower_reference": n_m0,
            "follower_reference_availability": (n_m0 / n_onsets) if n_onsets else 0.0,
            "events_with_bridge_entry": n_bridge,
            "bridge_entry_availability_of_onsets": (n_bridge / n_onsets)
            if n_onsets
            else 0.0,
            "bridge_entry_availability_of_referenced": (n_bridge / n_m0)
            if n_m0
            else 0.0,
            "follower_reference_reason_counts": reasons,
            "bridge_entry_reason_counts": bridge_reasons,
            "follower_m0_age_ms_mean": (sum(ages) / len(ages)) if ages else None,
            "follower_m0_age_ms_max": max(ages) if ages else None,
            "events_per_day": events_day,
            "days_to_200_events": days_to_200,
            "status": status,
        }

    duties = [v["trigger_duty_cycle"] for v in leaders.values()]
    doc = {
        "mode": "feasibility",
        "date": trade_date.isoformat(),
        "purpose": (
            "Day-0 counts and binding only. Performance columns are structurally "
            "absent: build_pair_events(feasibility=True) never calls "
            "compute_screen_outcome / compute_bridge_outcome."
        ),
        "frozen_spec": {
            "leader_lookback_secs": RET_LOOKBACK_SECS,
            "leader_spread_multiple": LEADER_SPREAD_MULT,
            "gap_guard_secs": GAP_GUARD_SECS,
            "bridge_latency_secs": BRIDGE_LATENCY_SECS,
            "bridge_max_entry_secs": BRIDGE_MAX_ENTRY_SECS,
            "min_events_per_pair": MIN_EVENTS_PER_PAIR,
            "min_sessions": MIN_SESSIONS,
            "directed_pairs": len(PAIRS),
        },
        "resolved_ambiguities": [PAIR_COUNT_AMBIGUITY],
        "leaders": leaders,
        "pairs": pairs,
        "threshold_binding": {
            "min_leader_duty_cycle": min(duties) if duties else 0.0,
            "max_leader_duty_cycle": max(duties) if duties else 0.0,
            "leaders_at_zero_duty_cycle": [
                k for k, v in leaders.items() if v["trigger_duty_cycle"] == 0.0
            ],
            "leaders_above_50pct_duty_cycle": [
                k for k, v in leaders.items() if v["trigger_duty_cycle"] > 0.5
            ],
            "note": (
                "A duty cycle near 0 means the threshold is binding hard; near 1 "
                "means it is not a gate at all. Both extremes are reported, "
                "neither is used to move the threshold."
            ),
        },
        "inert_pairs": inert,
        "reading": (
            "INERT_FROZEN_SPEC"
            if len(inert) == len(PAIRS)
            else "FEASIBLE_WAIT_DATA"
        ),
    }
    assert_no_performance_fields(doc)
    return doc


# --------------------------------------------------------------------------
# Full Phase A (section 4) -- only runs once 5 fresh full sessions exist
# --------------------------------------------------------------------------


def _required_sign_days(n_days: int) -> int:
    """4-of-5 is the frozen rule; longer samples are held to the same 80%.

    The pre-registration only fixes the 5-day case. Reading "4" as an absolute
    would let a 20-day sample pass on 4 positive days, which is plainly not the
    intent, so the ratio is carried forward. This can only ever tighten the gate,
    never loosen it.
    """
    return max(SIGN_CONSISTENCY_DAYS, math.ceil(0.8 * n_days))


@dataclass
class Cell:
    """One (pair, horizon, statistic) cell of the 32 x 2 x {screen, bridge} table."""

    pair: str
    horizon: float
    statistic: str  # "screen" | "bridge"
    n_events: int
    n_days: int
    overall_median: float | None
    daily_medians: dict[str, float]
    sign_days: int
    required_sign_days: int

    @property
    def powered(self) -> bool:
        return self.n_events >= MIN_EVENTS_PER_PAIR and self.n_days >= MIN_SESSIONS

    @property
    def passes(self) -> bool:
        return (
            self.powered
            and self.overall_median is not None
            and self.overall_median > 0
            and self.sign_days >= self.required_sign_days
        )

    def to_dict(self) -> dict:
        return {
            "pair": self.pair,
            "group": PAIR_GROUPS.get(self.pair),
            "horizon_secs": self.horizon,
            "statistic": self.statistic,
            "n_events": self.n_events,
            "n_days": self.n_days,
            "median_edge_bps": self.overall_median,
            "daily_median_edge_bps": self.daily_medians,
            "sign_consistent_days": self.sign_days,
            "required_sign_days": self.required_sign_days,
            "powered": self.powered,
            "status": "PASS"
            if self.passes
            else ("UNDER_POWERED" if not self.powered else "FAIL"),
        }


def _cell(pair: str, horizon: float, statistic: str, rows: Sequence[EventRow]) -> Cell:
    key = "screen_edge" if statistic == "screen" else "bridge_edge"
    by_day: dict[str, list[float]] = {}
    values: list[float] = []
    for row in rows:
        if not row.usable:
            continue
        value = getattr(row, key).get(horizon)
        if value is None:
            continue
        values.append(value)
        by_day.setdefault(row.trade_date.isoformat(), []).append(value)
    daily = {d: median(v) for d, v in sorted(by_day.items())}
    overall = median(values) if values else None
    if overall is None or overall == 0:
        sign_days = 0
    else:
        want = 1 if overall > 0 else -1
        sign_days = sum(
            1 for v in daily.values() if (1 if v > 0 else (-1 if v < 0 else 0)) == want
        )
    return Cell(
        pair=pair,
        horizon=horizon,
        statistic=statistic,
        n_events=len(values),
        n_days=len(daily),
        overall_median=overall,
        daily_medians=daily,
        sign_days=sign_days,
        required_sign_days=_required_sign_days(len(daily)),
    )


def decide(cells: Sequence[Cell]) -> str:
    """The frozen decision matrix. +30/+60s cells are not consulted.

    Kill A is a screen-only question and the BRIDGE cannot rescue it; the BRIDGE
    can only downgrade a screen pass to `PASS_SCREEN_ONLY`, which means the
    signal is real but not reachable, and Phase B still does not get written.
    """
    decision_cells = [c for c in cells if c.horizon in DECISION_HORIZONS]
    screen = {(c.pair, c.horizon): c for c in decision_cells if c.statistic == "screen"}
    bridge = {(c.pair, c.horizon): c for c in decision_cells if c.statistic == "bridge"}

    if not any(c.powered for c in screen.values()):
        return "WAIT_DATA"

    screen_pass = [k for k, c in screen.items() if c.passes]
    if not screen_pass:
        return "KILL"
    for k in screen_pass:
        b = bridge.get(k)
        if b is not None and b.passes:
            return "PASS_CANDIDATE"
    return "PASS_SCREEN_ONLY"


def _matched_null_medians(
    rows_by_pair: dict[str, list[EventRow]], horizon: float
) -> dict[str, float | None]:
    """Same events, direction re-drawn from the day's other events of that pair.

    Shuffling the sign within a day keeps the event times, the follower rows and
    the spread cost, and destroys only the leader's directional claim. If the
    null medians look like the real ones, the "signal" is a property of the
    follower's own volatility, not of the leader.
    """
    out: dict[str, float | None] = {}
    for pair, rows in rows_by_pair.items():
        values: list[float] = []
        by_day: dict[str, list[EventRow]] = {}
        for row in rows:
            if row.usable and row.screen_drift.get(horizon) is not None:
                by_day.setdefault(row.trade_date.isoformat(), []).append(row)
        for day_rows in by_day.values():
            n = len(day_rows)
            for i, row in enumerate(day_rows):
                other = day_rows[(i + n // 2) % n]  # deterministic pairing
                drift = row.screen_drift[horizon] * (
                    other.direction * row.direction
                )  # re-sign with another event's direction
                values.append(drift - 0.5 * row.spread0 / row.m0 * 1e4)
        out[pair] = median(values) if values else None
    return out


def run_full(
    settings: Settings,
    dates: Sequence[date],
) -> tuple[dict, list[EventRow]]:
    """Build the whole 32 x 2 x {screen, bridge} table over the judged days."""
    rows_by_pair: dict[str, list[EventRow]] = {pair_key(a, b): [] for a, b in PAIRS}
    all_rows: list[EventRow] = []
    leader_stats: list[dict] = []

    for trade_date in dates:
        series = load_day_series(settings, trade_date)
        onsets_by_leader: dict[str, list[Onset]] = {}
        for leader in LEADERS:
            s = series.get(leader)
            if s is None or not len(s):
                onsets_by_leader[leader] = []
                continue
            onsets, stats = find_onsets(s)
            onsets_by_leader[leader] = onsets
            leader_stats.append(
                {
                    "date": trade_date.isoformat(),
                    "leader": leader,
                    "n_eligible_snaps": stats.n_eligible_snaps,
                    "n_evaluable_snaps": stats.n_evaluable_snaps,
                    "raw_trigger_snaps": stats.raw_trigger_snaps,
                    "onset_events": stats.onset_events,
                    "trigger_duty_cycle": stats.trigger_duty_cycle,
                    "event_inflation_ratio": stats.event_inflation_ratio,
                }
            )
        for leader, follower in PAIRS:
            ls, fs = series.get(leader), series.get(follower)
            if ls is None or fs is None:
                continue
            rows = build_pair_events(ls, fs, onsets_by_leader.get(leader, []))
            rows_by_pair[pair_key(leader, follower)].extend(rows)
            all_rows.extend(rows)

    cells: list[Cell] = []
    for pair, rows in rows_by_pair.items():
        for horizon in ALL_HORIZONS:
            for statistic in ("screen", "bridge"):
                cells.append(_cell(pair, horizon, statistic, rows))

    decision = decide(cells)
    decision_cells = [c for c in cells if c.horizon in DECISION_HORIZONS]

    report = {
        "mode": "full",
        "dates": [d.isoformat() for d in dates],
        "n_sessions": len(dates),
        "day0_excluded": DAY0_DATE.isoformat(),
        "resolved_ambiguities": [PAIR_COUNT_AMBIGUITY],
        "frozen_spec": {
            "leader_lookback_secs": RET_LOOKBACK_SECS,
            "leader_spread_multiple": LEADER_SPREAD_MULT,
            "decision_horizons_secs": list(DECISION_HORIZONS),
            "report_only_horizons_secs": list(REPORT_HORIZONS),
            "bridge_latency_secs": BRIDGE_LATENCY_SECS,
            "min_events_per_pair": MIN_EVENTS_PER_PAIR,
            "min_sessions": MIN_SESSIONS,
            "sign_consistency": "4 of 5 daily medians (80% for longer samples)",
            "directed_pairs": len(PAIRS),
        },
        "decision": decision,
        "decision_cells": [
            c.to_dict() for c in decision_cells
        ],  # all 32 x 2 x 2 = 128, multiplicity not hidden
        "report_only_cells": [
            c.to_dict() for c in cells if c.horizon in REPORT_HORIZONS
        ],
        "leader_stats": leader_stats,
        "diagnostics": {
            "matched_null_median_screen_edge_bps": {
                str(h): _matched_null_medians(rows_by_pair, h)
                for h in DECISION_HORIZONS
            },
            "note": (
                "Report-only. The +30/+60s cells and every diagnostic below are "
                "excluded from `decision` by construction (see decide())."
            ),
        },
    }
    return report, all_rows


EVENT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("trade_date", "DATE"),
    ("pair", "VARCHAR"),
    ("leader", "VARCHAR"),
    ("follower", "VARCHAR"),
    ("t_iso", "VARCHAR"),
    ("segment", "INTEGER"),
    ("direction", "INTEGER"),
    ("leader_mid", "DOUBLE"),
    ("leader_mid_lag2", "DOUBLE"),
    ("leader_spread", "DOUBLE"),
    ("leader_spread_ret", "DOUBLE"),
    ("ret2", "DOUBLE"),
    ("move_spread_ratio", "DOUBLE"),
    ("m0", "DOUBLE"),
    ("spread0", "DOUBLE"),
    ("m0_age_ms", "DOUBLE"),
    ("follower_own_dir", "INTEGER"),
    ("entry_ts_iso", "VARCHAR"),
    ("entry_age_ms", "DOUBLE"),
    ("m_entry_05", "DOUBLE"),
    ("spread_entry_05", "DOUBLE"),
    ("exclusion_reason", "VARCHAR"),
    ("bridge_reason", "VARCHAR"),
) + tuple(
    (f"{prefix}_{int(h)}s", "DOUBLE")
    for h in ALL_HORIZONS
    for prefix in ("m", "screen_drift_bps", "screen_edge_bps", "bridge_drift_bps",
                   "bridge_edge_bps", "full_spread_edge_bps")
)


def _event_tuple(row: EventRow) -> tuple:
    base = (
        row.trade_date,
        row.pair,
        row.leader,
        row.follower,
        row.t_iso,
        row.segment,
        row.direction,
        row.leader_mid,
        row.leader_mid_lag2,
        row.leader_spread,
        row.leader_spread_ret,
        row.ret2,
        row.move_spread_ratio,
        row.m0,
        row.spread0,
        row.m0_age_ms,
        row.follower_own_dir,
        row.entry_ts_iso,
        row.entry_age_ms,
        row.m_entry_05,
        row.spread_entry_05,
        row.exclusion_reason,
        row.bridge_reason,
    )
    horizons: list[float | None] = []
    for h in ALL_HORIZONS:
        horizons.extend(
            [
                row.mh.get(h),
                row.screen_drift.get(h),
                row.screen_edge.get(h),
                row.bridge_drift.get(h),
                row.bridge_edge.get(h),
                row.full_spread_edge.get(h),
            ]
        )
    return base + tuple(horizons)


def write_events_parquet(rows: Sequence[EventRow], path: Path) -> int:
    """Write the event table. Floats are stored unrounded, as the spec demands."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ", ".join(f'"{name}" {sqltype}' for name, sqltype in EVENT_COLUMNS)
    placeholders = ", ".join("?" for _ in EVENT_COLUMNS)
    con = duckdb.connect()
    try:
        con.execute(f"CREATE TABLE events ({cols})")
        if rows:
            con.executemany(
                f"INSERT INTO events VALUES ({placeholders})",
                [_event_tuple(r) for r in rows],
            )
        con.execute(
            f"COPY events TO '{path.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        con.close()
    return len(rows)


def write_json(doc: object, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=True), encoding="utf-8")
