"""Contracts for the entry-submission seam.

The cycle adapter may assemble candidates and print outcomes, but it must not
need a broker session to prove that a rejected proposal never reaches the
executor.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from gates.evaluation import ProposalSubject
from gates.registry import Gate, GateResult
from strategy.proposal import OptionLeg, Proposal


def _proposal() -> Proposal:
    expiry = date(2026, 8, 28)
    return Proposal(
        engine="trend_income", underlying="SPY", direction="neutral",
        structure="credit_vertical", expiry=expiry,
        legs=[
            OptionLeg("SPY260828P00600000", "sell", 1, 600.0, "put", expiry),
            OptionLeg("SPY260828P00595000", "buy", 1, 595.0, "put", expiry),
        ],
        limit_price=-0.50, max_loss_dollars=450.0, thesis="test",
    )


class _RefusingEvaluator:
    gates = (
        Gate("entry_window", lambda _ctx: GateResult(False), ProposalSubject,
             "BLOCKING", "Process Health", "test"),
    )

    def proposal_subject(self, cycle, proposal):
        return proposal

    def evaluate(self, _subject):
        return {"entry_window": GateResult(False, "before entry window")}


class _NeverSubmit:
    def __init__(self):
        self.calls = []

    def submit(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("a gate-refused proposal must never be submitted")


class _ApprovingEvaluator:
    gates = ()

    def proposal_subject(self, _cycle, proposal):
        return proposal

    def evaluate(self, _subject):
        return {}


def _decision_row() -> dict:
    return {"authorized": False, "submitted": False, "refused_by": []}


def test_gate_refusal_is_returned_without_touching_the_executor():
    from agent.entry_submission import submit_entries

    proposal = _proposal()
    row = {"authorized": False, "submitted": False, "refused_by": []}
    executor = _NeverSubmit()
    result = submit_entries(
        candidates=[SimpleNamespace(proposal=proposal, label="candidate")],
        chosen=[0], decisions=[row],
        manifest=SimpleNamespace(),
        portfolio=SimpleNamespace(max_loss_total=0.0),
        day=SimpleNamespace(new_risk_dollars=0.0), journal=SimpleNamespace(),
        journal_view=SimpleNamespace(), trading_date=date(2026, 8, 28),
        at_risk_cap=40_000.0, exposure_cap=30_000.0,
        entry_evaluator=_RefusingEvaluator(), entry_cycle_subject=object(),
        structure_admission=SimpleNamespace(record_pending=lambda *_args: None),
    )

    assert executor.calls == []
    assert result.submissions == ()
    assert result.uncertain is False
    assert row["refused_by"] == ["gate:entry_window"]
    assert result.events[0].kind == "gate_refused"


def test_structure_admission_records_policy_exit_intent_before_dispatch():
    from agent.entry_submission import StructureAdmission

    proposal = _proposal()
    recorded = []
    structures = SimpleNamespace(record_pending_entry=lambda *args, **kwargs:
                                 recorded.append((args, kwargs)))
    manifest = SimpleNamespace(
        exit_intent_for=lambda engine, structure: SimpleNamespace(
            take_profit=0.5, stop_loss_factor=2.0))
    reservation = SimpleNamespace(client_order_id="client-1")

    StructureAdmission(
        manifest, structures, None, None, account_id="account",
        trading_date=date(2026, 8, 28), cycle_id="cycle", entered_at="101500").record_pending(
        proposal, reservation)

    args, kwargs = recorded[0]
    assert args[0] is proposal
    assert args[1:] == ("101500", "client-1")
    assert kwargs == {"take_profit": 0.5, "stop_loss": 2.0}


def test_unresolved_admission_preserves_identity_and_aborts_later_entries(
        tmp_path):
    from agent import submission_wal
    from agent.entry_submission import AdmissionDispatchError, submit_entries

    journal = submission_wal.SubmissionJournal(tmp_path / "submissions.jsonl")
    reservation = SimpleNamespace(client_order_id="sentinel-unresolved")

    class Admission:
        def admit(self, *_args, **_kwargs):
            raise AdmissionDispatchError(reservation)

    rows = [_decision_row(), _decision_row()]
    proposal = _proposal()
    result = submit_entries(
        candidates=[SimpleNamespace(proposal=proposal, label="first"),
                    SimpleNamespace(proposal=proposal, label="second")],
        chosen=[0, 1], decisions=rows, manifest=SimpleNamespace(),
        portfolio=SimpleNamespace(max_loss_total=0.0),
        day=SimpleNamespace(new_risk_dollars=0.0, fired_once=[]),
        journal=journal, journal_view=journal.replay(),
        trading_date=date(2026, 8, 28), at_risk_cap=40_000.0,
        exposure_cap=30_000.0, entry_evaluator=_ApprovingEvaluator(),
        entry_cycle_subject=object(), structure_admission=Admission())

    assert result.uncertain is True
    assert rows[0]["authorized"] is True
    assert rows[0]["client_order_id"] == "sentinel-unresolved"
    assert rows[0]["submission_uncertain"] is True
    assert rows[1]["refused_by"] == [
        "control:cycle_aborted_after_uncertain_dispatch"]


def test_admission_setup_failure_is_refused_and_does_not_abort_cycle(tmp_path):
    from agent import submission_wal
    from agent.entry_submission import submit_entries

    journal = submission_wal.SubmissionJournal(tmp_path / "submissions.jsonl")
    proposal = _proposal()
    calls = []

    class Admission:
        def admit(self, *_args, **_kwargs):
            calls.append("admit")
            raise OSError("journal unavailable")

    row = _decision_row()
    result = submit_entries(
        candidates=[SimpleNamespace(proposal=proposal, label="candidate")],
        chosen=[0], decisions=[row], manifest=SimpleNamespace(),
        portfolio=SimpleNamespace(max_loss_total=0.0),
        day=SimpleNamespace(new_risk_dollars=0.0, fired_once=[]),
        journal=journal, journal_view=journal.replay(),
        trading_date=date(2026, 8, 28), at_risk_cap=40_000.0,
        exposure_cap=30_000.0, entry_evaluator=_ApprovingEvaluator(),
        entry_cycle_subject=object(), structure_admission=Admission())

    assert calls == ["admit"]
    assert result.uncertain is False
    assert row["authorized"] is False
    assert row["refused_by"] == ["control:admission_setup_failed"]
    assert result.events[0].kind == "admission_setup_failed"
