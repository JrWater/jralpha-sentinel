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


def test_gate_refusal_is_returned_without_touching_the_executor():
    from agent.entry_submission import submit_entries

    proposal = _proposal()
    row = {"authorized": False, "submitted": False, "refused_by": []}
    executor = _NeverSubmit()
    result = submit_entries(
        candidates=[SimpleNamespace(proposal=proposal, label="candidate")],
        chosen=[0], decisions=[row],
        state=SimpleNamespace(account=SimpleNamespace(account_number="PA-test")),
        manifest=SimpleNamespace(), executor=executor,
        portfolio=SimpleNamespace(max_loss_total=0.0),
        day=SimpleNamespace(new_risk_dollars=0.0), journal=SimpleNamespace(),
        journal_view=SimpleNamespace(), trading_date=date(2026, 8, 28),
        cycle_id="cycle", at_risk_cap=40_000.0, exposure_cap=30_000.0,
        entry_evaluator=_RefusingEvaluator(), entry_cycle_subject=object(),
    )

    assert executor.calls == []
    assert result.submissions == ()
    assert result.uncertain is False
    assert row["refused_by"] == ["gate:entry_window"]
    assert result.events[0].kind == "gate_refused"
