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
import hashlib
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
from agent.ledger import (atomic_write, append_decision, ledger_positions,
                          mirror_from_broker)
from agent.proposer import select as llm_select
from agent.submission_wal import (JournalView, Reservation, SubmissionJournal,
                                  dispatch_entry, make_client_order_id,
                                  reconcile_unresolved,
                                  refresh_committed_orders)
from gates import checks
from gates.registry import severity_of
from gates.safety_gate import write_permit
from policy.loader import load as load_manifest
from scripts.verify_account import creds, load_env
from strategy.data import AlpacaData, MarketState, parse_contract
from strategy.daystate import (check_kill, fire_key, fired, load_or_reset,
                               mark_fired, record_risk, release_risk)
from strategy.engine import EngineContext, run as run_engines
from strategy.exits import (GroupView, build_close_proposal, decide_exit,
                            group_key, pnl_of)
from strategy.proposal import OptionLeg, Proposal
from strategy.regime import classify, universe_breadth
from strategy.signals import score_symbol
from strategy.sizing import PortfolioState, record_open_risk, release_open_risk

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m")

META_PATH = ROOT / "state" / "positions_meta.json"
DAY_PATH = ROOT / "state" / "day_state.json"
SUBMISSION_WAL_PATH = ROOT / "state" / "submission_wal.jsonl"
CYCLE_LOCK_PATH = ROOT / "state" / "cycle.lock"
PUBLIC_ACCOUNT_SCOPE = "competition"


def proposal_fingerprint(proposal: Proposal) -> str:
    """Canonical identity of the entry content used by this release.

    This is intentionally narrower than the future ``EntryIntent`` module:
    the competition safety patch needs a stable audit identity now, while the
    one-object wire translation is deferred to the post-competition rewrite.
    Every field that currently changes the order or its declared risk is in
    the canonical payload.
    """
    payload = {
        "engine": proposal.engine,
        "underlying": proposal.underlying,
        "direction": proposal.direction,
        "structure": proposal.structure,
        "expiry": proposal.expiry.isoformat() if proposal.expiry else None,
        "order_class": proposal.order_class,
        "type": proposal.type,
        "time_in_force": proposal.time_in_force,
        "limit_price": proposal.limit_price,
        "max_loss_dollars": proposal.max_loss_dollars,
        "legs": [{
            "symbol": leg.symbol,
            "side": leg.side,
            "quantity": leg.quantity,
        } for leg in proposal.legs],
    }
    encoded = json.dumps(payload, sort_keys=True,
                         separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def unresolved_dispatch_count(view: JournalView) -> int | None:
    """Gate input from the journal; corruption is unknown, never zero."""
    if not view.integrity_ok:
        return None
    return len(view.unresolved_dispatches)


def project_day_risk(day, view: JournalView, trading_date) -> None:
    """Make the mutable day-state number a projection, not an accumulator."""
    risk = view.risk_for(trading_date)
    day.new_risk_dollars = (risk.committed_cents + risk.held_cents) / 100.0
    day.fired_once = list(risk.fire_keys)


def project_gap_usage(view: JournalView, trading_date, key: str) -> int:
    """Read one window counter from the same durable submission truth."""
    return int(view.risk_for(trading_date).gap_units.get(key, 0))


def entry_budget_refusal(portfolio: PortfolioState, day, dollars: float,
                         *, at_risk_cap: float,
                         exposure_cap: float) -> str | None:
    """Pure capacity check; the WAL owns the reservation that follows."""
    if portfolio.max_loss_total + dollars > at_risk_cap:
        return "portfolio"
    if day.new_risk_dollars + dollars > exposure_cap:
        return "daily"
    return None


def make_reservation(*, proposal: Proposal, manifest, account_id: str,
                     trading_date, cycle_id: str,
                     logical_submission_id: str | None = None,
                     fire_keys: tuple[str, ...] = (),
                     gap_counters: tuple[tuple[str, int], ...] = ()) -> Reservation:
    """Bind one logical attempt to its content, account and risk claims."""
    logical_id = logical_submission_id or uuid.uuid4().hex
    fingerprint = proposal_fingerprint(proposal)
    client_id = make_client_order_id(
        manifest_sha=manifest.sha, intent_fingerprint=fingerprint,
        logical_submission_id=logical_id)
    head, _dirty = _code_identity()
    return Reservation(
        logical_submission_id=logical_id,
        client_order_id=client_id,
        account_id=account_id,
        trading_date_et=trading_date,
        cycle_id=cycle_id,
        intent_fingerprint=fingerprint,
        manifest_sha=manifest.sha,
        git_head=head or "UNKNOWN",
        max_loss_cents=int(round(proposal.max_loss_dollars * 100)),
        fire_keys=fire_keys,
        gap_counters=gap_counters,
    )


def new_decision_row(candidate, *, at_utc: datetime,
                     selected: bool, account_scope: str) -> dict:
    """Public facts for one candidate; the retired `accepted` cannot appear."""
    proposal = candidate.proposal
    return {
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


def mark_submission_uncertain(row: dict, exc: Exception) -> None:
    """Record uncertainty without falsely claiming a broker refusal."""
    row["submission_uncertain"] = True
    row["broker_status"] = "unknown"
    row["reason"] = f"submission unresolved: {type(exc).__name__}"


def mark_remaining_aborted(decisions: list[dict], indices: list[int]) -> None:
    """Mark candidates not evaluated after an uncertain broker dispatch."""
    for index in indices:
        row = decisions[index]
        if row["refused_by"]:
            continue
        row["refused_by"] = [
            "control:cycle_aborted_after_uncertain_dispatch"]
        row["reason"] = "not evaluated after an unresolved dispatch"


def broker_order_facts(order) -> dict:
    """JSON-safe immediate broker observation for the public decision row."""
    status = str(getattr(order, "status", "") or "")
    if "." in status:
        status = status.rsplit(".", 1)[-1]
    filled_qty = getattr(order, "filled_qty", 0) or 0
    filled_avg = getattr(order, "filled_avg_price", None)
    try:
        filled_qty = float(filled_qty)
    except (TypeError, ValueError):
        filled_qty = 0.0
    try:
        filled_avg = float(filled_avg) if filled_avg is not None else None
    except (TypeError, ValueError):
        filled_avg = None
    return {
        "broker_order_id": str(order.id),
        "broker_status": status.lower(),
        "filled_qty": filled_qty,
        "filled_avg_price": filled_avg,
    }


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
                     regime=None, day=None, decision_updates=None) -> None:
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
            decision_updates=decision_updates,
            now_utc=state.now_utc)
        snap_mod.write(payload)
    except Exception as exc:                                  # noqa: BLE001
        print(f"{YELLOW}snapshot not written{RESET}: "
              f"{type(exc).__name__}: {exc}")


