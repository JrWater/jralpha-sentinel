#!/usr/bin/env python3
"""Position sizing. Fractions of DECLARED STARTING equity, never of current.

The per_trade_risk gate enforces the outer bound; this layer applies the
stricter per-engine caps and the portfolio at-risk cap, so a drawdown shrinks
absolute risk instead of rescaling the same aggression downward.
"""
from __future__ import annotations

from dataclasses import dataclass

from strategy.proposal import Proposal

MIN_QUANTITY = 1


@dataclass
class PortfolioState:
    """What sizing needs to know about the open book."""
    max_loss_by_underlying: dict[str, float]
    max_loss_total: float
    count_by_engine: dict[str, int]
    current_equity: float
    starting_equity: float
    day_pl_dollars: float = 0.0
    loss_days_in_row: int = 0
    is_final_date: bool = False

    def count(self, engine: str) -> int:
        return self.count_by_engine.get(engine, 0)


def engine_cap(manifest, engine: str) -> float:
    start = float(manifest.get("environment", "required_starting_equity"))
    cfg = manifest.get("strategies", engine)
    key = "max_loss_per_trade_fraction"
    frac = float(cfg.get(key, 0.01)) if isinstance(cfg, dict) else 0.01
    return start * frac


def fixed_quantity(proposal: Proposal, manifest, engine: str,
                   state: PortfolioState) -> Proposal | None:
    """Size by max loss: how many contracts keep the trade inside its cap.

    Returns None when the trade is refused at this layer (too big for the
    engine cap at even one contract, or the portfolio at-risk cap would be
    exceeded, or a mandated cap exists per engine).
    """
    cap = engine_cap(manifest, engine)
    max_per_contract = proposal.max_loss_dollars  # with qty == 1
    if max_per_contract <= 0:
        return None

    # one contract already over the engine cap -> refuse
    if max_per_contract > cap:
        return None

    qty = max(MIN_QUANTITY, int(cap // max_per_contract))

    # portfolio at-risk cap (sum of max losses of open book + this trade)
    at_risk_cap = (float(manifest.get("risk_caps", "at_risk_cap_fraction"))
                   * float(manifest.get("environment", "required_starting_equity")))
    if proposal.max_loss_dollars * qty + state.max_loss_total > at_risk_cap:
        # try to shrink to what fits; never below 1 contract of an approved trade
        fit = int((at_risk_cap - state.max_loss_total) // proposal.max_loss_dollars)
        if fit < MIN_QUANTITY:
            return None
        qty = min(qty, fit)

    out = _resize(proposal, qty)
    out.max_loss_dollars = round(out.max_loss_dollars, 2)
    if out.max_loss_dollars <= 0:
        return None
    return out


def _resize(p: Proposal, qty: int) -> Proposal:
    from dataclasses import replace
    new_legs = []
    for leg in p.legs:
        from strategy.proposal import OptionLeg
        new_legs.append(OptionLeg(
            symbol=leg.symbol, side=leg.side, quantity=qty,
            strike=leg.strike, contract_type=leg.contract_type,
            expiration=leg.expiration, ref_bid=leg.ref_bid,
            ref_ask=leg.ref_ask))
    factor = qty
    return replace(p, legs=new_legs,
                   max_loss_dollars=round(p.max_loss_dollars * factor, 2),
                   max_gain_dollars=(round(p.max_gain_dollars * factor, 2)
                                     if p.max_gain_dollars is not None else None))
