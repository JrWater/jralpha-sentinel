#!/usr/bin/env python3
"""Structure selection: turn a signal + a chain into a priced Proposal.

Pricing model: Black-Scholes on the real-time underlying (IEX stock feed is
real-time on the free tier) with implied volatility taken from the delayed
indicative option snapshot. Volatility moves slowly; the underlying does not.
A 15-minute-old bid/ask is therefore only used as a *sanity check* on the
fair value, never as the price itself.

All limits are DAY limits; the market_session gate plus a fresh underlying
keep the "stale quote" failure mode from ever mattering.
"""
from __future__ import annotations

from datetime import date, timedelta
from math import sqrt

from strategy.data import ChainContract, contract_symbol
from strategy.indicators import black_scholes, bs_delta, ivr, realized_vol
from strategy.proposal import OptionLeg, Proposal

RATE = 0.045


def _t_year(expiry: date, now: date) -> float:
    return max((expiry - now).days + 1, 1) / 365.0


def _pick(contracts: list[ChainContract], s: float, expiry: date,
          ctype: str, delta_target: float, tol: float,
          now: date) -> ChainContract | None:
    """Contract of a type whose delta is closest to the target, within tol."""
    best, best_abs = None, None
    for c in contracts:
        if c.expiration != expiry or c.contract_type != ctype:
            continue
        d = c.delta if c.delta is not None else bs_delta(
            s, c.strike, _t_year(expiry, now), c.iv or 0.30, RATE,
            is_call=(ctype == "call"))
        # greeks report put deltas as negative; compare magnitude
        if abs(abs(d) - delta_target) > tol:
            continue
        err = abs(abs(d) - delta_target)
        if best_abs is None or err < best_abs:
            best, best_abs = c, err
    return best


def _fair(c: ChainContract, s: float, now: date) -> float:
    """BS fair value using the snapshot IV (or a vol fallback)."""
    sigma = c.iv if c.iv and c.iv > 0.02 else 0.30
    return black_scholes(s, c.strike, _t_year(c.expiration, now), sigma, RATE,
                         is_call=(c.contract_type == "call"))


def _leg(c: ChainContract, side: str, qty: int) -> OptionLeg:
    return OptionLeg(
        symbol=c.symbol, side=side, quantity=qty, strike=c.strike,
        contract_type=c.contract_type, expiration=c.expiration,
        ref_bid=c.bid or 0.0, ref_ask=c.ask or 0.0)


def _shift(contracts: list[ChainContract], base: ChainContract,
           strikes: int, *, up: bool) -> ChainContract | None:
    """A contract on the same expiry/type, `strikes` steps above/below."""
    same = sorted((c for c in contracts
                   if c.expiration == base.expiration
                   and c.contract_type == base.contract_type),
                  key=lambda c: c.strike)
    idx = next((i for i, c in enumerate(same) if c.strike == base.strike), None)
    if idx is None:
        return None
    j = idx + strikes if up else idx - strikes
    if 0 <= j < len(same):
        return same[j]
    return None


def build_debit_vertical(s: float, now: date, expiry: date,
                         underlying: str, direction: int,
                         contracts: list[ChainContract],
                         delta_target: float, tol: float,
                         width_strikes: int) -> Proposal | None:
    """Long debit vertical: call spread for long, put spread for short."""
    ctype = "call" if direction > 0 else "put"
    long_leg = _pick(contracts, s, expiry, ctype, delta_target, tol, now)
    if long_leg is None:
        return None
    short_leg = _shift(contracts, long_leg, width_strikes,
                       up=(direction > 0)) if direction > 0 else \
        _shift(contracts, long_leg, width_strikes, up=False)
    if short_leg is None:
        return None

    fl, fs = _fair(long_leg, s, now), _fair(short_leg, s, now)
    debit = round(max(fl - fs, 0.0), 2)
    if debit <= 0.01:
        return None

    width = abs(short_leg.strike - long_leg.strike)
    qty = 1  # sized later by the sizing layer
    return Proposal(
        engine="", underlying=underlying,
        direction="long" if direction > 0 else "short",
        structure="debit_vertical", expiry=expiry,
        dte=(expiry - now).days,
        legs=[_leg(long_leg, "buy", qty), _leg(short_leg, "sell", qty)],
        limit_price=debit,
        max_loss_dollars=round(debit * 100 * qty, 2),
        max_gain_dollars=round((width - debit) * 100 * qty, 2),
        reason=(f"debit vertical {long_leg.symbol}/{short_leg.symbol} "
                f"@ {debit:.2f} (a {width:g}pt wing)"),
    )


def build_credit_vertical(s: float, now: date, expiry: date,
                          underlying: str, direction: int,
                          contracts: list[ChainContract],
                          short_delta: float, tol: float,
                          width_pts: float) -> Proposal | None:
    """Credit vertical: bull-put spread (long bias) or bear-call spread."""
    ctype = "put" if direction > 0 else "call"
    short_leg = _pick(contracts, s, expiry, ctype, short_delta, tol, now)
    if short_leg is None:
        return None
    # protection leg further OTM in the *opposite* risk direction
    up = direction <= 0          # bear-call: protect above; bull-put: protect below
    long_leg = _shift_pts(contracts, short_leg, width_pts, up=up)
    if long_leg is None:
        return None

    fs, fl = _fair(short_leg, s, now), _fair(long_leg, s, now)
    credit = round(max(fs - fl, 0.0), 2)
    width = abs(long_leg.strike - short_leg.strike)
    if credit <= 0.02 * width:
        return None
    qty = 1
    return Proposal(
        engine="", underlying=underlying,
        direction="long" if direction > 0 else "short",
        structure="credit_vertical", expiry=expiry,
        dte=(expiry - now).days,
        legs=[_leg(short_leg, "sell", qty), _leg(long_leg, "buy", qty)],
        limit_price=credit,
        max_loss_dollars=round((width - credit) * 100 * qty, 2),
        max_gain_dollars=round(credit * 100 * qty, 2),
        reason=(f"credit vertical {short_leg.symbol}/{long_leg.symbol} "
                f"@ {credit:.2f} credit, {width:g}pt width"),
    )