def run_preflight(state: MarketState, manifest, ledger,
                  journal_view: JournalView | None = None) -> dict:
    head, dirty = _code_identity()
    journal_view = journal_view or SubmissionJournal(
        SUBMISSION_WAL_PATH).replay()
    ctx = checks.EvalContext(
        manifest=manifest, now_utc=state.now_utc, account=state.account,
        is_paper_session=True, clock=state.clock, positions=state.positions,
        ledger_positions=ledger,
        option_quote_age_seconds=state.chain_ages.get("SPY"),
        underlying_bar_age_seconds=_underlying_bar_age(state),
        decision_log_writable=_decisions_writable(),
        git_head=head, git_dirty=dirty,
        unresolved_dispatch_count=unresolved_dispatch_count(journal_view),
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
        # v3.1.1: the window cap on the gap continuation counts prior entries
        # out of this file, matching on engine AND structure — the NFP
        # strangle is also engine event_macro and must not spend the gap's
        # budget. Without this key that match is None == "single_long" for
        # every record, so the tally is always 0 and the cap never trips.
        "structure": proposal.structure,
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
        save_meta(meta)
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

    # These rows are updated by the admission/submission loop below, then
    # published once the final outcome is known. Publishing here used to label
    # a proposer selection as `accepted` before the pretrade gates had run.
    decisions = [new_decision_row(
        c, at_utc=now_utc, selected=(i in chosen),
        account_scope=PUBLIC_ACCOUNT_SCOPE)
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

    # ── 4. fire-once guards, exposure cap, pretrade gates, submit ───────────
    entered_at = now_utc.strftime("%H%M%S")
    submitted = 0
    submission_uncertain = False
    for chosen_position, idx in enumerate(chosen):
        c = candidates[idx]
        p = c.proposal
        row = decisions[idx]

        fire_once = p.engine in ("catalyst", "event_macro", "vol_income")
        if p.engine == "event_macro" and p.structure == "single_long":
            # v3.0 ALL-IN: the NFP gap continuation may fire twice in the
            # 09:30-09:50 window (two cycles) - the second entry is the
            # re-confirmation bet. Bounded by the per-underlying and daily
            # exposure caps, not by the fire-once guard.
            fire_once = False
        if fire_once:
            key = fire_key(p.engine, p.underlying, today)
            if fired(day, key):
                print(f"  {YELLOW}SKIP (already fired today){RESET} "
                      f"{p.underlying} {p.structure}")
                row["refused_by"] = ["control:fire_once"]
                row["reason"] = "already fired today"
                continue
        if p.engine in ("catalyst", "event_macro") and p.expiry and \
                p.expiry.isoformat() < today:
            # 0-DTE is legitimate during session hours; only the PAST is refused
            print(f"  {YELLOW}SKIP{RESET} {p.underlying}: expired-by-design "
                  f"entry ({p.expiry})")
            row["refused_by"] = ["control:expired_contract"]
            row["reason"] = f"contract expired on {p.expiry}"
            continue

        # The gap continuation is capped at N entries per window. Its usage is
        # a projection of durable logical submissions, not a second counter in
        # position metadata; the latter can lag a broker response or be written
        # twice after a retry.
        if p.engine == "event_macro" and p.structure == "single_long":
            gap_total = project_gap_usage(
                journal_view, trading_date, "event_macro:single_long")
            gap_max = int(manifest.get("strategies", "event_macro",
                                       "gap_max_entries_total", default=2))
            if gap_total >= gap_max:
                print(f"  {YELLOW}SKIP{RESET} {p.underlying} {p.structure}: "
                      f"window gap entries {gap_total}/{gap_max} used")
                row["refused_by"] = ["control:gap_entry_limit"]
                row["reason"] = f"window gap entries {gap_total}/{gap_max} used"
                continue

        # Pretrade gates run BEFORE either cap reserves, because both reserve
        # on success and neither refunds. A gate refusal after a reservation
        # leaks it: `day` is written to disk, so a refused proposal used to
        # burn its max loss out of the daily cap for the rest of the session
        # without ever taking a cent of real risk. Observed 2026-08-27, when
        # competition_window refused two NVDA candidates and day_state still
        # recorded new_risk_dollars=4715 against 0 submissions. Reserving only
        # once the proposal is cleared to submit, and handing both back if
        # the submit itself raises, keeps "reserved" and "actually at risk"
        # the same set.
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
            row["refused_by"] = [f"gate:{name}" for name in refused]
            row["reason"] = f"pretrade refused: {', '.join(refused)}"
            continue

        # The WAL, not DayState +=, owns reservations. Re-project before every
        # candidate so a prior submission in this same loop spends headroom.
        journal_view = journal.replay()
        project_day_risk(day, journal_view, trading_date)
        refused_budget = entry_budget_refusal(
            portfolio, day, p.max_loss_dollars,
            at_risk_cap=at_risk_cap, exposure_cap=exposure_cap)
        if refused_budget == "portfolio":
            print(f"  {YELLOW}SKIP{RESET} {p.underlying} {p.structure}: "
                      f"portfolio at-risk cap reached "
                      f"(${portfolio.max_loss_total:,.0f}/${at_risk_cap:,.0f})")
            row["refused_by"] = ["control:portfolio_risk_budget"]
            row["reason"] = "portfolio at-risk cap reached"
            continue
        if refused_budget == "daily":
            print(f"  {YELLOW}SKIP{RESET} {p.underlying} {p.structure}: daily "
                  f"exposure cap reached")
            row["refused_by"] = ["control:daily_exposure_budget"]
            row["reason"] = "daily exposure cap reached"
            continue

        fire_keys = ((fire_key(p.engine, p.underlying, today),)
                     if fire_once else ())
        gap_counters = (("event_macro:single_long", 1),) if (
            p.engine == "event_macro" and p.structure == "single_long") else ()
        reservation = make_reservation(
            proposal=p, manifest=manifest,
            account_id=str(state.account.account_number),
            trading_date=trading_date, cycle_id=cycle_id,
            fire_keys=fire_keys, gap_counters=gap_counters)
        row["authorized"] = True
        row["client_order_id"] = reservation.client_order_id
        try:
            order = dispatch_entry(
                journal, reservation,
                lambda client_id: executor.submit(
                    p, client_order_id=client_id))
        except Exception as exc:                            # noqa: BLE001
            # The request may have reached Alpaca. Leave DISPATCHING held and
            # stop every later entry in this cycle; startup reconciliation will
            # resolve it by the predeclared client order id.
            journal_view = journal.replay()
            project_day_risk(day, journal_view, trading_date)
            mark_submission_uncertain(row, exc)
            print(f"  {RED}SUBMIT UNCERTAIN{RESET}: {exc}")
            submission_uncertain = True
            mark_remaining_aborted(
                decisions, list(chosen[chosen_position + 1:]))
            break
        print(f"  {GREEN}OK{RESET} {order.id}")
        row["submitted"] = True
        row.update(broker_order_facts(order))
        row["reason"] = c.label
        portfolio.max_loss_total += p.max_loss_dollars
        journal_view = journal.replay()
        project_day_risk(day, journal_view, trading_date)

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

        submitted += 1

    atomic_write(DAY_PATH, day.as_dict())
    mirror_from_broker(data.positions())
    publish_snapshot(manifest=manifest, state=state, results=results,
                     blockers=blockers, decisions=decisions, regime=regime,
                     day=day, decision_updates=decision_updates)
    print(f"\nsubmitted {submitted} proposal(s) this cycle")
    return 1 if submission_uncertain else 0


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
