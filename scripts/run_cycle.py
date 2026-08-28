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
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.executor import Executor
from agent.cycle_lock import CycleAlreadyRunning, cycle_lock
from agent.entry_submission import (project_day_risk, proposal_fingerprint,
                                    submit_entries)
from agent.ledger import (StructureLedger, atomic_write, append_decision,
                          ledger_positions, mirror_from_broker)
from agent.proposer import SelectionResult, select as llm_select
from agent.submission_wal import (JournalView, SubmissionJournal,
                                  reconcile_unresolved,
                                  refresh_committed_orders)
from gates import checks
from gates.evaluation import GateEvaluator, code_identity
from gates.registry import severity_of
from gates.safety_gate import write_permit
from policy.loader import load as load_manifest
from scripts.verify_account import creds, load_env
from strategy.data import AlpacaData, MarketState, parse_contract
from strategy.daystate import check_kill, load_or_reset, record_risk, release_risk
from strategy.engine import EngineContext, run as run_engines
from strategy.exits import (GroupView, build_close_proposal, decide_exit,
                            pnl_of)
from strategy.proposal import OptionLeg, Proposal
from strategy.regime import classify, universe_breadth
from strategy.signals import score_symbol
from strategy.sizing import PortfolioState, record_open_risk, release_open_risk

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m")

DAY_PATH = ROOT / "state" / "day_state.json"
SUBMISSION_WAL_PATH = ROOT / "state" / "submission_wal.jsonl"
CYCLE_LOCK_PATH = ROOT / "state" / "cycle.lock"
PUBLIC_ACCOUNT_SCOPE = "competition"
STRUCTURES = StructureLedger()


def unresolved_dispatch_count(view: JournalView) -> int | None:
    """Compatibility projection of durable dispatch state.

    Gate context construction lives in :mod:`gates.evaluation`; this small
    pure helper remains for callers that need to display the same durable
    fact without constructing an evaluation subject.
    """
    if not view.integrity_ok:
        return None
    return len(view.unresolved_dispatches)


def new_decision_row(candidate, *, at_utc: datetime,
                     selected: bool, account_scope: str,
                     proposer: SelectionResult | None = None) -> dict:
    """Public facts for one candidate; the retired `accepted` cannot appear."""
    proposal = candidate.proposal
    row = {
        "at": at_utc.isoformat(),
        "engine": proposal.engine,
        "underlying": proposal.underlying,
        "structure": proposal.structure,
        "max_loss_dollars": proposal.max_loss_dollars,
        "conviction": proposal.conviction,
        "selected": bool(selected),
        "authorized": False,
        "submitted": False,
        "account_scope": account_scope,
        "refused_by": [],
        "reason": candidate.label,
    }
    if proposer is not None:
        row["proposer"] = {
            "decision_mode": proposer.decision_mode,
            "provider": proposer.provider,
            "model": proposer.model,
            "fallback_reason": proposer.fallback_reason,
        }
    return row


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


def publish_snapshot(*, manifest, state, results, blockers, decisions,
                     regime=None, day=None, decision_updates=None) -> None:
    """Write the credential-free page the judges read. Never fatal.

    A dashboard that cannot render must not be able to stop the agent from
    trading, so every failure here is reported and swallowed.
    """
    from agent import snapshot as snap_mod
    head, dirty = code_identity()
    try:
        payload = snap_mod.build(
            manifest=manifest, account=state.account, clock=state.clock,
            gate_results=results, gates=checks.GATES,
            permit_status="BLOCKED" if blockers else "READY",
            blockers=blockers, positions=state.positions,
            decisions=decisions, git_head=head, git_dirty=dirty,
            regime=regime, day_state=(day.as_dict() if day else None),
            decision_updates=decision_updates,
            now_utc=state.now_utc)
        snap_mod.write(payload)
    except Exception as exc:                                  # noqa: BLE001
        print(f"{YELLOW}snapshot not written{RESET}: "
              f"{type(exc).__name__}: {exc}")


def run_preflight(state: MarketState, manifest, ledger,
                  journal_view: JournalView | None = None) -> dict:
    journal_view = journal_view or SubmissionJournal(
        SUBMISSION_WAL_PATH).replay()
    evaluator = GateEvaluator(root=ROOT)
    subject = evaluator.cycle_subject(
        state=state, manifest=manifest, ledger_positions=ledger,
        journal_view=journal_view,
    )
    return evaluator.evaluate(subject)


