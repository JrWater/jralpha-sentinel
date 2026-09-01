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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.executor import Executor
from agent.cycle_lock import CycleAlreadyRunning, cycle_lock
from agent.entry_submission import (StructureAdmission, project_day_risk,
                                    proposal_fingerprint, submit_entries)
from agent.ledger import (StructureLedger, atomic_write, append_decision,
                          ledger_positions, mirror_from_broker)
from agent.position_lifecycle import PositionLifecycle
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
from strategy.data import AlpacaData, MarketState, partition_positions
from strategy.daystate import check_kill, load_or_reset, record_risk, release_risk
from strategy.engine import EngineContext, run as run_engines
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


@dataclass(frozen=True)
class CycleEnvironment:
    """All mutable adapters for one operational cycle.

    The cycle owns ordering; a CLI, scheduler, or rehearsal supplies only
    isolated paths and adapter implementations through this boundary.
    """

    root: Path
    state_dir: Path
    structures: StructureLedger
    manifest_loader: Callable[[], Any]
    environment_loader: Callable[[Path], dict]
    credential_loader: Callable[[dict], tuple[str | None, str | None]]
    data_factory: Callable[[str, str], AlpacaData]
    executor_factory: Callable[..., Executor]
    engine_runner: Callable[[EngineContext], list]
    snapshot_writer: Callable[[dict], None]
    snapshot_history_path: Path
    account_scope: str = PUBLIC_ACCOUNT_SCOPE

    @property
    def day_path(self) -> Path:
        return self.state_dir / "day_state.json"

    @property
    def submission_wal_path(self) -> Path:
        return self.state_dir / "submission_wal.jsonl"

    @property
    def cycle_lock_path(self) -> Path:
        return self.state_dir / "cycle.lock"

    @property
    def ledger_path(self) -> Path:
        return self.state_dir / "ledger.json"

    @property
    def decisions_path(self) -> Path:
        return self.state_dir / "decisions.jsonl"

    @property
    def permit_path(self) -> Path:
        return self.state_dir / "entry_permit.json"

    @property
    def prior_snapshot_path(self) -> Path:
        return self.snapshot_history_path

    def append_decision(self, record: dict) -> None:
        append_decision(record, path=self.decisions_path)

    def mirror_positions(self, positions) -> dict:
        return mirror_from_broker(positions, path=self.ledger_path)

    def ledger_positions(self) -> list[dict]:
        return ledger_positions(path=self.ledger_path)

    def decision_log_writable(self) -> bool:
        try:
            self.decisions_path.parent.mkdir(parents=True, exist_ok=True)
            with self.decisions_path.open("a"):
                pass
            return True
        except OSError:
            return False


def production_environment() -> CycleEnvironment:
    """Bind the default CLI/scheduler path to the production state directory."""
    from agent import snapshot as snapshot_mod

    return CycleEnvironment(
        root=ROOT, state_dir=DAY_PATH.parent, structures=STRUCTURES,
        manifest_loader=load_manifest, environment_loader=load_env,
        credential_loader=creds, data_factory=AlpacaData,
        executor_factory=Executor, engine_runner=run_engines,
        snapshot_writer=snapshot_mod.write,
        account_scope=PUBLIC_ACCOUNT_SCOPE,
        snapshot_history_path=ROOT / "docs" / "snapshot.json")


@dataclass(frozen=True)
class CycleResult:
    """The public outcome of one ordered operational cycle."""

    exit_code: int
    disposition: str
    blockers: tuple[str, ...] = ()
    decisions: tuple[dict, ...] = ()
    gate_results: dict[str, Any] = field(default_factory=dict)
    submission_count: int = 0
    uncertain: bool = False


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
    positions, non_option_positions = partition_positions(data.positions())
    state = MarketState(account=account, clock=clock,
                        equity=float(account.equity), positions=positions,
                        non_option_positions=non_option_positions,
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
                     regime=None, day=None, decision_updates=None,
                     root: Path = ROOT,
                     snapshot_writer: Callable[[dict], None] | None = None,
                     previous_snapshot_path: Path | None = None
                     ) -> None:
    """Write the credential-free page the judges read. Never fatal.

    A dashboard that cannot render must not be able to stop the agent from
    trading, so every failure here is reported and swallowed.
    """
    from agent import snapshot as snap_mod
    head, dirty = code_identity(root)
    try:
        payload = snap_mod.build(
            manifest=manifest, account=state.account, clock=state.clock,
            gate_results=results, gates=checks.GATES,
            permit_status="BLOCKED" if blockers else "READY",
            blockers=blockers,
            positions=[*state.positions, *state.non_option_positions],
            decisions=decisions, git_head=head, git_dirty=dirty,
            regime=regime, day_state=(day.as_dict() if day else None),
            decision_updates=decision_updates,
            now_utc=state.now_utc,
            previous_snapshot_path=(previous_snapshot_path or
                                    snap_mod.SNAPSHOT))
        (snapshot_writer or snap_mod.write)(payload)
    except Exception as exc:                                  # noqa: BLE001
        print(f"{YELLOW}snapshot not written{RESET}: "
              f"{type(exc).__name__}: {exc}")


