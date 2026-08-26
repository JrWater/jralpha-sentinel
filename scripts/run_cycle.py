#!/usr/bin/env python3
"""One decision cycle: verify -> snapshot -> gates -> engine -> proposer ->
pretrade gates -> execute -> manage exits. Run every 30 minutes during the
competition session (cron or launchd).

    .venv/bin/python scripts/run_cycle.py --dry-run     # nothing touches disk
    .venv/bin/python scripts/run_cycle.py               # live (requires permit)

The dry run is the interesting one before kickoff: the competition_window
gate must refuse to trade the pristine account until 2026-08-28 15:00 UTC.
If a dry run ever shows a green preflight before kickoff, that is a bug in
the policy, not a gift.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.executor import Executor
from agent.ledger import (atomic_write, append_decision, ledger_positions,
                          mirror_from_broker)
from agent.proposer import select as llm_select
from gates import checks
from gates.registry import severity_of
from gates.safety_gate import write_permit
from policy.loader import load as load_manifest
from scripts.verify_account import creds, load_env
from strategy.data import AlpacaData, MarketState, parse_contract
from strategy.engine import EngineContext, run as run_engines
from strategy.proposal import OptionLeg, Proposal
from strategy.regime import classify, universe_breadth
from strategy.signals import score_symbol

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m")

META_PATH = ROOT / "state" / "positions_meta.json"


def build_state(data: AlpacaData, manifest, symbols: list[str]) -> MarketState:
    """Fetch everything one cycle needs. Failures are loud, never silent."""
    account = data.account()
    clock = data.clock()
    positions = [p for p in data.positions() if p.asset_class == "us_option"]

    state = MarketState(account=account, clock=clock,
                        equity=float(account.equity), positions=positions,
                        now_utc=datetime.now(timezone.utc))

    bars = data.daily_bars(
        symbols, days=int(manifest.get("agent", "market_summary_window_days",
                                       default=60)))
    state.bars = bars
    state.latest = data.latest_quotes(symbols)

    for sym in symbols:
        contracts = data.option_chain(sym)
        if contracts:
            state.chains[sym] = contracts
            state.chain_ages[sym] = data.chain_age_seconds(contracts)
    return state


def run_preflight(state: MarketState, manifest, ledger) -> dict:
    ctx = checks.EvalContext(
        manifest=manifest, now_utc=state.now_utc, account=state.account,
        is_paper_session=True, clock=state.clock, positions=state.positions,
        ledger_positions=ledger,
        option_quote_age_seconds=state.chain_ages.get("SPY"),
        underlying_bar_age_seconds=_underlying_bar_age(state),
        decision_log_writable=_decisions_writable(),
    )
    results = {}
    for gate in checks.GATES:
        if gate.phase != "preflight":
            continue
        results[gate.name] = gate.check(ctx)
    return results


def _underlying_bar_age(state: MarketState) -> float | None:
    bars = state.bars.get("SPY", [])
    if not bars:
        return None
    ts = getattr(bars[-1], "timestamp", None)
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (state.now_utc - ts).total_seconds() - 15 * 60)


def _decisions_writable() -> bool:
    p = ROOT / "state" / "decisions.jsonl"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a"):
            pass
        return True
    except OSError:
        return False


def print_gates(results: dict) -> None:
    for name, r in results.items():
        sev = severity_of(checks.GATES, name)
        mark = f"{GREEN}PASS{RESET}" if r.ok else (
            f"{RED}BLOCK{RESET}" if sev == "BLOCKING" else f"{YELLOW}WARN{RESET}")
        print(f"  [{mark}] {name:<22} {r.detail}")


def save_meta(symbol: str, entry_price: float, engine: str, take_profit: float,
              stop_loss: float, expiry: str) -> None:
    try:
        meta = json.loads(META_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        meta = {"positions": {}}
    meta["positions"][symbol] = {
        "engine": engine, "entry_price": entry_price,
        "take_profit_pct": take_profit, "stop_loss_pct": stop_loss,
        "expiry": expiry,
    }
    atomic_write(META_PATH, meta)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="no writes, no orders; gates + candidates only")
    ap.add_argument("--no-llm", action="store_true",
                    help="skip the LLM selection; use engine ranking")
    ap.add_argument("--env", default=".env")
    args = ap.parse_args()

    manifest = load_manifest()
    env = load_env(ROOT / args.env)
    key, secret = creds(env)
    if not key or not secret:
        print(f"{RED}No credentials.{RESET} Fill {ROOT}/{args.env}.")
        return 2

    data = AlpacaData(key, secret)
    symbols = sorted(set(manifest.declared_symbols()))
    state = build_state(data, manifest, symbols)

    now_utc = state.now_utc
    print(f"{DIM}manifest {manifest.identity}{RESET}")
    print(f"{DIM}account  {state.account.account_number}  equity "
          f"${state.equity:,.2f}  market "
          f"{'OPEN' if state.clock.is_open else 'CLOSED'}{RESET}")

    # ── 1. preflight gates ──────────────────────────────────────────────────
    ledger = ledger_positions()
    results = run_preflight(state, manifest, ledger)
    print("\npreflight gates")
    print_gates(results)
    blockers = [n for n, r in results.items()
                if not r.ok and severity_of(checks.GATES, n) == "BLOCKING"]

    if blockers and not args.dry_run:
        print(f"\n{RED}PERMIT REFUSED{RESET}: {', '.join(blockers)} — no new "
              f"exposure this cycle.")
        return 1

    if not args.dry_run:
        write_permit(results, checks.GATES, manifest_sha=manifest.sha)

    # ── 2. engine: candidates ───────────────────────────────────────────────
    signals = {}
    spy_closes = [b.close for b in state.bars.get("SPY", [])]
    qqq_closes = [b.close for b in state.bars.get("QQQ", [])]
    breadth = universe_breadth({s: [b.close for b in bars]
                                for s, bars in state.bars.items()})
    regime = classify(spy_closes, qqq_closes, [breadth])

    from zoneinfo import ZoneInfo
    now_et = now_utc.astimezone(ZoneInfo("America/New_York"))
    for sym in symbols:
        closes = [b.close for b in state.bars.get(sym, [])]
        highs = [b.high for b in state.bars.get(sym, [])]
        lows = [b.low for b in state.bars.get(sym, [])]
        if len(closes) >= 60:
            signals[sym] = score_symbol(closes, spy_closes, highs, lows, sym)

    ctx = EngineContext(state=state, manifest=manifest, regime=regime,
                        now_et=now_et, signals=signals)
    candidates = run_engines(ctx)

    print(f"\nregime  {regime.mode} ({regime.confidence:.2f}) — {regime.reason}")
    print(f"candidates: {len(candidates)}")
    for i, c in enumerate(candidates):
        p = c.proposal
        print(f"  [{i}] {p.engine:<18} {p.underlying:<5} {p.structure:<16} "
              f"dte={p.dte} limit={p.limit_price:>8.2f} "
              f"maxloss=${p.max_loss_dollars:,.0f} conv={p.conviction:.2f} "
              f"{c.label}")

    if not candidates:
        print(f"{DIM}no candidate met the manifests' criteria — a refusal, "
              f"not an error.{RESET}")

    # ── 3. proposer (LLM) ────────────────────────────────────────────────────
    chosen = []
    if candidates:
        chosen = (list(range(min(int(manifest.get("agent",
                                                  "max_proposals_per_cycle",
                                                  default=3)),
                                len(candidates))))
                  if args.no_llm else llm_select(
                      candidates, regime=regime.mode,
                      portfolio={"equity": state.equity,
                                 "open_positions": len(state.positions),
                                 "regime": regime.mode},
                      manifest=manifest))
    print(f"\nproposer chose candidates: {chosen}")

    if blockers or args.dry_run or not chosen:
        print(f"\n{DIM}demonstration state: "
              f"{'dry run' if args.dry_run else 'no selection'} — nothing was "
              f"sent.{RESET}")
        return 0

    # ── 4. executor: pretrade gates + submit ────────────────────────────────
    executor = Executor(data.trading, manifest)
    for idx in chosen:
        c = candidates[idx]
        p = c.proposal
        pre = {}
        for g in checks.GATES:
            if g.phase != "pretrade":
                continue
            pre[g.name] = g.check(checks.EvalContext(
                manifest=manifest, now_utc=now_utc, account=state.account,
                is_paper_session=True, clock=state.clock,
                positions=state.positions, ledger_positions=ledger,
                option_quote_age_seconds=state.chain_ages.get(p.underlying),
                underlying_bar_age_seconds=_underlying_bar_age(state),
                decision_log_writable=_decisions_writable(), proposal=p))
        refused = [n for n, r in pre.items() if not r.ok
                   and severity_of(checks.GATES, n) == "BLOCKING"]
        if refused:
            print(f"  {YELLOW}REFUSED{RESET} {p.underlying} {p.structure}: "
                  f"{', '.join(refused)}")
            continue
        try:
            order = executor.submit(p)
        except Exception as exc:                            # noqa: BLE001
            print(f"  {RED}SUBMIT FAILED{RESET}: {exc}")
            continue
        print(f"  {GREEN}OK{RESET} {order.id}")
        cfg = manifest.get("strategies", p.engine)
        for leg in p.legs:
            save_meta(
                leg.symbol, entry_price=leg.mid or 0.0, engine=p.engine,
                take_profit=float(cfg.get("take_profit_fraction", 0.5)) * 100.0,
                stop_loss=(float(cfg.get("stop_loss_fraction", 0.5)) * 100.0
                           if p.structure in ("debit_vertical", "straddle",
                                              "strangle") else 0.0),
                expiry=p.expiry.isoformat() if p.expiry else "")

    # ── 5. exits & hygiene ──────────────────────────────────────────────────
    executor.retry_open_orders_cleanup()
    manage_exits(data, manifest, state, executor)
    mirror_from_broker(data.positions())
    return 0


def manage_exits(data: AlpacaData, manifest, state, executor: Executor) -> None:
    """Defined-risk exits: TP/SL by mark, time-stops, final-day flatten.

    Every exit is a LIMIT order at the touch (sell at bid, buy at ask). No
    market orders exist in this policy. On the final date everything is
    flattened by order before 10:45 ET so nothing expires for a next-day
    paper settlement that would not exist by judging time.
    """
    from zoneinfo import ZoneInfo
    now_et = state.now_utc.astimezone(ZoneInfo("America/New_York"))
    final_date = str(manifest.get("session", "final_trading_date"))
    is_final = now_et.date().isoformat() == final_date
    flatten_at = str(manifest.get("session", "flatten_all_at"))
    hh, mm = map(int, flatten_at.split(":"))

    try:
        meta = json.loads(META_PATH.read_text()).get("positions", {})
    except (OSError, json.JSONDecodeError):
        meta = {}

    for pos in state.positions:
        sym = pos.symbol
        qty = int(float(pos.qty))
        if qty == 0:
            continue
        info = meta.get(sym, {})
        entry = float(info.get("entry_price", 0.0))
        mark = float(getattr(pos, "current_price", 0.0) or 0.0)

        reason = None
        if entry > 0 and mark > 0:
            pl_pct = (mark / entry - 1.0) * 100.0
            tp = float(info.get("take_profit_pct", 0.0))
            sl = float(info.get("stop_loss_pct", 0.0))
            if tp and pl_pct >= tp:
                reason = f"take-profit {pl_pct:+.1f}%"
            elif sl and pl_pct <= -sl:
                reason = f"stop-loss {pl_pct:+.1f}%"
        if is_final and (now_et.hour, now_et.minute) >= (hh, mm):
            reason = "final-day flatten"

        if not reason:
            continue

        contract = next((c for c in state.chains.get(_underlying(sym), [])
                         if c.symbol == sym), None)
        if contract is None:
            parsed = parse_contract(sym)
            if parsed:
                contract = next(
                    (c for c in state.chains.get(parsed[0], [])
                     if c.symbol == sym), None)
        if contract is None:
            print(f"  {YELLOW}EXIT SKIPPED{RESET} {sym}: no touch available")
            continue

        price = contract.bid if qty > 0 else contract.ask
        if price is None or price <= 0:
            print(f"  {YELLOW}EXIT SKIPPED{RESET} {sym}: no bid/ask")
            continue

        close = Proposal(
            engine="exit", underlying=_underlying(sym), direction="neutral",
            structure="single_close",
            legs=[OptionLeg(symbol=sym, side="sell" if qty > 0 else "buy",
                            quantity=abs(qty), strike=contract.strike,
                            contract_type=contract.contract_type,
                            expiration=contract.expiration,
                            ref_bid=contract.bid or 0.0,
                            ref_ask=contract.ask or 0.0)],
            limit_price=price, max_loss_dollars=0.0,
            thesis=reason, reason=reason)
        try:
            order = executor.submit(close, closing=True)
            append_decision({"kind": "position_closed", "symbol": sym,
                             "reason": reason, "order_id": str(order.id)})
            print(f"  {GREEN}EXIT{RESET} {sym} {reason} @ {price:.2f}")
        except Exception as exc:                            # noqa: BLE001
            print(f"  {RED}EXIT FAILED{RESET} {sym}: {exc}")


def _underlying(sym: str) -> str:
    parsed = parse_contract(sym)
    return parsed[0] if parsed else sym[:4]


if __name__ == "__main__":
    raise SystemExit(main())
