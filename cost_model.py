"""JP equity transaction cost model (jpx_mlbot_15m, #8 Phase 0b / C4).

Replaces the crypto tutorial's single ``maker_fee`` column (which could be a
*negative* maker rebate) with a JP-equity round-trip cost made of:

  - 委託手数料 (commission)   : fraction of notional, per side
  - スリッページ (slippage)   : N JPX ticks per side, tick size by price band
  - 貸株料 (borrow fee)       : annual rate on SHORT notional, pro-rated by
                                 holding days (LONG pays none)

There is **no maker rebate** (JP retail equities have none), so every cost is
non-negative. ``friction_floor`` is the minimum gross edge a trade must clear
to break even — the input to the #8 pre-registered friction-floor kill gate
(R3: evaluated *per side* so a SHORT floor breach can fall back to buy-only).

Pure stdlib + config-driven: construct from a ``CostParams`` or via
``CostModel.from_config_dict(...)``. No env / data-file / pandas dependency so
the friction arithmetic is unit-testable on any machine.
"""
from __future__ import annotations

from dataclasses import dataclass

_SIDES = frozenset({"LONG", "SHORT"})

# JPX 呼値 (tick size) table for ordinary stocks (non-TOPIX100 regime), as
# ``(upper_inclusive_price, tick)`` pairs in ascending order. "X円以下" maps to
# the first row whose upper bound is >= price.
_DEFAULT_TICK_TABLE: tuple[tuple[float, float], ...] = (
    (3_000.0, 1.0),
    (5_000.0, 5.0),
    (10_000.0, 10.0),
    (30_000.0, 50.0),
    (50_000.0, 100.0),
    (300_000.0, 500.0),
    (500_000.0, 1_000.0),
    (3_000_000.0, 5_000.0),
    (5_000_000.0, 10_000.0),
    (float("inf"), 50_000.0),
)


@dataclass(frozen=True)
class CostParams:
    """All knobs are config-driven; defaults are conservative retail values."""

    commission_rate: float                       # per side, fraction of notional
    slippage_ticks: float                        # per side, in JPX ticks
    borrow_fee_annual: float                     # 貸株料 annual rate (SHORT only)
    tick_table: tuple[tuple[float, float], ...]  # (upper_inclusive, tick), ascending

    @classmethod
    def default(cls) -> "CostParams":
        # Conservative retail baseline (#8 design judgement 4). Override via config.
        return cls(
            commission_rate=0.0005,    # 5 bps per side
            slippage_ticks=1.0,        # 1 tick per side
            borrow_fee_annual=0.0365,  # 3.65%/yr ≈ 1 bp/day
            tick_table=_DEFAULT_TICK_TABLE,
        )


class CostModel:
    """Round-trip JP-equity cost as a fraction of notional."""

    def __init__(self, params: CostParams) -> None:
        self.params = params

    # -- construction -------------------------------------------------------

    @classmethod
    def from_config_dict(cls, cfg: dict) -> "CostModel":
        """Build from a parsed config mapping. Missing keys fall back to the
        conservative defaults so a partial ``cost:`` block is valid."""
        base = CostParams.default()
        section = (cfg or {}).get("cost", {}) or {}
        tick_table = section.get("tick_table")
        if tick_table is not None:
            tick_table = tuple((float(u), float(t)) for u, t in tick_table)
        else:
            tick_table = base.tick_table
        return cls(
            CostParams(
                commission_rate=float(section.get("commission_rate", base.commission_rate)),
                slippage_ticks=float(section.get("slippage_ticks", base.slippage_ticks)),
                borrow_fee_annual=float(section.get("borrow_fee_annual", base.borrow_fee_annual)),
                tick_table=tick_table,
            )
        )

    # -- tick / slippage ----------------------------------------------------

    def tick_size(self, price: float) -> float:
        """JPX 呼値 at ``price`` (first band whose upper bound >= price)."""
        if price <= 0:
            raise ValueError(f"price must be > 0, got {price!r}")
        for upper, tick in self.params.tick_table:
            if price <= upper:
                return tick
        # tick_table ends with an inf upper bound, so this is unreachable.
        return self.params.tick_table[-1][1]

    def slippage_frac(self, price: float) -> float:
        """Per-side slippage as a fraction of notional = ticks * tick / price."""
        return self.params.slippage_ticks * self.tick_size(price) / price

    # -- cost ---------------------------------------------------------------

    def one_way_cost_frac(self, price: float) -> float:
        """Per-side cost fraction (commission + slippage). Always >= 0."""
        return self.params.commission_rate + self.slippage_frac(price)

    def round_trip_cost_frac(
        self, price: float, side: str, holding_days: float
    ) -> float:
        """Entry+exit cost fraction. SHORT adds borrow fee pro-rated by days.

        ``price`` is the reference (entry) price for tick/slippage sizing.
        """
        if side not in _SIDES:
            raise ValueError(f"side must be one of {sorted(_SIDES)}, got {side!r}")
        cost = 2.0 * self.one_way_cost_frac(price)
        if side == "SHORT":
            cost += self.params.borrow_fee_annual * (holding_days / 365.0)
        return cost

    def friction_floor(self, price: float, side: str, holding_days: float) -> float:
        """Minimum gross edge to break even = the round-trip cost (per side)."""
        return self.round_trip_cost_frac(price, side, holding_days)