def run_preflight(state: MarketState, manifest, ledger,
                  journal_view: JournalView | None = None,
                  *, structures: StructureLedger | None = None,
                  root: Path = ROOT,
                  decision_log_writable: bool | None = None) -> dict:
    journal_view = journal_view or SubmissionJournal(
        SUBMISSION_WAL_PATH).replay()
    evaluator = GateEvaluator(root=root)
    subject = evaluator.cycle_subject(
        state=state, manifest=manifest, ledger_positions=ledger,
        journal_view=journal_view,
        decision_log_writable=decision_log_writable,
        unresolved_structure_close_count=(
            structures or STRUCTURES).unresolved_structure_close_count(),
        unresolved_entry_reconciliation_count=(
            structures or STRUCTURES).unresolved_entry_reconciliation_count(),
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
                 *, structures: StructureLedger | None = None,
                 record_decision: Callable[[dict], None] | None = None) -> int:
    """Delegate structure exits to their lifecycle module.

    Kept here as the cycle's compatibility seam; all lifecycle semantics live
    in :class:`agent.position_lifecycle.PositionLifecycle`.
    """
    return PositionLifecycle(record_decision=record_decision or append_decision).manage(
        state, manifest, executor, structures=structures or STRUCTURES)


# ── the cycle ────────────────────────────────────────────────────────────────

