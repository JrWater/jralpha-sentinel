"""The public dashboard renders the new decision facts, not `accepted`."""
from __future__ import annotations

from dashboard.decision_view import classify_decision, summarize_decisions


def test_summary_counts_submission_and_refusal_as_distinct_facts():
    rows = [
        {"submitted": True, "refused_by": [], "account_scope": "legacy_test"},
        {"submitted": False, "refused_by": ["gate:competition_window"],
         "account_scope": "competition"},
    ]

    assert summarize_decisions(rows) == {
        "recorded": 2, "submitted": 1, "refused": 1}


def test_closed_legacy_test_is_not_rendered_as_a_competition_refusal():
    view = classify_decision({
        "selected": True,
        "authorized": True,
        "submitted": True,
        "broker_status": "filled",
        "lifecycle_status": "closed",
        "account_scope": "legacy_test",
        "refused_by": [],
    })

    assert view.icon == "✅"
    assert view.label == "filled and closed"
    assert view.account_scope == "legacy_test"


def test_gate_refusal_names_the_refusing_gate():
    view = classify_decision({
        "selected": True,
        "authorized": False,
        "submitted": False,
        "account_scope": "competition",
        "refused_by": ["gate:competition_window"],
    })

    assert view.icon == "⛔"
    assert view.label == "refused by gate:competition_window"


def test_uncertain_submission_is_not_rendered_as_refused():
    view = classify_decision({
        "selected": True,
        "authorized": True,
        "submitted": False,
        "submission_uncertain": True,
        "account_scope": "competition",
        "refused_by": [],
    })

    assert view.icon == "⚠️"
    assert view.label == "submission uncertain; reconciliation pending"


def test_partial_fill_is_not_claimed_as_fully_filled():
    view = classify_decision({
        "submitted": True,
        "broker_status": "partially_filled",
        "account_scope": "competition",
        "refused_by": [],
    })

    assert view.icon == "📤"
    assert view.label == "submitted · partially_filled"