def print_gates(results: dict) -> None:
    for name, r in results.items():
        sev = severity_of(checks.GATES, name)
        mark = f"{GREEN}PASS{RESET}" if r.ok else (
            f"{RED}BLOCK{RESET}" if sev == "BLOCKING" else f"{YELLOW}WARN{RESET}")
        print(f"  [{mark}] {name:<22} {r.detail}")


def reserve_entry_risk(portfolio: PortfolioState, day, dollars: float,
                       *, at_risk_cap: float,
                       exposure_cap: float) -> str | None:
    """Reserve both entry budgets atomically from the caller's perspective.

    The portfolio budget is checked first because it is reconstructed each
    cycle, while the day budget is durable.  If the durable budget refuses,
    give the first reservation back before returning.  A caller must never
    observe a half-reserved Proposal.

    Returns the budget which refused, or None when both reservations hold.
    """
    if not record_open_risk(portfolio, dollars, at_risk_cap):
        return "portfolio"
    if not record_risk(day, dollars, exposure_cap):
        release_open_risk(portfolio, dollars)
        return "daily"
    return None


def release_entry_risk(portfolio: PortfolioState, day, dollars: float) -> None:
    """Undo reserve_entry_risk. The counterpart exists so a caller never has
    to know there are two budgets underneath: reserve_entry_risk already hides
    that, and a caller reaching past it to release both by hand is the same
    interface leaking in the other direction.
    """
    release_open_risk(portfolio, dollars)
    release_risk(day, dollars)


# ── exits ────────────────────────────────────────────────────────────────────

def manage_exits(state: MarketState, manifest, executor: Executor,
                 *, structures: StructureLedger | None = None) -> int:
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

    structures = structures or STRUCTURES
    meta = structures.load()
    groups = meta.get("groups", {})
    broker = {p.symbol: p for p in state.positions}
    exit_orders_submitted = 0
    meta_changed = False
    # Freeze ownership against this broker snapshot.  A close submission
    # below moves its group to close_pending, while the positions in `state`
    # remain the pre-submission snapshot.  Ownership must remain stable for
    # this invocation so those legs cannot also enter the orphan pass.  A
    # later cycle rebuilds the set from broker-confirmed state.
    managed_symbols = {
        sym
        for group in groups.values()
        if not group.get("closed")
        for sym in (group.get("legs") or {})
    }

    for gid, g in groups.items():
        if g.get("closed"):
            continue
        legs = g.get("legs", {})
        open_legs = {sym: info for sym, info in legs.items()
                     if sym in broker and int(float(broker[sym].qty)) != 0}
        if not open_legs:
            if g.get("close_pending"):
                g["close_pending"] = False
                g["closed"] = True
                append_decision({"kind": "structure_close_confirmed",
                                 "group": gid,
                                 "order_id": g.get("close_order_id", "")})
                meta_changed = True
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
            append_decision({"kind": "structure_close_submitted",
                             "group": gid, "reason": reason,
                             "pnl": round(pnl, 2),
                             "order_id": str(order.id)})
            g["close_pending"] = True
            g["close_order_id"] = str(order.id)
            g["close_reason"] = reason
            meta_changed = True
            exit_orders_submitted += 1
            print(f"  {GREEN}EXIT{RESET} {gid} {reason} (pnl ${pnl:,.0f}) "
                  f"net {close.limit_price:.2f}")
        except Exception as exc:                            # noqa: BLE001
            print(f"  {RED}EXIT FAILED{RESET} {gid}: {exc}")

    # orphan positions (no meta) close at the touch as singles - defensive
    # only; the agent never creates structures outside the meta.
    for pos in state.positions:
        sym = pos.symbol
        if sym in managed_symbols:
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
            exit_orders_submitted += 1
            print(f"  {GREEN}EXIT{RESET} orphan {sym} @ {price:.2f}")
        except Exception as exc:                            # noqa: BLE001
            print(f"  {RED}EXIT FAILED{RESET} orphan {sym}: {exc}")

    if meta_changed:
        structures.save(meta)
    return exit_orders_submitted


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