def _shift_pts(contracts: list[ChainContract], base: ChainContract,
               pts: float, *, up: bool) -> ChainContract | None:
    """Nearest same-type contract at least `pts` away in the given direction."""
    same = [c for c in contracts
            if c.expiration == base.expiration
            and c.contract_type == base.contract_type]
    target = base.strike + pts if up else base.strike - pts
    same = [c for c in same if (c.strike >= target if up else c.strike <= target)]
    if not same:
        return None
    return min(same, key=lambda c: abs(c.strike - target))


def build_straddle(s: float, now: date, expiry: date, underlying: str,
                   contracts: list[ChainContract],
                   delta_target: float = 0.50, tol: float = 0.06) -> Proposal | None:
    """ATM straddle: same strike, call + put. For a confirmed earnings event."""
    call = _pick(contracts, s, expiry, "call", delta_target, tol, now)
    if call is None:
        return None
    put = _pick(contracts, s, expiry, "put", delta_target, tol, now)
    if put is None or put.strike != call.strike:
        # prefer exact ATM on the call strike for the put leg
        put = next((c for c in contracts
                    if c.expiration == expiry and c.contract_type == "put"
                    and c.strike == call.strike), None)
    if put is None:
        return None
    fc, fp = _fair(call, s, now), _fair(put, s, now)
    debit = round(fc + fp, 2)
    if debit <= 0.02:
        return None
    return Proposal(
        engine="", underlying=underlying, direction="neutral",
        structure="straddle", expiry=expiry, dte=(expiry - now).days,
        legs=[_leg(call, "buy", 1), _leg(put, "buy", 1)],
        limit_price=debit,
        max_loss_dollars=round(debit * 100, 2),
        max_gain_dollars=None,
        reason=f"ATM straddle {call.strike:g} @ {debit:.2f}",
    )


def build_strangle(s: float, now: date, expiry: date, underlying: str,
                   contracts: list[ChainContract],
                   delta_target: float = 0.40, tol: float = 0.08) -> Proposal | None:
    """OTM strangle: call and put, both at `delta_target`."""
    call = _pick(contracts, s, expiry, "call", delta_target, tol, now)
    put = _pick(contracts, s, expiry, "put", delta_target, tol, now)
    if call is None or put is None:
        return None
    fc, fp = _fair(call, s, now), _fair(put, s, now)
    debit = round(fc + fp, 2)
    if debit <= 0.02:
        return None
    return Proposal(
        engine="", underlying=underlying, direction="neutral",
        structure="strangle", expiry=expiry, dte=(expiry - now).days,
        legs=[_leg(call, "buy", 1), _leg(put, "buy", 1)],
        limit_price=debit,
        max_loss_dollars=round(debit * 100, 2),
        max_gain_dollars=None,
        reason=f"strangle C {call.strike:g}/P {put.strike:g} @ {debit:.2f}",
    )


def build_iron_condor(s: float, now: date, expiry: date, underlying: str,
                      contracts: list[ChainContract],
                      short_delta: float = 0.16, tol: float = 0.04,
                      width_pts: float = 5.0) -> Proposal | None:
    """Four-legged range play, SPY only, defined risk = width minus credit."""
    short_call = _pick(contracts, s, expiry, "call", short_delta, tol, now)
    short_put = _pick(contracts, s, expiry, "put", short_delta, tol, now)
    if short_call is None or short_put is None:
        return None
    long_call = _shift_pts(contracts, short_call, width_pts, up=True)
    long_put = _shift_pts(contracts, short_put, width_pts, up=False)
    if long_call is None or long_put is None:
        return None
    fsc, fsp = _fair(short_call, s, now), _fair(short_put, s, now)
    flc, flp = _fair(long_call, s, now), _fair(long_put, s, now)
    credit = round((fsc + fsp) - (flc + flp), 2)
    width = width_pts
    if credit <= 0.15 * width:
        return None
    return Proposal(
        engine="", underlying=underlying, direction="neutral",
        structure="iron_condor", expiry=expiry, dte=(expiry - now).days,
        legs=[_leg(short_call, "sell", 1), _leg(long_call, "buy", 1),
              _leg(short_put, "sell", 1), _leg(long_put, "buy", 1)],
        limit_price=credit,
        max_loss_dollars=round((width - credit) * 100, 2),
        max_gain_dollars=round(credit * 100, 2),
        reason=(f"iron condor {short_put.strike:g}P/{long_put.strike:g}P "
                f"{short_call.strike:g}C/{long_call.strike:g}C @ {credit:.2f}"),
    )


def atm_iv(contracts: list[ChainContract], s: float, now: date) -> float | None:
    """Blend of ATM call/put implied vol from the chain, or None."""
    near = [c for c in contracts if c.contract_type == "call"]
    if not near:
        near = [c for c in contracts if c.contract_type == "put"]
    if not near:
        return None
    atm = min(near, key=lambda c: abs(c.strike - s))
    if atm.iv and atm.iv > 0.02:
        return float(atm.iv)
    return None


def pick_expiry(expiries: list[date], now: date, min_dte: int,
                max_dte: int) -> date | None:
    """Nearest available expiry in [min_dte, max_dte]."""
    for d in sorted(expiries):
        dte = (d - now).days
        if min_dte <= dte <= max_dte:
            return d
    return None
