#!/usr/bin/env python3
"""Structure-level exit logic. Pure functions, no I/O, fully testable.

Round-2 fix for the v2.0 defect where exits were managed per contract: a
multi-leg structure whose legs are closed independently can leave a naked
short leg behind — the one shape this policy forbids. From v2.1 on, a
structure is a structure: it is marked, decided, and closed as one unit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from strategy.proposal import OptionLeg, Proposal


@dataclass
class GroupView:
    """One open structure, reconstructed from broker positions + meta."""
    group_id: str
    engine: str
    underlying: str
    expiry: str                    # ISO date or ""
    kind: str                      # "debit" | "credit"
    entry_net: float               # signed: debit positive, credit negative
    ref_amount: float              # debit paid or credit received (positive)
    take_profit_fraction: float
    stop_loss_fraction: float      # debits: fraction of debit; credits: multiple of credit
    event_exit_date: str = ""      # ISO date, e.g. the session after earnings
    event_exit_time: str = ""      # "09:35" ET
    legs: list = field(default_factory=list)   # (symbol, side, qty)


def group_key(engine: str, underlying: str, expiry: str,
              entered_at: str) -> str:
    return f"{engine}:{underlying}:{expiry}:{entered_at}"


def net_of(entry_net: float, prices: dict) -> float:
    """Recompute a structure's signed net value from signed leg prices.

    prices: {symbol: signed_price} where a short leg carries a negative price
    (its cost to close). net = sum. For a debit spread at entry net=+D; later
    net=V; pnl = V - D. For a credit spread entry net=-C; later net=-V;
    pnl = (-V) - (-C) = C - V. One formula covers both.
    """
    return sum(prices.values())


def pnl_of(entry_net: float, net: float) -> float:
    return net - entry_net


def decide_exit(gv: GroupView, pnl: float, *, now_et: datetime,
                final_date: str, flatten_at: str,
                zero_dte_time_stop: str = "15:15") -> str | None:
    """Reason to close, or None. Never closes without a reason."""
    today = now_et.date().isoformat()
    hhmm = (now_et.hour, now_et.minute)

    tp_abs = gv.take_profit_fraction * gv.ref_amount
    if gv.kind == "credit":
        sl_abs = gv.stop_loss_fraction * gv.ref_amount
    else:
        sl_abs = gv.stop_loss_fraction * gv.ref_amount
    if pnl >= tp_abs:
        return f"take-profit ${pnl:,.0f}"
    if pnl <= -sl_abs:
        return f"stop-loss ${pnl:,.0f}"

    if gv.expiry == today:
        hh, mm = map(int, zero_dte_time_stop.split(":"))
        if hhmm >= (hh, mm):
            return "zero-DTE time-stop"

    if gv.event_exit_date == today and gv.event_exit_time:
        hh, mm = map(int, gv.event_exit_time.split(":"))
        if hhmm >= (hh, mm):
            return "post-event time-stop"

    if today == final_date:
        hh, mm = map(int, flatten_at.split(":"))
        if hhmm >= (hh, mm):
            return "final-day flatten"

    return None


def build_close_proposal(gv: GroupView, touch_prices: dict) -> Proposal:
    """Opposing limit legs at the touch. sell at bid, buy at ask.

    touch_prices: {symbol: limit_price}. Sides flip from the group's legs.
    The net limit is the sum of signed touches, which is exactly the cost or
    proceeds of closing the structure. DAY limit, never market.
    """
    legs = []
    net = 0.0
    for symbol, side, qty in gv.legs:
        price = touch_prices.get(symbol)
        if price is None or qty <= 0:
            continue
        flip = "sell" if side == "buy" else "buy"
        legs.append(OptionLeg(symbol=symbol, side=flip, quantity=qty,
                              strike=0.0, contract_type="",
                              expiration=date.min,
                              ref_bid=price, ref_ask=price))
        # selling yields +price, buying costs -price
        net += price if flip == "sell" else -price
    # Alpaca's mleg convention: positive limit = debit to pay, negative =
    # credit to receive. net is proceeds-minus-cost, so the wire price is -net.
    return Proposal(
        engine="exit", underlying=gv.underlying, direction="neutral",
        structure="close_structure", legs=legs, limit_price=round(-net, 2),
        max_loss_dollars=0.0, thesis="close structure", reason="EXIT")
