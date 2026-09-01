"""Admission and submission of selected entry proposals.

``scripts.run_cycle`` is an adapter: it fetches data, builds candidates and
renders the returned events.  This module owns the safety-sensitive entry
loop, so a recorder executor can exercise it without a broker session and
every production adapter takes the same path.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any, Sequence

from agent.submission_wal import (DISPATCHING, JournalView, Reservation,
                                  dispatch_entry, make_client_order_id)
from gates.evaluation import code_identity
from gates.registry import severity_of
from strategy.daystate import fire_key, fired
from strategy.proposal import Proposal


def proposal_fingerprint(proposal: Proposal) -> str:
    """Canonical identity of all declared entry wire content and risk."""
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


def make_reservation(*, proposal: Proposal, manifest, account_id: str,
                     trading_date: date, cycle_id: str,
                     logical_submission_id: str | None = None,
                     fire_keys: tuple[str, ...] = (),
                     gap_counters: tuple[tuple[str, int], ...] = ()) -> Reservation:
    """Bind one logical attempt to its content, account and risk claims."""
    logical_id = logical_submission_id or uuid.uuid4().hex
    fingerprint = proposal_fingerprint(proposal)
    client_id = make_client_order_id(
        manifest_sha=manifest.sha, intent_fingerprint=fingerprint,
        logical_submission_id=logical_id)
    head, _dirty = code_identity()
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


def project_day_risk(day, view: JournalView, trading_date: date) -> None:
    """Make the mutable day-state number a WAL projection, not an accumulator."""
    risk = view.risk_for(trading_date)
    day.new_risk_dollars = (risk.committed_cents + risk.held_cents) / 100.0
    day.fired_once = list(risk.fire_keys)


def project_gap_usage(view: JournalView, trading_date: date, key: str) -> int:
    """Read one entry-window counter from durable submission truth."""
    return int(view.risk_for(trading_date).gap_units.get(key, 0))


def entry_budget_refusal(portfolio, day, dollars: float, *, at_risk_cap: float,
                         exposure_cap: float) -> str | None:
    """Pure capacity check; the WAL owns the reservation that follows."""
    if portfolio.max_loss_total + dollars > at_risk_cap:
        return "portfolio"
    if day.new_risk_dollars + dollars > exposure_cap:
        return "daily"
    return None


@dataclass(frozen=True)
class EntryEvent:
    """One observable entry-loop outcome for the cycle adapter to render."""

    kind: str
    index: int
    proposal: Proposal
    detail: str
    order: Any = None


class StructureAdmission:
    """Own reservation, durable structure handoff, and broker dispatch."""

    def __init__(self, manifest, structures, journal, executor, *,
                 account_id: str, trading_date: date, cycle_id: str,
                 entered_at: str,
                 underlying_baselines: dict[str, int] | None = None):
        self._manifest = manifest
        self._structures = structures
        self._journal = journal
        self._executor = executor
        self._account_id = account_id
        self._trading_date = trading_date
        self._cycle_id = cycle_id
        self._entered_at = entered_at
        self._underlying_baselines = underlying_baselines or {}

    def record_pending(self, proposal: Proposal, reservation: Reservation) -> None:
        exit_intent = self._manifest.exit_intent_for(
            proposal.engine, proposal.structure)
        kwargs = {
            "take_profit": exit_intent.take_profit,
            "stop_loss": exit_intent.stop_loss_factor,
        }
        kwargs["pre_expiry_underlying_qty"] = \
            self._underlying_baselines.get(proposal.underlying, 0)
        self._structures.record_pending_entry(
            proposal, self._entered_at, reservation.client_order_id,
            **kwargs)

    def admit(self, proposal: Proposal, *, fire_keys: tuple[str, ...],
              gap_counters: tuple[tuple[str, int], ...]) -> tuple[Reservation, Any]:
        """Reserve, hand off reconciliation identity, then dispatch once."""
        reservation = make_reservation(
            proposal=proposal, manifest=self._manifest,
            account_id=self._account_id, trading_date=self._trading_date,
            cycle_id=self._cycle_id, fire_keys=fire_keys,
            gap_counters=gap_counters)
        try:
            order = dispatch_entry(
                self._journal, reservation,
                lambda client_id: self._executor.submit(
                    proposal, client_order_id=client_id),
                before_broker=lambda reserved: self.record_pending(
                    proposal, reserved))
        except Exception as exc:                            # noqa: BLE001
            record = self._journal.replay().by_submission.get(
                reservation.logical_submission_id)
            if record is not None and record.state == DISPATCHING:
                raise AdmissionDispatchError(reservation) from exc
            raise
        return reservation, order


class AdmissionDispatchError(RuntimeError):
    """A post-reservation failure whose client identity needs reconciliation."""

    def __init__(self, reservation: Reservation):
        super().__init__("admission dispatch did not complete")
        self.reservation = reservation


@dataclass(frozen=True)
class SubmittedEntry:
    """The one fact a caller needs to book a broker-accepted structure."""

    index: int
    candidate: Any
    proposal: Proposal
    order: Any


@dataclass(frozen=True)
class EntrySubmissionResult:
    """Returned facts; the adapter decides presentation, not lifecycle writes."""

    submissions: tuple[SubmittedEntry, ...]
    events: tuple[EntryEvent, ...]
    journal_view: JournalView
    uncertain: bool


def _broker_order_facts(order) -> dict:
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


def _mark_remaining_aborted(decisions: list[dict], indices: Sequence[int]) -> None:
    for index in indices:
        row = decisions[index]
        if row["refused_by"]:
            continue
        row["refused_by"] = [
            "control:cycle_aborted_after_uncertain_dispatch"]
        row["reason"] = "not evaluated after an unresolved dispatch"


def _mark_submission_uncertain(row: dict, exc: Exception) -> None:
    """Record broker ambiguity without turning it into a false refusal."""
    row["submission_uncertain"] = True
    row["broker_status"] = "unknown"
    row["reason"] = f"submission unresolved: {type(exc).__name__}"


def submit_entries(*, candidates: Sequence[Any], chosen: Sequence[int],
                   decisions: list[dict], manifest,
                   portfolio, day, journal, journal_view: JournalView,
                   trading_date: date, at_risk_cap: float,
                   exposure_cap: float, entry_evaluator,
                   entry_cycle_subject,
                   structure_admission: StructureAdmission
                   ) -> EntrySubmissionResult:
    """Evaluate and submit selected entries, returning facts rather than prints.

    The input/output boundary is deliberately broker-neutral.  The admission
    object owns the executor; the critical guarantee is invariant in both
    modes: a gate-refused proposal never reaches that executor.
    """
    events: list[EntryEvent] = []
    submissions: list[SubmittedEntry] = []
    current_view = journal_view
    uncertain = False

    for chosen_position, index in enumerate(chosen):
        candidate = candidates[index]
        proposal = candidate.proposal
        row = decisions[index]

        fire_once = proposal.engine in ("catalyst", "event_macro", "vol_income")
        if proposal.engine == "event_macro" and proposal.structure == "single_long":
            fire_once = False
        if fire_once:
            key = fire_key(proposal.engine, proposal.underlying,
                           trading_date.isoformat())
            if fired(day, key):
                row["refused_by"] = ["control:fire_once"]
                row["reason"] = "already fired today"
                events.append(EntryEvent("fire_once", index, proposal,
                                         "already fired today"))
                continue

        if proposal.engine in ("catalyst", "event_macro") and proposal.expiry and \
                proposal.expiry.isoformat() < trading_date.isoformat():
            row["refused_by"] = ["control:expired_contract"]
            row["reason"] = f"contract expired on {proposal.expiry}"
            events.append(EntryEvent("expired_contract", index, proposal,
                                     row["reason"]))
            continue

        if proposal.engine == "event_macro" and proposal.structure == "single_long":
            gap_total = project_gap_usage(
                current_view, trading_date, "event_macro:single_long")
            gap_max = manifest.event_gap_entry_limit()
            if gap_total >= gap_max:
                row["refused_by"] = ["control:gap_entry_limit"]
                row["reason"] = f"window gap entries {gap_total}/{gap_max} used"
                events.append(EntryEvent("gap_entry_limit", index, proposal,
                                         row["reason"]))
                continue

        pre = entry_evaluator.evaluate(
            entry_evaluator.proposal_subject(entry_cycle_subject, proposal))
        refused = [name for name, result in pre.items() if not result.ok
                   and severity_of(entry_evaluator.gates, name) == "BLOCKING"]
        if refused:
            row["refused_by"] = [f"gate:{name}" for name in refused]
            row["reason"] = f"pretrade refused: {', '.join(refused)}"
            events.append(EntryEvent("gate_refused", index, proposal,
                                     row["reason"]))
            continue

        # Read the durable projection immediately before every reservation so
        # a prior logical submission in this same cycle consumes headroom.
        current_view = journal.replay()
        project_day_risk(day, current_view, trading_date)
        budget = entry_budget_refusal(
            portfolio, day, proposal.max_loss_dollars,
            at_risk_cap=at_risk_cap, exposure_cap=exposure_cap)
        if budget == "portfolio":
            row["refused_by"] = ["control:portfolio_risk_budget"]
            row["reason"] = "portfolio at-risk cap reached"
            events.append(EntryEvent("portfolio_risk_budget", index, proposal,
                                     row["reason"]))
            continue
        if budget == "daily":
            row["refused_by"] = ["control:daily_exposure_budget"]
            row["reason"] = "daily exposure cap reached"
            events.append(EntryEvent("daily_exposure_budget", index, proposal,
                                     row["reason"]))
            continue

        fire_keys = ((fire_key(proposal.engine, proposal.underlying,
                               trading_date.isoformat()),)
                     if fire_once else ())
        gap_counters = (("event_macro:single_long", 1),) if (
            proposal.engine == "event_macro" and
            proposal.structure == "single_long") else ()
        try:
            reservation, order = structure_admission.admit(
                proposal, fire_keys=fire_keys, gap_counters=gap_counters)
            row["authorized"] = True
            row["client_order_id"] = reservation.client_order_id
        except AdmissionDispatchError as exc:
            row["authorized"] = True
            row["client_order_id"] = exc.reservation.client_order_id
            current_view = journal.replay()
            project_day_risk(day, current_view, trading_date)
            _mark_submission_uncertain(row, exc)
            _mark_remaining_aborted(decisions, chosen[chosen_position + 1:])
            events.append(EntryEvent("submission_uncertain", index, proposal,
                                     row["reason"]))
            uncertain = True
            break
        except Exception as exc:                            # noqa: BLE001
            row["refused_by"] = ["control:admission_setup_failed"]
            row["reason"] = f"admission setup failed: {type(exc).__name__}"
            events.append(EntryEvent("admission_setup_failed", index, proposal,
                                     row["reason"]))
            continue

        row["submitted"] = True
        row.update(_broker_order_facts(order))
        row["reason"] = candidate.label
        portfolio.max_loss_total += proposal.max_loss_dollars
        current_view = journal.replay()
        project_day_risk(day, current_view, trading_date)
        submissions.append(SubmittedEntry(index, candidate, proposal, order))
        events.append(EntryEvent("submitted", index, proposal,
                                 str(order.id), order))

    return EntrySubmissionResult(tuple(submissions), tuple(events),
                                 current_view, uncertain)