def _run_cycle() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--exits-only", action="store_true",
                    help="manage the existing book but size nothing new "
                         "(used past the final trading date)")
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
    timezone_name = str(manifest.get("session", "timezone"))
    trading_date = now_utc.astimezone(ZoneInfo(timezone_name)).date()
    cycle_id = uuid.uuid4().hex
    journal = SubmissionJournal(SUBMISSION_WAL_PATH)
    journal_view = (journal.replay() if args.dry_run else
                    reconcile_unresolved(journal, data.trading))
    decision_updates = ({} if args.dry_run else
                        refresh_committed_orders(journal, data.trading))
    print(f"{DIM}manifest {manifest.identity}{RESET}")
    print(f"{DIM}account  {state.account.account_number}  equity "
          f"${state.equity:,.2f}  market "
          f"{'OPEN' if state.clock.is_open else 'CLOSED'}{RESET}")

    # ── 0. day-state gates (daily exposure cap, kill switch, scale) ─────────
    today = trading_date.isoformat()
    try:
        raw_day = json.loads(DAY_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        raw_day = None
    day = load_or_reset(raw_day, today=today, equity_now=state.equity,
                        scale_fraction=float(manifest.get(
                            "risk_caps", "drawdown_scale_fraction")))
    project_day_risk(day, journal_view, trading_date)
    killed = check_kill(day, state.equity,
                        float(manifest.get("risk_caps",
                                           "daily_loss_kill_fraction")))
    exposure_cap = (float(manifest.get("risk_caps",
                                       "daily_new_exposure_cap_fraction"))
                    * float(manifest.get("environment",
                                         "required_starting_equity")))
    at_risk_cap = (float(manifest.get("risk_caps", "at_risk_cap_fraction"))
                   * float(manifest.get("environment",
                                        "required_starting_equity")))

    # ── 1. preflight gates ──────────────────────────────────────────────────
    mirror_from_broker(data.positions())
    ledger = ledger_positions()
    results = run_preflight(state, manifest, ledger, journal_view)
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

    # Past the final trading date the account must stop growing, but it must
    # not stop being managed: exits are the only way a residual position from
    # an unfilled 09-04 flatten limit ever gets closed, and manage_exits is
    # the sole exit path in this repo. Suppressing NEW exposure is the whole
    # requirement; suppressing the cycle would strand the book.
    if args.exits_only:
        print(f"\n{YELLOW}EXITS ONLY{RESET}: past the final trading date — "
              f"book still managed, nothing new will be sized.")
        return 0

    if blockers and not args.dry_run:
        print(f"\n{RED}PERMIT REFUSED{RESET}: {', '.join(blockers)} — no new "
              f"exposure this cycle.")
        publish_snapshot(manifest=manifest, state=state, results=results,
                         blockers=blockers, decisions=[], day=day,
                         decision_updates=decision_updates)
        return 1

    # ── 3. engine candidates ────────────────────────────────────────────────
    signals = {}
    spy_closes = [b.close for b in state.bars.get("SPY", [])]
    qqq_closes = [b.close for b in state.bars.get("QQQ", [])]
    breadth = universe_breadth({s: [b.close for b in bars]
                                for s, bars in state.bars.items()})
    regime = classify(spy_closes, qqq_closes, [breadth])

    now_et = now_utc.astimezone(ZoneInfo(timezone_name))
    for sym in symbols:
        closes = [b.close for b in state.bars.get(sym, [])]
        highs = [b.high for b in state.bars.get(sym, [])]
        lows = [b.low for b in state.bars.get(sym, [])]
        if len(closes) >= 60:
            signals[sym] = score_symbol(closes, spy_closes, highs, lows, sym)

    meta = STRUCTURES.load()
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
    proposer_result = None
    if candidates and not (blockers or killed or args.dry_run):
        if args.no_llm:
            chosen = list(range(min(int(manifest.get(
                "agent", "max_proposals_per_cycle", default=3)),
                len(candidates))))
            proposer_result = SelectionResult(
                tuple(chosen), "deterministic_fallback", "disabled", "none",
                "disabled_by_operator")
        else:
            proposer_result = llm_select(
                candidates, regime=regime.mode,
                portfolio={"equity": state.equity,
                           "open_positions": len(state.positions),
                           "regime": regime.mode, "killed": day.killed},
                manifest=manifest)
            chosen = list(proposer_result.indices)
    print(f"\nproposer chose candidates: {chosen}")

    # These rows are updated by the admission/submission loop below, then
    # published once the final outcome is known. Publishing here used to label
    # a proposer selection as `accepted` before the pretrade gates had run.
    decisions = [new_decision_row(
        c, at_utc=now_utc, selected=(i in chosen),
        account_scope=PUBLIC_ACCOUNT_SCOPE, proposer=proposer_result)
                 for i, c in enumerate(candidates)]
    if blockers:
        for row in decisions:
            row["refused_by"] = [f"gate:{name}" for name in blockers]
            row["reason"] = f"cycle not ready: {', '.join(blockers)}"
    elif killed:
        for row in decisions:
            row["refused_by"] = ["control:daily_kill"]
            row["reason"] = "daily kill switch"
    elif args.dry_run:
        for row in decisions:
            row["reason"] = "dry run; proposer and submission not invoked"
    else:
        for i, row in enumerate(decisions):
            if i not in chosen:
                row["reason"] = "not selected by the proposer"

    if args.dry_run:
        publish_snapshot(manifest=manifest, state=state, results=results,
                         blockers=blockers, decisions=decisions, regime=regime,
                         day=day, decision_updates=decision_updates)
        print(f"\n{DIM}dry run — nothing was sent.{RESET}")
        return 0
    if blockers or killed or not chosen:
        why = "kill switch" if killed else (
            "permit refused" if blockers else "no selection")
        print(f"\n{DIM}{why} — nothing was sent.{RESET}")
        atomic_write(DAY_PATH, day.as_dict())
        publish_snapshot(manifest=manifest, state=state, results=results,
                         blockers=blockers, decisions=decisions, regime=regime,
                         day=day, decision_updates=decision_updates)
        return 0

    # ── 4. admission, WAL reservation, pretrade gates, submit ─────────────
    # Entry evaluation begins after exits refresh the broker and ledger views.
    # The typed subject supplies one shared fact set to every selected proposal;
    # `submit_entries` returns outcomes rather than emitting its own output.
    entry_evaluator = GateEvaluator(root=ROOT)
    entry_cycle_subject = entry_evaluator.cycle_subject(
        state=state, manifest=manifest, ledger_positions=ledger,
        journal_view=journal_view,
    )
    entry_result = submit_entries(
        candidates=candidates, chosen=chosen, decisions=decisions,
        state=state, manifest=manifest, executor=executor, portfolio=portfolio,
        day=day, journal=journal, journal_view=journal_view,
        trading_date=trading_date, cycle_id=cycle_id,
        at_risk_cap=at_risk_cap, exposure_cap=exposure_cap,
        entry_evaluator=entry_evaluator,
        entry_cycle_subject=entry_cycle_subject,
    )
    for event in entry_result.events:
        p = event.proposal
        if event.kind == "submitted":
            print(f"  {GREEN}OK{RESET} {event.detail}")
        elif event.kind == "submission_uncertain":
            print(f"  {RED}SUBMIT UNCERTAIN{RESET}: {event.detail}")
        elif event.kind == "gate_refused":
            refused = ", ".join(decisions[event.index]["refused_by"])
            print(f"  {YELLOW}REFUSED{RESET} {p.underlying} {p.structure}: "
                  f"{refused}")
        else:
            print(f"  {YELLOW}SKIP{RESET} {p.underlying} {p.structure}: "
                  f"{event.detail}")

    entered_at = now_utc.strftime("%H%M%S")
    for entry in entry_result.submissions:
        p = entry.proposal
        cfg = manifest.get("strategies", p.engine)
        gid = STRUCTURES.record_entry(p, entered_at)
        if p.structure in ("credit_vertical", "iron_condor"):
            tp, sl = float(cfg.get("take_profit_fraction", 0.5)), \
                float(cfg.get("stop_loss_multiple", 2.0))
        else:
            tp, sl = float(cfg.get("take_profit_fraction", 0.5)), \
                float(cfg.get("stop_loss_fraction", 0.5))
        STRUCTURES.set_exit_thresholds(gid, take_profit=tp, stop_loss=sl)

    atomic_write(DAY_PATH, day.as_dict())
    mirror_from_broker(data.positions())
    publish_snapshot(manifest=manifest, state=state, results=results,
                     blockers=blockers, decisions=decisions, regime=regime,
                     day=day, decision_updates=decision_updates)
    print(f"\nsubmitted {len(entry_result.submissions)} proposal(s) this cycle")
    return 1 if entry_result.uncertain else 0


def main() -> int:
    """The one ownership boundary shared by every cycle entry point."""
    try:
        with cycle_lock(CYCLE_LOCK_PATH, blocking=False):
            return _run_cycle()
    except CycleAlreadyRunning as exc:
        print(f"{YELLOW}CYCLE REFUSED{RESET}: {exc}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
