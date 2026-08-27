#!/usr/bin/env python3
"""Position sizing. Fractions of DECLARED STARTING equity, never of current.

The per_trade_risk gate enforces the outer bound; this layer applies the
stricter per-engine caps and the portfolio at-risk cap, so a drawdown shrinks
absolute risk instead of rescaling the same aggression downward. v2.1 adds
the daily drawdown scale (a killed day halves the next day's sizes) and a
per-engine cap-key override (the NFP gap add-on is smaller than the strangle).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from strategy.proposal import Proposal

MIN_QUANTITY = 1


@dataclass
class PortfolioState:
    """What sizing needs to know about the open book."""
    max_loss_by_underlying: dict[str, float] = field(default_factory=dict)
    max_loss_total: float = 0.0
    count_by_engine: dict[str, int] = field(default_factory=dict)
    current_equity: float = 100000.0
    starting_equity: float = 100000.0
    day_pl_dollars: float = 0.0
    loss_days_in_row: int = 0
    is_final_date: bool = False
    scale: float = 1.0          # 0.5 the day after a killed day

    def count(self, engine: str) -> int:
        return self.count_by_engine.get(engine, 0)


def record_open_risk(state: PortfolioState, dollars: float,
                     cap: float) -> bool:
    """True when this trade's max loss still fits under the at-risk cap.

    The counterpart to daystate.record_risk, which does exactly this job for
    the daily new-exposure cap, and called from the same place for the same
    reason: risk is taken on when an order is SENT, not when a candidate is
    built. The engine builds more candidates than the proposer selects, so
    reserving budget at build time would hold capital against trades that
    never open.

    Before this existed the at-risk cap was measured only against positions
    that were already open when the cycle began — fixed_quantity() read
    PortfolioState.max_loss_total and nothing ever moved it — so siblings in
    one cycle each spent the same headroom and the book could finish well
    past a cap the write-up calls hard.

    Refusal is whole-trade, not a trim, matching record_risk. A candidate
    that does not fit is skipped rather than shrunk: the two caps then behave
    identically at the submission layer, which is worth more than squeezing
    the last few hundred dollars of headroom out of the ranked tail.
    """
    if state.max_loss_total + dollars > cap:
        return False
    state.max_loss_total += dollars
    return True


def release_open_risk(state: PortfolioState, dollars: float) -> None:
    """Counterpart to record_open_risk, for a submit that never happened."""
    state.max_loss_total = max(0.0, state.max_loss_total - dollars)


def engine_cap(manifest, engine: str, cap_key: str | None = None,
               scale: float = 1.0) -> float:
    """The engine's per-trade max-loss cap, in dollars.

    cap_key overrides which manifest key holds the fraction (the Event Vector
    has one cap for the strangle and a smaller one for the gap add-on). scale
    is the drawdown scale carried in PortfolioState.
    """
    start = float(manifest.get("environment", "required_starting_equity"))
    cfg = manifest.get("strategies", engine)
    key = cap_key or "max_loss_per_trade_fraction"
    frac = float(cfg.get(key, 0.01)) if isinstance(cfg, dict) else 0.01
    return start * frac * scale


def fixed_quantity(proposal: Proposal, manifest, engine: str,
                   state: PortfolioState,
                   cap_key: str | None = None) -> Proposal | None:
    """Size by max loss: how many contracts keep the trade inside its cap.

    Returns None when the trade is refused at this layer (too big for the
    engine cap at even one contract, or the portfolio at-risk cap would be
    exceeded).
    """
    scale = getattr(state, "scale", 1.0) or 1.0
    cap = engine_cap(manifest, engine, cap_key=cap_key, scale=scale)
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
