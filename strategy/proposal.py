#!/usr/bin/env python3
"""The inert proposal: the only thing the model may emit.

A proposal carries zero ability to submit itself — no client, no credentials,
nothing but data. The gates read ``order_class``, ``type``, ``time_in_force``,
``legs``, ``underlying`` and ``max_loss_dollars`` from it; the executor builds
the wire order from the same object. One object, two consumers, no second
translation that could disagree with the first.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class OptionLeg:
    """One leg of a multi-leg option order."""
    symbol: str                     # full OCC contract symbol, e.g. SPY260904C00770000
    side: str                       # "buy" | "sell"
    quantity: int
    strike: float
    contract_type: str              # "call" | "put"
    expiration: date
    # reference prices (from the delayed indicative chain) for reporting only
    ref_bid: float = 0.0
    ref_ask: float = 0.0

    @property
    def mid(self) -> float:
        return round((self.ref_bid + self.ref_ask) / 2.0, 4)


@dataclass
class Proposal:
    """The unit of decision. Inert by construction."""
    engine: str                     # trend_directional | trend_income | catalyst | event_macro | vol_income
    underlying: str
    direction: str                  # long | short | neutral
    structure: str                  # debit_vertical | credit_vertical | straddle | strangle | iron_condor | single_long
    legs: list[OptionLeg] = field(default_factory=list)
    expiry: date | None = None
    dte: int = 0
    limit_price: float = 0.0        # NET price: debit paid (long strategies) or credit received
    max_loss_dollars: float = 0.0
    max_gain_dollars: float | None = None
    conviction: float = 0.0         # 0..1 engine conviction, for ranking
    thesis: str = ""
    reason: str = ""                # machine-readable rationale for the decision log
    event_exit_date: str = ""       # ISO date the structure must exit (post-event)
    event_exit_time: str = ""       # "09:35" ET on that date

    # ── the attributes the pretrade gates read ────────────────────────────────
    @property
    def order_class(self) -> str:
        return "simple" if len(self.legs) == 1 else "mleg"

    @property
    def type(self) -> str:
        return "limit"

    @property
    def time_in_force(self) -> str:
        return "day"

    @property
    def is_opening(self) -> bool:
        """True when every leg opens. Used by the executor to pick position_intent."""
        return True

    def close_proposal(self) -> "Proposal":
        """Build the mirror proposal that closes this one (sell-to-close etc.).

        Sides flip, the net limit is the opposite side of the current edge,
        and the intent is set by the caller. The shape attributes are
        unchanged, so the same declared shape covers both directions.
        """
        legs = []
        for leg in self.legs:
            legs.append(OptionLeg(
                symbol=leg.symbol,
                side="sell" if leg.side == "buy" else "buy",
                quantity=leg.quantity,
                strike=leg.strike,
                contract_type=leg.contract_type,
                expiration=leg.expiration,
                ref_bid=leg.ref_bid,
                ref_ask=leg.ref_ask,
            ))
        return Proposal(
            engine=self.engine, underlying=self.underlying,
            direction=self.direction, structure=self.structure,
            legs=legs, expiry=self.expiry, dte=self.dte,
            limit_price=0.0, max_loss_dollars=0.0,
            max_gain_dollars=self.max_gain_dollars,
            conviction=self.conviction,
            thesis=f"close {self.structure} on {self.underlying}: {self.thesis}",
            reason="EXIT",
        )
