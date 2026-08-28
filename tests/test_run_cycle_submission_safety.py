"""Integration contracts between the WAL and the live cycle."""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from decimal import Decimal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.submission_wal import (DISPATCHING, JournalView, Risk,
                                  SubmissionState)
from scripts import run_cycle
from strategy.daystate import DayState
from strategy.proposal import OptionLeg, Proposal


def _proposal(limit_price: float = -0.50) -> Proposal:
    expiry = date(2026, 8, 28)
    return Proposal(
        engine="trend_income", underlying="NVDA", direction="long",
        structure="credit_vertical", expiry=expiry,
        legs=[
            OptionLeg("NVDA260828P00220000", "sell", 1, 220.0, "put",
                      expiry),
            OptionLeg("NVDA260828P00215000", "buy", 1, 215.0, "put",
                      expiry),
        ],
        limit_price=limit_price, max_loss_dollars=450.0,
        thesis="test",
    )


def test_intent_fingerprint_is_stable_and_sensitive_to_wire_content():
    first = run_cycle.proposal_fingerprint(_proposal())
    retry = run_cycle.proposal_fingerprint(_proposal())
    changed_limit = run_cycle.proposal_fingerprint(_proposal(-0.55))

    assert first == retry
    assert first != changed_limit


def test_broken_journal_maps_to_unknown_gate_input():
    damaged = JournalView(integrity_ok=False, problems=("bad line",))

    assert run_cycle.unresolved_dispatch_count(damaged) is None


def test_unresolved_dispatch_count_comes_from_replayed_state():
    view = JournalView(by_submission={
        "logical-1": SubmissionState(
            "logical-1", DISPATCHING,
            client_order_id="sentinel-logical-1")
    })

    assert run_cycle.unresolved_dispatch_count(view) == 1


def test_day_risk_is_overwritten_from_wal_projection():
    day = DayState(date="2026-08-28", start_equity=100_000,
                   new_risk_dollars=9_193)

    class View:
        def risk_for(self, trading_date):
            assert trading_date == date(2026, 8, 28)
            return Risk(committed_cents=800_000, held_cents=100_000,
                        fire_keys=(), gap_units={})

    run_cycle.project_day_risk(day, View(), date(2026, 8, 28))

    assert day.new_risk_dollars == 9_000


def test_gap_usage_is_read_from_the_wal_projection():
    class View:
        def risk_for(self, trading_date):
            assert trading_date == date(2026, 8, 28)
            return Risk(committed_cents=0, held_cents=0, fire_keys=(),
                        gap_units={"event_macro:single_long": 2})

    assert run_cycle.project_gap_usage(
        View(), date(2026, 8, 28), "event_macro:single_long") == 2


def test_cycle_main_owns_the_single_lock(monkeypatch, tmp_path):
    entered: list[Path] = []

    class Lock:
        def __enter__(self):
            entered.append(tmp_path / "cycle.lock")

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(run_cycle, "CYCLE_LOCK_PATH", tmp_path / "cycle.lock")
    monkeypatch.setattr(run_cycle, "cycle_lock",
                        lambda path, blocking=False: Lock())
    monkeypatch.setattr(run_cycle, "_run_cycle", lambda: 7)

    assert run_cycle.main() == 7
    assert entered == [tmp_path / "cycle.lock"]


def test_new_public_decision_has_no_accepted_alias():
    candidate = SimpleNamespace(proposal=_proposal(), label="trend")

    row = run_cycle.new_decision_row(
        candidate, at_utc=datetime(2026, 8, 28, 16, 0,
                                   tzinfo=timezone.utc),
        selected=True, account_scope="competition")

    assert "accepted" not in row
    assert row["selected"] is True
    assert row["authorized"] is False
    assert row["submitted"] is False
    assert row["account_scope"] == "competition"
    assert row["refused_by"] == []


def test_uncertain_submission_is_not_mislabeled_as_a_refusal():
    row = {"authorized": True, "submitted": False, "refused_by": []}

    run_cycle.mark_submission_uncertain(row, TimeoutError("response lost"))

    assert row["submission_uncertain"] is True
    assert row["broker_status"] == "unknown"
    assert row["refused_by"] == []


def test_uncertain_dispatch_aborts_remaining_selection_order_not_index_order():
    decisions = [
        {"refused_by": []}, {"refused_by": []}, {"refused_by": []}]

    run_cycle.mark_remaining_aborted(decisions, [0, 1])

    assert decisions[0]["refused_by"] == [
        "control:cycle_aborted_after_uncertain_dispatch"]
    assert decisions[1]["refused_by"] == [
        "control:cycle_aborted_after_uncertain_dispatch"]


def test_broker_order_facts_are_json_safe_and_status_is_normalized():
    order = SimpleNamespace(
        id="broker-1", status="OrderStatus.PARTIALLY_FILLED",
        filled_qty=Decimal("0.5"), filled_avg_price=Decimal("-0.62"))

    assert run_cycle.broker_order_facts(order) == {
        "broker_order_id": "broker-1",
        "broker_status": "partially_filled",
        "filled_qty": 0.5,
        "filled_avg_price": -0.62,
    }
