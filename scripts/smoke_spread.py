#!/usr/bin/env python3
"""End-to-end smoke test: select a credit spread, gate it, optionally submit.

This is the single highest-risk unknown in the build. Everything downstream
assumes that a multi-leg options order, selected off a 15-minute-delayed chain
and priced with a limit, is actually accepted by Alpaca on this account. If
that is false, the strategy has to change, and it is far better to learn it now
than at 06:30 on competition morning.

It doubles as the first real exercise of the pretrade gates: the proposal built
here is the same shape the agent will emit, and it goes through the same
`gates.checks` functions rather than a parallel code path built for testing.

    .venv/bin/python scripts/smoke_spread.py                # dry run
    .venv/bin/python scripts/smoke_spread.py --submit       # actually send
    .venv/bin/python scripts/smoke_spread.py --close-all    # flatten
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (ContractType, OrderClass, OrderSide,
                                  OrderType, PositionIntent, TimeInForce)
from alpaca.trading.requests import (GetOptionContractsRequest,
                                     LimitOrderRequest, OptionLegRequest)

from gates import checks
from gates.registry import GateResult, severity_of
from policy.loader import load as load_manifest
from scripts.verify_account import creds, load_env

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m")


@dataclass
class Leg:
    symbol: str
    side: str          # "buy" | "sell"
    strike: float
    delta: float | None
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return round((self.bid + self.ask) / 2, 2)


@dataclass
class Proposal:
    """What the agent emits. Inert: it carries no ability to submit itself."""
    underlying: str
    structure: str
    order_class: str
    type: str
    time_in_force: str
    legs: list = field(default_factory=list)
    limit_price: float = 0.0
    max_loss_dollars: float = 0.0
    credit: float = 0.0
    rationale: str = ""


def select_credit_spread(manifest, chain, underlying: str,
                         expiry_window: tuple) -> Proposal | None:
    """Pick a put credit spread from the chain by delta, per the manifest.

    Every threshold here is read from the manifest rather than written into
    this function. The manifest is the parameter authority; this file is only
    the mechanism that applies it.
    """
    cfg = manifest.get("strategies", "trend_income")
    target = float(cfg["target_short_delta"])
    tol = float(cfg["delta_tolerance"])
    width = 5.0  # manifest v2 fixed SPY/QQQ width
    min_credit_frac = float(cfg["min_credit_fraction_of_width"])

    puts = {}
    for sym, snap in chain.items():
        greeks = getattr(snap, "greeks", None)
        quote = getattr(snap, "latest_quote", None)
        if not greeks or not quote or quote.bid_price is None:
            continue
        # OCC symbol: SPY 260828 P 00600000
        body = sym[len(underlying):]
        exp = datetime.strptime(body[:6], "%y%m%d").date()
        if not (expiry_window[0] <= exp <= expiry_window[1]):
            continue
        if body[6] != "P":
            continue
        strike = int(body[7:]) / 1000.0
        puts[strike] = (sym, exp, abs(greeks.delta or 0.0),
                        float(quote.bid_price), float(quote.ask_price))

    if not puts:
        return None

    # Short leg: delta closest to target, inside tolerance.
    candidates = [(abs(d - target), k, v)
                  for k, v in puts.items()
                  for d in (v[2],) if abs(d - target) <= tol]
    if not candidates:
        return None
    _, short_strike, short = min(candidates, key=lambda c: c[0])

    long_strike = short_strike - width
    if long_strike not in puts:
        return None
    long = puts[long_strike]

    short_leg = Leg(short[0], "sell", short_strike, short[2], short[3], short[4])
    long_leg = Leg(long[0], "buy", long_strike, long[2], long[3], long[4])

    credit = round(short_leg.mid - long_leg.mid, 2)
    if credit < width * min_credit_frac:
        return None

    return Proposal(
        underlying=underlying,
        structure="vertical_credit_spread",
        order_class="mleg",
        type="limit",
        time_in_force="day",
        legs=[short_leg, long_leg],
        limit_price=-credit,
        credit=credit,
        max_loss_dollars=round((width - credit) * 100, 2),
        rationale=(f"short {short_strike:g}P delta {short_leg.delta:.3f}, "
                   f"long {long_strike:g}P, width ${width:g}, "
                   f"credit ${credit:.2f}"),
    )


def run_pretrade_gates(manifest, ctx_kwargs, proposal) -> bool:
    """Run every pretrade gate. Any BLOCKING failure refuses the proposal."""
    ctx = checks.EvalContext(manifest=manifest, proposal=proposal, **ctx_kwargs)
    print(f"\n  {DIM}pretrade gates{RESET}")
    allowed = True
    for gate in checks.GATES:
        if gate.phase != "pretrade":
            continue
        try:
            result = gate.check(ctx)
        except Exception as exc:                              # noqa: BLE001
            result = GateResult(False, f"{type(exc).__name__}: {exc}")
        mark = f"{GREEN}PASS{RESET}" if result.ok else (
            f"{RED}FAIL{RESET}" if gate.severity == "BLOCKING"
            else f"{YELLOW}WARN{RESET}")
        print(f"    [{mark}] {gate.name:<24} {result.detail}")
        if not result.ok and severity_of(checks.GATES, gate.name) == "BLOCKING":
            allowed = False
    return allowed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--submit", action="store_true")
    ap.add_argument("--close-all", action="store_true")
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--env", default=".env.dev",
                    help="credentials file. Defaults to the DEV account: "
                         "the safe path is the default path, so reaching "
                         "the competition account takes a deliberate act.")
    args = ap.parse_args()

    manifest = load_manifest()
    key, secret = creds(load_env(ROOT / args.env))
    trading = TradingClient(key, secret, paper=True)
    data = OptionHistoricalDataClient(key, secret)

    account = trading.get_account()
    clock = trading.get_clock()
    positions = [p for p in trading.get_all_positions()
                 if p.asset_class == "us_option"]

    print(f"{DIM}manifest {manifest.identity}{RESET}")
    print(f"{DIM}account  {account.account_number}  equity ${float(account.equity):,.2f}  "
          f"market {'OPEN' if clock.is_open else 'CLOSED'}{RESET}")

    if args.close_all:
        if not positions:
            print("no option positions to close")
            return 0
        for p in positions:
            trading.close_position(p.symbol)
            print(f"  closing {p.symbol} qty {p.qty}")
        return 0

    today = datetime.now(timezone.utc).date()
    cfg = manifest.get("strategies", "trend_income")
    window = (today + timedelta(days=int(cfg["min_dte"])),
              today + timedelta(days=int(cfg["max_dte"])))
    print(f"\nselecting {args.symbol} put credit spread, expiry "
          f"{window[0]} .. {window[1]}")

    chain = data.get_option_chain(OptionChainRequest(
        underlying_symbol=args.symbol))
    proposal = select_credit_spread(manifest, chain, args.symbol, window)

    if proposal is None:
        print(f"{YELLOW}no spread met the manifest's criteria{RESET} — this is "
              f"a refusal, not an error. Widening thresholds to force a trade "
              f"is a policy change, not a runtime decision.")
        return 1

    print(f"\n  {proposal.rationale}")
    for leg in proposal.legs:
        print(f"    {leg.side:<4} {leg.symbol}  bid {leg.bid:.2f} ask "
              f"{leg.ask:.2f} mid {leg.mid:.2f}  delta {leg.delta:.3f}")
    print(f"\n  credit ${proposal.credit:.2f}  max loss "
          f"${proposal.max_loss_dollars:,.2f}")

    ctx_kwargs = dict(
        now_utc=datetime.now(timezone.utc), account=account,
        is_paper_session=True, clock=clock, positions=positions,
        ledger_positions=[], option_quote_age_seconds=900.0,
        underlying_bar_age_seconds=60.0, decision_log_writable=True,
        git_head="0" * 40, git_dirty=False)

    if not run_pretrade_gates(manifest, ctx_kwargs, proposal):
        print(f"\n{RED}REFUSED{RESET} by a BLOCKING gate. The proposal does not "
              f"become an order.")
        return 1
    print(f"\n  {GREEN}all pretrade gates pass{RESET}")

    if not args.submit:
        print(f"\n{DIM}dry run — pass --submit to send it{RESET}")
        return 0

    order = trading.submit_order(LimitOrderRequest(
        qty=1,
        order_class=OrderClass.MLEG,
        time_in_force=TimeInForce.DAY,
        limit_price=-proposal.credit,
        legs=[OptionLegRequest(
            symbol=leg.symbol, ratio_qty=1,
            side=OrderSide.SELL if leg.side == "sell" else OrderSide.BUY,
            position_intent=(PositionIntent.SELL_TO_OPEN if leg.side == "sell"
                             else PositionIntent.BUY_TO_OPEN))
            for leg in proposal.legs],
    ))
    print(f"\n{GREEN}SUBMITTED{RESET} id={order.id} status={order.status} "
          f"class={order.order_class} legs={len(order.legs or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
