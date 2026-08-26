#!/usr/bin/env python3
"""One decision cycle: verify -> snapshot -> day gates -> exits -> engine ->
proposer -> pretrade gates -> execute. Run every 30 minutes in the entry
window.

    .venv/bin/python scripts/run_cycle.py --dry-run     # nothing touches disk
    .venv/bin/python scripts/run_cycle.py               # live (requires permit)

v2.1 changes from v2.0:
  * exits run BEFORE entries, and a multi-leg structure is marked, decided,
    and closed as ONE unit (strategy/exits.py) — legs are never closed
    independently, so a naked short leg cannot be manufactured by an exit;
  * the manifest's daily gates are actually enforced: daily new-exposure cap,
    daily kill switch, next-day drawdown scale (strategy/daystate.py);
  * catalyst/event entries fire once per day per name (no duplicate buys
    from later cycles);
  * chains are fetched for today..+3, so 0-DTE structures exist.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
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
from strategy.daystate import (check_kill, fire_key, fired, load_or_reset,
                               mark_fired, record_risk)
from strategy.engine import EngineContext, run as run_engines
from strategy.exits import (GroupView, build_close_proposal, decide_exit,
                            group_key, pnl_of)
from strategy.proposal import OptionLeg, Proposal
from strategy.regime import classify, universe_breadth
from strategy.signals import score_symbol
from strategy.sizing import PortfolioState

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m")

META_PATH = ROOT / "state" / "positions_meta.json"
DAY_PATH = ROOT / "state" / "day_state.json"


def build_state(data: AlpacaData, manifest, symbols: list[str]) -> MarketState:
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
    # v2.1: today..+3 so 0-DTE engines and post-event expiries both exist
    today = state.now_utc.date()
    expiries = [today + timedelta(days=d) for d in range(0, 4)]
    for sym in symbols:
        contracts = data.option_chains_multi(sym, expiries)
        if contracts:
            state.chains[sym] = contracts
            state.chain_ages[sym] = data.chain_age_seconds(contracts)
    return state


def _code_identity() -> tuple:
    """The commit this cycle runs, and whether the tree was edited.

    Without this the release_integrity gate reported "git head unknown" every
    cycle, and every decision record carried no code identity at all — which
    defeats the point of recording one. Failure is not fatal: the gate is
    ATTENTION, and (None, None) is the honest answer when git cannot be read.
    """
    import subprocess
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=10,
                              check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"],
                                    cwd=str(ROOT), capture_output=True,
                                    text=True, timeout=10,
                                    check=True).stdout.strip())
        return head, dirty
    except (OSError, subprocess.SubprocessError):
        return None, None


def publish_snapshot(*, manifest, state, results, blockers, decisions,
                     regime=None, day=None) -> None:
    """Write the credential-free page the judges read. Never fatal.

    A dashboard that cannot render must not be able to stop the agent from
    trading, so every failure here is reported and swallowed.
    """
    from agent import snapshot as snap_mod
    head, dirty = _code_identity()
    try:
        payload = snap_mod.build(
            manifest=manifest, account=state.account, clock=state.clock,
            gate_results=results, gates=checks.GATES,
            permit_status="BLOCKED" if blockers else "READY",
            blockers=blockers, positions=state.positions,
            decisions=decisions, git_head=head, git_dirty=dirty,
            regime=regime, day_state=(day.as_dict() if day else None),
            now_utc=state.now_utc)
        snap_mod.write(payload)
    except Exception as exc:                                  # noqa: BLE001
        print(f"{YELLOW}snapshot not written{RESET}: "
              f"{type(exc).__name__}: {exc}")


def run_preflight(state: MarketState, manifest, ledger) -> dict:
    head, dirty = _code_identity()
    ctx = checks.EvalContext(
        manifest=manifest, now_utc=state.now_utc, account=state.account,
        is_paper_session=True, clock=state.clock, positions=state.positions,
        ledger_positions=ledger,
        option_quote_age_seconds=state.chain_ages.get("SPY"),
        underlying_bar_age_seconds=_underlying_bar_age(state),
        decision_log_writable=_decisions_writable(),
        git_head=head, git_dirty=dirty,
    )
    results = {}
    for gate in checks.GATES:
        if gate.phase != "preflight":
            continue
        results[gate.name] = gate.check(ctx)
    return results


def _underlying_bar_age(state: MarketState) -> float | None:
    """Freshness of the underlying the signal consumes.

    The signal reads real-time IEX quotes (plus daily bars for trend); the
    freshness gate must measure the QUOTE, not the daily bar - a 1-Day bar's
    timestamp is the session open, which would read hours old all day and
    keep the gate permanently red. IEX quote timestamps tick all session.
    """
    q = state.latest.get("SPY")
    if q is None:
        return None
    ts = getattr(q, "timestamp", None)
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (state.now_utc - ts).total_seconds())


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


# ── meta (structure records) ─────────────────────────────────────────────────

def load_meta() -> dict:
    try:
        return json.loads(META_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"groups": {}}


def save_meta(meta: dict) -> None:
    atomic_write(META_PATH, meta)


def record_group(proposal: Proposal, entered_at: str) -> None:
    meta = load_meta()
    gid = group_key(proposal.engine, proposal.underlying,
                    proposal.expiry.isoformat() if proposal.expiry else "",
                    entered_at)
    entry_net = sum(leg.mid * (1.0 if leg.side == "buy" else -1.0)
                    for leg in proposal.legs)
    kind = "credit" if proposal.structure in ("credit_vertical",
                                              "iron_condor") else "debit"
    ref_amount = -entry_net if kind == "credit" else entry_net
    meta["groups"][gid] = {
        "engine": proposal.engine,
        "underlying": proposal.underlying,
        "expiry": proposal.expiry.isoformat() if proposal.expiry else "",
        "kind": kind,
        "entry_net": round(entry_net, 4),
        "ref_amount": round(abs(ref_amount), 4),
        "max_loss_dollars": proposal.max_loss_dollars,
        "take_profit_fraction": 0.0,
        "stop_loss_fraction": 0.0,
        "event_exit_date": proposal.event_exit_date,
        "event_exit_time": proposal.event_exit_time,
        "legs": {leg.symbol: {"side": leg.side, "qty": leg.quantity,
                              "entry_mid": leg.mid}
                 for leg in proposal.legs},
        "closed": False,
    }
    save_meta(meta)


def patch_group_tp_sl(gid: str, take_profit: float, stop_loss: float) -> None:
    meta = load_meta()
    group = meta.get("groups", {}).get(gid)
    if group:
        group["take_profit_fraction"] = take_profit
        group["stop_loss_fraction"] = stop_loss
        save_meta(meta)


# ── exits ────────────────────────────────────────────────────────────────────

def manage_exits(state: MarketState, manifest, executor: Executor) -> int:
    """Structure-level exits. Legs of one structure close as one order.

    Marked on the STRUCTURE net (all legs together), so a debit spread's
    losing long leg can never be closed alone while its short leg survives
    naked. Every close is a DAY limit at the touch - no market orders exist
    in this policy.
    """
    from zoneinfo import ZoneInfo
    now_et = state.now_utc.astimezone(ZoneInfo("America/New_York"))
    final_date = str(manifest.get("session", "final_trading_date"))
    flatten_at = str(manifest.get("session", "flatten_all_at"))

    meta = load_meta()
    groups = meta.get("groups", {})
    broker = {p.symbol: p for p in state.positions}
    closed_any = 0

    for gid, g in groups.items():
        if g.get("closed"):
            continue
        legs = g.get("legs", {})
        open_legs = {sym: info for sym, info in legs.items()
                     if sym in broker and int(float(broker[sym].qty)) != 0}
        if not open_legs:
            continue

        prices = {}
        touch = {}
        for sym, info in open_legs.items():
            pos = broker[sym]
            price = float(getattr(pos, "current_price", 0.0) or 0.0)
            contract = _contract(state, sym)
            if price <= 0 and contract is not None:
                price = contract.bid if info["side"] == "buy" else contract.ask
            if price is None or price <= 0:
                continue
            signed = price * (1.0 if info["side"] == "buy" else -1.0)
            prices[sym] = signed
            t = None
            if contract is not None:
                t = contract.bid if info["side"] == "buy" else contract.ask
            touch[sym] = t if t else price

        if not prices:
            continue

        net = sum(prices.values())
        entry_net = float(g.get("entry_net", 0.0))
        qty0 = int(list(open_legs.values())[0]["qty"])
        pnl_contract = pnl_of(entry_net, net)
        pnl = pnl_contract * abs(qty0)
        gv = GroupView(
            group_id=gid, engine=g.get("engine", ""),
            underlying=g.get("underlying", ""), expiry=g.get("expiry", ""),
            kind=g.get("kind", "debit"),
            entry_net=entry_net,
            ref_amount=float(g.get("ref_amount", 0.0) or 0.0),
            take_profit_fraction=float(g.get("take_profit_fraction", 0.0)),
            stop_loss_fraction=float(g.get("stop_loss_fraction", 0.0)),
            event_exit_date=g.get("event_exit_date", ""),
            event_exit_time=g.get("event_exit_time", ""),
            legs=[(sym, info["side"], int(info["qty"]))
                  for sym, info in open_legs.items()],
        )
        reason = decide_exit(gv, pnl_contract, now_et=now_et,
                             final_date=final_date, flatten_at=flatten_at)
        if not reason:
            continue

        if g.get("take_profit_fraction", 0.0) <= 0:
            # unset tp/sl (should not happen) - only event/flatten closes
            if "event" not in reason and "flatten" not in reason and \
                    "time-stop" not in reason:
                continue

        close = build_close_proposal(gv, touch)
        if not close.legs:
            continue
        try:
            order = executor.submit(close, closing=True)
            append_decision({"kind": "structure_closed", "group": gid,
                             "reason": reason, "pnl": round(pnl, 2),
                             "order_id": str(order.id)})
            g["closed"] = True
            closed_any += 1
            print(f"  {GREEN}EXIT{RESET} {gid} {reason} (pnl ${pnl:,.0f}) "
                  f"net {close.limit_price:.2f}")
        except Exception as exc:                            # noqa: BLE001
            print(f"  {RED}EXIT FAILED{RESET} {gid}: {exc}")

    # orphan positions (no meta) close at the touch as singles - defensive
    # only; the agent never creates structures outside the meta.
    for pos in state.positions:
        sym = pos.symbol
        if any(sym in (g.get("legs") or {}) for g in groups.values()
               if not g.get("closed")):
            continue
        contract = _contract(state, sym)
        if contract is None:
            continue
        qty = int(float(pos.qty))
        if qty == 0:
            continue
        price = contract.bid if qty > 0 else contract.ask
        if price is None or price <= 0:
            continue
        close = Proposal(engine="exit", underlying=_underlying(sym),
                         direction="neutral", structure="single_close",
                         legs=[OptionLeg(symbol=sym,
                                         side="sell" if qty > 0 else "buy",
                                         quantity=abs(qty),
                                         strike=contract.strike,
                                         contract_type=contract.contract_type,
                                         expiration=contract.expiration,
                                         ref_bid=contract.bid or 0.0,
                                         ref_ask=contract.ask or 0.0)],
                         limit_price=price, max_loss_dollars=0.0,
                         thesis="orphan close", reason="ORPHAN")
        try:
            order = executor.submit(close, closing=True)
            append_decision({"kind": "orphan_closed", "symbol": sym,
                             "order_id": str(order.id)})
            closed_any += 1
            print(f"  {GREEN}EXIT{RESET} orphan {sym} @ {price:.2f}")
        except Exception as exc:                            # noqa: BLE001
            print(f"  {RED}EXIT FAILED{RESET} orphan {sym}: {exc}")

    if closed_any:
        save_meta(meta)
    return closed_any


def _contract(state: MarketState, sym: str):
    parsed = parse_contract(sym)
    if not parsed:
        return None
    for c in state.chains.get(parsed[0], []):
        if c.symbol == sym:
            return c
    return None


def _underlying(sym: str) -> str:
    parsed = parse_contract(sym)
    return parsed[0] if parsed else sym[:4]


# ── the cycle ────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
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

    # ── 0. day-state gates (daily exposure cap, kill switch, scale) ─────────
    today = now_utc.date().isoformat()
    try:
        raw_day = json.loads(DAY_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        raw_day = None
    day = load_or_reset(raw_day, today=today, equity_now=state.equity,
                        scale_fraction=float(manifest.get(
                            "risk_caps", "drawdown_scale_fraction")))
    killed = check_kill(day, state.equity,
                        float(manifest.get("risk_caps",
                                           "daily_loss_kill_fraction")))
    exposure_cap = (float(manifest.get("risk_caps",
                                       "daily_new_exposure_cap_fraction"))
                    * float(manifest.get("environment",
                                         "required_starting_equity")))

    # ── 1. preflight gates ──────────────────────────────────────────────────
    mirror_from_broker(data.positions())
    ledger = ledger_positions()
    results = run_preflight(state, manifest, ledger)
    print("\npreflight gates")
    print_gates(results)
    blockers = [n for n, r in results.items()
                if not r.ok and severity_of(checks.GATES, n) == "BLOCKING"]

    if not args.dry_run:
        write_permit(results, checks.GATES, manifest_sha=manifest.sha)

    # ── 2. exits FIRST: the book is settled before we size anything new ─────
    # Exits are risk-REDUCING, so they run even when preflight gates are red;
    # the permit only gates NEW exposure. This is what keeps the final-day
    # flatten alive even if a data gate is red at 10:45 ET.
    executor = Executor(data.trading, manifest)
    if not args.dry_run:
        executor.retry_open_orders_cleanup()
        n_exits = manage_exits(state, manifest, executor)
        if n_exits:
            state.positions = [p for p in data.positions()
                               if p.asset_class == "us_option"]
            mirror_from_broker(data.positions())
            ledger = ledger_positions()

    if blockers and not args.dry_run:
        print(f"\n{RED}PERMIT REFUSED{RESET}: {', '.join(blockers)} — no new "
              f"exposure this cycle.")
        return 1

    # ── 3. engine candidates ────────────────────────────────────────────────
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

    meta = load_meta()
    open_groups = [g for g in meta.get("groups", {}).values()
                   if not g.get("closed")]
    portfolio = PortfolioState(
        max_loss_by_underlying={},
        max_loss_total=sum(float(g.get("max_loss_dollars", 0.0))
                           for g in open_groups),
        count_by_engine={},
        current_equity=state.equity,
        starting_equity=float(
            manifest.get("environment", "required_starting_equity")),
        scale=day.scale,
    )
    for g in open_groups:
        eng = g.get("engine", "?")
        portfolio.count_by_engine[eng] = portfolio.count_by_engine.get(eng, 0) + 1

    ctx = EngineContext(state=state, manifest=manifest, regime=regime,
                        now_et=now_et, signals=signals, portfolio=portfolio)
    candidates = run_engines(ctx)

    print(f"\nregime  {regime.mode} ({regime.confidence:.2f}) — {regime.reason}")
    print(f"day     new_risk=${day.new_risk_dollars:,.0f}/{exposure_cap:,.0f} "
          f"killed={day.killed} scale={day.scale}")
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

    chosen = []
    if candidates and not (blockers or killed or args.dry_run):
        chosen = (list(range(min(int(manifest.get("agent",
                                                  "max_proposals_per_cycle",
                                                  default=3)),
                                len(candidates))))
                  if args.no_llm else llm_select(
                      candidates, regime=regime.mode,
                      portfolio={"equity": state.equity,
                                 "open_positions": len(state.positions),
                                 "regime": regime.mode, "killed": day.killed},
                      manifest=manifest))
    print(f"\nproposer chose candidates: {chosen}")

    # Every candidate the engines produced, with what happened to it. A refused
    # proposal is the evidence that the gates do anything at all, so refusals
    # are recorded exactly as carefully as fills — see agent/snapshot.py.
    why_none = ("daily kill switch" if killed else
                (f"permit refused: {', '.join(blockers)}" if blockers else
                 "not selected by the proposer"))
    decisions = [{
        "at": now_utc.isoformat(),
        "engine": c.proposal.engine,
        "underlying": c.proposal.underlying,
        "structure": c.proposal.structure,
        "max_loss_dollars": c.proposal.max_loss_dollars,
        "conviction": c.proposal.conviction,
        "accepted": (i in chosen) and not (blockers or killed or args.dry_run),
        "reason": (c.label if (i in chosen) and
                   not (blockers or killed or args.dry_run) else why_none),
    } for i, c in enumerate(candidates)]

    publish_snapshot(manifest=manifest, state=state, results=results,
                     blockers=blockers, decisions=decisions, regime=regime,
                     day=day)

    if args.dry_run:
        print(f"\n{DIM}dry run — nothing was sent.{RESET}")
        return 0
    if blockers or killed or not chosen:
        why = "kill switch" if killed else (
            "permit refused" if blockers else "no selection")
        print(f"\n{DIM}{why} — nothing was sent.{RESET}")
        atomic_write(DAY_PATH, day.as_dict())
        return 0

    # ── 4. fire-once guards, exposure cap, pretrade gates, submit ───────────
    entered_at = now_utc.strftime("%H%M%S")
    submitted = 0
    for idx in chosen:
        c = candidates[idx]
        p = c.proposal

        if p.engine in ("catalyst", "event_macro", "vol_income"):
            key = fire_key(p.engine, p.underlying, today)
            if fired(day, key):
                print(f"  {YELLOW}SKIP (already fired today){RESET} "
                      f"{p.underlying} {p.structure}")
                continue
        if p.engine in ("catalyst", "event_macro") and p.expiry and \
                p.expiry.isoformat() < today:
            # 0-DTE is legitimate during session hours; only the PAST is refused
            print(f"  {YELLOW}SKIP{RESET} {p.underlying}: expired-by-design "
                  f"entry ({p.expiry})")
            continue

        if not record_risk(day, p.max_loss_dollars, exposure_cap):
            print(f"  {YELLOW}SKIP{RESET} {p.underlying} {p.structure}: daily "
                  f"exposure cap reached")
            continue

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
        gid = group_key(p.engine, p.underlying,
                        p.expiry.isoformat() if p.expiry else "", entered_at)
        record_group(p, entered_at)
        if p.structure in ("credit_vertical", "iron_condor"):
            tp, sl = float(cfg.get("take_profit_fraction", 0.5)), \
                float(cfg.get("stop_loss_multiple", 2.0))
        else:
            tp, sl = float(cfg.get("take_profit_fraction", 0.5)), \
                float(cfg.get("stop_loss_fraction", 0.5))
        patch_group_tp_sl(gid, tp, sl)

        if p.engine in ("catalyst", "event_macro", "vol_income"):
            mark_fired(day, fire_key(p.engine, p.underlying, today))
        submitted += 1

    atomic_write(DAY_PATH, day.as_dict())
    mirror_from_broker(data.positions())
    print(f"\nsubmitted {submitted} proposal(s) this cycle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