def _run_cycle(environment: CycleEnvironment) -> CycleResult:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--exits-only", action="store_true",
                    help="manage the existing book but size nothing new "
                         "(used past the final trading date)")
    args = ap.parse_args()

    manifest = environment.manifest_loader()
    env = environment.environment_loader(environment.root / args.env)
    key, secret = environment.credential_loader(env)
    if not key or not secret:
        print(f"{RED}No credentials.{RESET} Fill {environment.root}/{args.env}.")
        return CycleResult(2, "missing_credentials")

    data = environment.data_factory(key, secret)
    symbols = sorted(set(manifest.declared_symbols()))
    state = build_state(data, manifest, symbols)
    now_utc = state.now_utc
    timezone_name = str(manifest.get("session", "timezone"))
    trading_date = now_utc.astimezone(ZoneInfo(timezone_name)).date()
    cycle_id = uuid.uuid4().hex
    journal = SubmissionJournal(environment.submission_wal_path)
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
        raw_day = json.loads(environment.day_path.read_text())
    except (OSError, json.JSONDecodeError):
        raw_day = None
    limits = manifest.risk_limits()
    day = load_or_reset(raw_day, today=today, equity_now=state.equity,
                        scale_fraction=limits.drawdown_scale_fraction)
    project_day_risk(day, journal_view, trading_date)
    killed = check_kill(day, state.equity,
                        limits.daily_loss_kill_fraction)
    exposure_cap = limits.daily_new_exposure_cap
    at_risk_cap = limits.at_risk_cap

    # ── 1. establish the broker/ledger view used by risk-reducing exits ────
    environment.mirror_positions(data.positions())
    ledger = environment.ledger_positions()

    # ── 2. exits FIRST: the book is settled before we size anything new ─────
    # Exits are risk-REDUCING, so they run even when preflight gates are red;
    # the permit only gates NEW exposure. This is what keeps the final-day
    # flatten alive even if a data gate is red at 10:45 ET.
    executor = environment.executor_factory(
        data.trading, manifest, record_decision=environment.append_decision)
    if not args.dry_run:
        preserved_lifecycle_orders = \
            environment.structures.protected_open_order_ids()
        pending_entry_clients = \
            environment.structures.pending_entry_client_order_ids()
        executor.retry_open_orders_cleanup(
            preserve_order_ids=preserved_lifecycle_orders,
            preserve_client_order_ids=pending_entry_clients)
        # An accepted DAY entry is owned by its pending structure record until
        # the broker proves fill, cancel, rejection, or expiry.  Cleanup may
        # still remove unknown orders, but must not erase that reconciliation
        # evidence before the following read.
        entry_reconciliation = environment.structures.reconcile_pending_entries(
            data.trading.get_order_by_client_id)
        if (entry_reconciliation.activated or entry_reconciliation.discarded
                or entry_reconciliation.quarantined):
            print(f"entry reconciliation: activated "
                  f"{len(entry_reconciliation.activated)}, discarded "
                  f"{len(entry_reconciliation.discarded)}, quarantined "
                  f"{len(entry_reconciliation.quarantined)}")
        manage_exits(state, manifest, executor, structures=environment.structures,
                     record_decision=environment.append_decision)
        # Rebuild the fact set even when no close was submitted: reconciliation
        # may have quarantined a partial or unknown close.  The ensuing permit
        # must describe the post-exit world, not the one it observed before
        # lifecycle ownership was resolved.
        state.positions, state.non_option_positions = partition_positions(
            data.positions())
        environment.mirror_positions(data.positions())
        ledger = environment.ledger_positions()

    # ── 3. preflight gates for NEW exposure ─────────────────────────────────
    # This deliberately happens after exits.  A partial/unknown structure
    # close therefore turns Entry Authority red in this very cycle, while
    # exit management above remains available even if the gate is red.
    results = run_preflight(
        state, manifest, ledger, journal_view, structures=environment.structures,
        root=environment.root,
        decision_log_writable=environment.decision_log_writable())
    print("\npreflight gates")
    print_gates(results)
    blockers = [n for n, r in results.items()
                if not r.ok and severity_of(checks.GATES, n) == "BLOCKING"]

    if not args.dry_run:
        write_permit(results, checks.GATES, manifest_sha=manifest.sha,
                     path=environment.permit_path, repo=environment.root)

    # Past the final trading date the account must stop growing, but it must
    # not stop being managed: exits are the only way a residual position from
    # an unfilled 09-04 flatten limit ever gets closed, and manage_exits is
    # the sole exit path in this repo. Suppressing NEW exposure is the whole
    # requirement; suppressing the cycle would strand the book.
    if args.exits_only:
        print(f"\n{YELLOW}EXITS ONLY{RESET}: past the final trading date — "
              f"book still managed, nothing new will be sized.")
        return CycleResult(0, "exits_only", tuple(blockers),
                           gate_results=results)

    if blockers and not args.dry_run:
        print(f"\n{RED}PERMIT REFUSED{RESET}: {', '.join(blockers)} — no new "
              f"exposure this cycle.")
        publish_snapshot(manifest=manifest, state=state, results=results,
                         blockers=blockers, decisions=[], day=day,
                         decision_updates=decision_updates,
                         root=environment.root,
                         snapshot_writer=environment.snapshot_writer,
                         previous_snapshot_path=environment.prior_snapshot_path)
        return CycleResult(1, "entry_permit_refused", tuple(blockers),
                           gate_results=results)

    # ── 4. engine candidates ────────────────────────────────────────────────
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

    meta = environment.structures.load()
    open_groups = [g for g in meta.get("groups", {}).values()
                   if not g.get("closed")]
    portfolio = PortfolioState(
        max_loss_by_underlying={},
        max_loss_total=sum(float(g.get("max_loss_dollars", 0.0))
                           for g in open_groups),
        count_by_engine={},
        current_equity=state.equity,
        starting_equity=limits.starting_equity,
        scale=day.scale,
    )
    for g in open_groups:
        eng = g.get("engine", "?")
        portfolio.count_by_engine[eng] = portfolio.count_by_engine.get(eng, 0) + 1

    ctx = EngineContext(state=state, manifest=manifest, regime=regime,
                        now_et=now_et, signals=signals, portfolio=portfolio)
    candidates = environment.engine_runner(ctx)

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
        account_scope=environment.account_scope, proposer=proposer_result)
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
                         day=day, decision_updates=decision_updates,
                         root=environment.root,
                         snapshot_writer=environment.snapshot_writer,
                         previous_snapshot_path=environment.prior_snapshot_path)
        print(f"\n{DIM}dry run — nothing was sent.{RESET}")
        return CycleResult(0, "dry_run", tuple(blockers), tuple(decisions),
                           results)
    if blockers or killed or not chosen:
        why = "kill switch" if killed else (
            "permit refused" if blockers else "no selection")
        print(f"\n{DIM}{why} — nothing was sent.{RESET}")
        atomic_write(environment.day_path, day.as_dict())
        publish_snapshot(manifest=manifest, state=state, results=results,
                         blockers=blockers, decisions=decisions, regime=regime,
                         day=day, decision_updates=decision_updates,
                         root=environment.root,
                         snapshot_writer=environment.snapshot_writer,
                         previous_snapshot_path=environment.prior_snapshot_path)
        return CycleResult(0, "no_new_entry", tuple(blockers),
                           tuple(decisions), results)

    # ── 5. admission, WAL reservation, pretrade gates, submit ─────────────
    # Entry evaluation begins after exits refresh the broker and ledger views.
    # The typed subject supplies one shared fact set to every selected proposal;
    # `submit_entries` returns outcomes rather than emitting its own output.
    entry_evaluator = GateEvaluator(root=environment.root)
    entry_cycle_subject = entry_evaluator.cycle_subject(
        state=state, manifest=manifest, ledger_positions=ledger,
        journal_view=journal_view,
        decision_log_writable=environment.decision_log_writable(),
        unresolved_structure_close_count=(
            environment.structures.unresolved_structure_close_count()),
        unresolved_entry_reconciliation_count=(
            environment.structures.unresolved_entry_reconciliation_count()),
    )
    entered_at = now_utc.strftime("%H%M%S")
    underlying_baselines: dict[str, int] = {}
    for position in state.non_option_positions:
        try:
            quantity = int(float(position.qty))
        except (AttributeError, TypeError, ValueError):
            continue
        symbol = str(getattr(position, "symbol", "")).strip().upper()
        if symbol:
            underlying_baselines[symbol] = \
                underlying_baselines.get(symbol, 0) + quantity

    entry_result = submit_entries(
        candidates=candidates, chosen=chosen, decisions=decisions,
        manifest=manifest, portfolio=portfolio,
        day=day, journal=journal, journal_view=journal_view,
        trading_date=trading_date,
        at_risk_cap=at_risk_cap, exposure_cap=exposure_cap,
        entry_evaluator=entry_evaluator,
        entry_cycle_subject=entry_cycle_subject,
        structure_admission=StructureAdmission(
            manifest, environment.structures, journal, executor,
            account_id=str(state.account.account_number),
            trading_date=trading_date, cycle_id=cycle_id,
            entered_at=entered_at,
            underlying_baselines=underlying_baselines),
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

    atomic_write(environment.day_path, day.as_dict())
    environment.mirror_positions(data.positions())
    publish_snapshot(manifest=manifest, state=state, results=results,
                     blockers=blockers, decisions=decisions, regime=regime,
                     day=day, decision_updates=decision_updates,
                     root=environment.root,
                     snapshot_writer=environment.snapshot_writer,
                     previous_snapshot_path=environment.prior_snapshot_path)
    print(f"\nsubmitted {len(entry_result.submissions)} proposal(s) this cycle")
    return CycleResult(
        1 if entry_result.uncertain else 0,
        "submission_uncertain" if entry_result.uncertain else "completed",
        tuple(blockers), tuple(decisions), results,
        submission_count=len(entry_result.submissions),
        uncertain=entry_result.uncertain)


def run(environment: CycleEnvironment | None = None) -> CycleResult:
    """Run one Cycle behind the shared adapter and outcome seams."""
    environment = environment or production_environment()
    try:
        with cycle_lock(environment.cycle_lock_path, blocking=False):
            return _run_cycle(environment)
    except CycleAlreadyRunning as exc:
        print(f"{YELLOW}CYCLE REFUSED{RESET}: {exc}")
        return CycleResult(0, "already_running")


def main(environment: CycleEnvironment | None = None) -> int:
    """CLI compatibility adapter for the Cycle result seam."""
    return run(environment).exit_code


if __name__ == "__main__":
    raise SystemExit(main())
