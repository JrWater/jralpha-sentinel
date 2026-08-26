#!/usr/bin/env python3
"""Empirically validate the "bypass the 15-minute delay chain" design claim.

The strategy's central bet is that a stale options quote is safe to trade
against IF: the strike is picked by delta (insensitive to a stale price),
the fill is priced fresh via Black-Scholes on the real-time underlying, and
the order is always a limit, never a market order. That is a claim about
real exchange behavior, not something a unit test can prove. This script
proves it, or disproves it, with one real order.

It measures three things with a single same-expiry debit vertical:
  1. options quote delay   — each contract's quote_ts vs wall-clock, right now
  2. multi-leg limit fills — a real mleg DAY limit order, tracked to fill
  3. BS pricing vs fill    — Black-Scholes theoretical (same formula the
     strategy uses to pick strikes) vs the mid quoted and the price actually
     filled at

SAFETY — this is the one thing that must never go wrong: this script talks
ONLY to the pre-existing legacy paper account (~/.openclaw/.env). It never
reads ~/jralpha-sentinel/.env, and it asserts the account it connected to is
NOT the manifest's competition_account_id before doing anything else. The
competition account must read exactly $100,000 until kickoff — see
gates/checks.py::check_competition_window for the mechanical version of this
same rule inside the real pipeline.

It also never touches state/decisions.jsonl (the file the public dashboard
snapshot is built from) — it calls client.submit_order() directly rather
than going through agent/executor.py's Executor.submit(), specifically so a
validation order can never be mistaken for a competition decision by anyone
reading the dashboard. Its own record goes to state/legacy_validation.jsonl.

Default mode is a DRY RUN — it fetches real data, prices the real vertical,
and prints exactly what it would submit, but places no order. Pass --live to
actually submit. This mirrors the project's own default: unregistered/
unconfirmed defaults to the safe side, never the active one.

Usage:
    .venv/bin/python scripts/validate_delay_chain.py                  # dry run, SPY
    .venv/bin/python scripts/validate_delay_chain.py --underlying QQQ # dry run, QQQ
    .venv/bin/python scripts/validate_delay_chain.py --live           # real order
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m")

LEGACY_ENV = Path.home() / ".openclaw" / ".env"
RECORD_PATH = ROOT / "state" / "legacy_validation.jsonl"


def load_env(path: Path) -> dict:
    env: dict = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def creds(env: dict) -> tuple:
    key = env.get("ALPACA_API_KEY")
    secret = env.get("ALPACA_SECRET_KEY") or env.get("ALPACA_API_SECRET")
    return key, secret


def log(msg: str) -> None:
    print(msg)


def record(kind: str, **fields) -> None:
    RECORD_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"kind": kind, "at_utc": datetime.now(timezone.utc).isoformat(), **fields}
    with RECORD_PATH.open("a") as f:
        f.write(json.dumps(row, default=str) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--underlying", default="SPY")
    ap.add_argument("--live", action="store_true",
                    help="actually submit the order; default is dry-run preview only")
    ap.add_argument("--max-wait-seconds", type=int, default=180)
    args = ap.parse_args()

    from alpaca.trading.enums import OrderClass, OrderSide, PositionIntent, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

    from policy.loader import load as load_manifest
    from strategy.data import AlpacaData
    from strategy.indicators import black_scholes

    manifest = load_manifest()
    key, secret = creds(load_env(LEGACY_ENV))
    if not key or not secret:
        log(f"{RED}No legacy credentials in {LEGACY_ENV}.{RESET} Aborting — "
            f"refusing to fall back to any other .env.")
        return 2

    data = AlpacaData(key, secret)
    account = data.account()
    competition_id = manifest.get("environment", "competition_account_id",
                                  default=None)

    log(f"\n{DIM}manifest {manifest.identity}{RESET}")
    log(f"connected account: {account.account_number}  "
        f"(competition account is {competition_id})\n")

    if account.account_number == competition_id:
        log(f"{RED}ABORT.{RESET} Connected to the COMPETITION account. "
            f"This script must only ever run against the legacy account. "
            f"Refusing to proceed regardless of --live.")
        return 3
    log(f"{GREEN}PASS{RESET} — this is the legacy account, not the "
        f"competition account. Safe to proceed.\n")

    # ── 1. quote delay ──────────────────────────────────────────────────────
    chain = data.option_chain(args.underlying)
    if not chain:
        log(f"{RED}No option chain returned for {args.underlying}.{RESET}")
        return 4
    expiry = chain[0].expiration
    now_utc = datetime.now(timezone.utc)
    ages = [
        (now_utc - c.quote_ts).total_seconds()
        for c in chain if c.quote_ts is not None
    ]
    if ages:
        log(f"quote delay — {len(ages)} contracts, expiry {expiry}: "
            f"min {min(ages):.0f}s / median {sorted(ages)[len(ages)//2]:.0f}s / "
            f"max {max(ages):.0f}s old, right now")
    else:
        log(f"{YELLOW}no quote_ts on any contract — chain may be from a "
            f"closed session{RESET}")

    quotes = data.latest_quotes([args.underlying])
    q = quotes.get(args.underlying)
    if q is None:
        log(f"{RED}No real-time quote for {args.underlying}.{RESET}")
        return 4
    spot = (float(q.bid_price) + float(q.ask_price)) / 2.0
    log(f"real-time IEX spot {args.underlying}: {spot:.2f} "
        f"(bid {q.bid_price} / ask {q.ask_price})\n")

    # ── 2. pick a simple 1-wide same-expiry debit call vertical ────────────
    calls = sorted(
        (c for c in chain if c.contract_type == "call" and c.bid and c.ask),
        key=lambda c: c.strike)
    if len(calls) < 2:
        log(f"{RED}Not enough quoted call strikes to build a vertical.{RESET}")
        return 4
    long_leg = min(calls, key=lambda c: abs(c.strike - spot))
    wider = [c for c in calls if c.strike > long_leg.strike]
    if not wider:
        log(f"{RED}No strike above the ATM leg — can't build a vertical.{RESET}")
        return 4
    short_leg = min(wider, key=lambda c: c.strike)

    t_year = max((expiry - date.today()).days + 1, 1) / 365.0
    theo_long = black_scholes(spot, long_leg.strike, t_year,
                              long_leg.iv or 0.30, is_call=True)
    theo_short = black_scholes(spot, short_leg.strike, t_year,
                               short_leg.iv or 0.30, is_call=True)
    theo_net = round(theo_long - theo_short, 4)
    quoted_net = round(long_leg.mid - short_leg.mid, 4)
    width = short_leg.strike - long_leg.strike
    max_loss = round(quoted_net * 100, 2)

    log(f"vertical: BUY {long_leg.symbol} (K={long_leg.strike}) / "
        f"SELL {short_leg.symbol} (K={short_leg.strike}), width {width:g}, "
        f"expiry {expiry}")
    log(f"  BS theoretical net debit : {theo_net:.4f}  "
        f"(spot={spot:.2f}, iv_long={long_leg.iv}, iv_short={short_leg.iv})")
    log(f"  quoted mid net debit     : {quoted_net:.4f}  "
        f"(long mid {long_leg.mid} / short mid {short_leg.mid})")
    log(f"  max loss @ quoted mid    : ${max_loss:,.2f}  (qty=1)\n")

    if max_loss > 150:
        log(f"{RED}ABORT.{RESET} max loss ${max_loss:,.2f} exceeds this "
            f"script's $150 sanity ceiling for a validation test — pick a "
            f"tighter-width vertical or a different underlying.")
        return 5

    record("priced", underlying=args.underlying, expiry=str(expiry),
          long_symbol=long_leg.symbol, short_symbol=short_leg.symbol,
          theo_net=theo_net, quoted_net=quoted_net, spot=spot,
          quote_ages_sec=ages)

    if not args.live:
        log(f"{YELLOW}DRY RUN — no order submitted.{RESET} Re-run with "
            f"--live during market hours to actually place this and "
            f"measure the fill.")
        return 0

    # ── 3. submit, poll, and immediately flatten ────────────────────────────
    legs = [
        OptionLegRequest(symbol=long_leg.symbol, ratio_qty=1,
                         side=OrderSide.BUY,
                         position_intent=PositionIntent.BUY_TO_OPEN),
        OptionLegRequest(symbol=short_leg.symbol, ratio_qty=1,
                         side=OrderSide.SELL,
                         position_intent=PositionIntent.SELL_TO_OPEN),
    ]
    submit_ts = datetime.now(timezone.utc)
    order = data.trading.submit_order(LimitOrderRequest(
        qty=1, order_class=OrderClass.MLEG, time_in_force=TimeInForce.DAY,
        limit_price=quoted_net, legs=legs))
    log(f"SUBMITTED {order.id} net debit {quoted_net:.4f} -> {order.status}")
    record("order_submitted", order_id=str(order.id), limit=quoted_net)

    filled = None
    deadline = time.time() + args.max_wait_seconds
    while time.time() < deadline:
        o = data.trading.get_order_by_id(order.id)
        if str(o.status).endswith(("FILLED", "PARTIALLY_FILLED")):
            filled = o
            break
        if str(o.status).endswith(("CANCELED", "EXPIRED", "REJECTED")):
            log(f"{RED}order ended {o.status} without filling.{RESET}")
            record("order_ended", order_id=str(order.id), status=str(o.status))
            return 6
        time.sleep(5)

    if filled is None:
        log(f"{YELLOW}Not filled within {args.max_wait_seconds}s — "
            f"canceling.{RESET}")
        data.trading.cancel_order_by_id(order.id)
        record("order_canceled_unfilled", order_id=str(order.id))
        return 0

    fill_latency = (datetime.now(timezone.utc) - submit_ts).total_seconds()
    fill_price = float(getattr(filled, "filled_avg_price", 0) or quoted_net)
    log(f"\n{GREEN}FILLED{RESET} in {fill_latency:.1f}s at net {fill_price:.4f} "
        f"(theoretical was {theo_net:.4f}, quoted mid was {quoted_net:.4f}, "
        f"diff vs theoretical: {fill_price - theo_net:+.4f})")
    record("order_filled", order_id=str(order.id), fill_price=fill_price,
          fill_latency_sec=fill_latency, theo_net=theo_net)

    # flatten immediately — never leave a "test" position open
    close_legs = [
        OptionLegRequest(symbol=long_leg.symbol, ratio_qty=1,
                         side=OrderSide.SELL,
                         position_intent=PositionIntent.SELL_TO_CLOSE),
        OptionLegRequest(symbol=short_leg.symbol, ratio_qty=1,
                         side=OrderSide.BUY,
                         position_intent=PositionIntent.BUY_TO_CLOSE),
    ]
    close_order = data.trading.submit_order(LimitOrderRequest(
        qty=1, order_class=OrderClass.MLEG, time_in_force=TimeInForce.DAY,
        limit_price=-fill_price, legs=close_legs))
    log(f"flatten order submitted: {close_order.id} @ {-fill_price:.4f} "
        f"(closes the position we just opened; poll manually or re-run "
        f"verify_account.py --compare-legacy to confirm it clears)")
    record("flatten_submitted", order_id=str(close_order.id),
          limit=-fill_price)

    log(f"\n{GREEN}Validated{RESET}: real quote delay measured above, "
        f"real mleg limit fill in {fill_latency:.1f}s, BS theoretical vs "
        f"actual fill diff {fill_price - theo_net:+.4f}. Record: "
        f"{RECORD_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
