"""The public false-acceptance correction is deterministic and auditable."""
from __future__ import annotations

import copy

import pytest


LEGACY = [{
    "at": "2026-08-27T16:00:00+00:00",
    "engine": "trend_income",
    "underlying": "NVDA",
    "structure": "credit_vertical",
    "max_loss_dollars": 4715.0,
    "accepted": True,
    "reason": "chosen",
}]

EVIDENCE = [{
    "at": "2026-08-27T16:00:00+00:00",
    "engine": "trend_income",
    "underlying": "NVDA",
    "structure": "credit_vertical",
    "account_scope": "competition",
    "selected": True,
    "authorized": False,
    "submitted": False,
    "refused_by": ["gate:competition_window"],
}]

BROKER = {"competition": []}


def _correction():
    from scripts import correct_snapshot_decisions

    return correct_snapshot_decisions


def test_correction_reclassifies_instead_of_deleting_history():
    correction = _correction()

    corrected = correction.correct_decisions(
        copy.deepcopy(LEGACY), cycle_evidence=copy.deepcopy(EVIDENCE),
        broker_orders_by_account=copy.deepcopy(BROKER))

    assert len(corrected) == 1
    assert "accepted" not in corrected[0]
    assert corrected[0]["selected"] is True
    assert corrected[0]["authorized"] is False
    assert corrected[0]["submitted"] is False
    assert corrected[0]["account_scope"] == "competition"
    assert corrected[0]["refused_by"] == ["gate:competition_window"]
    assert corrected[0]["correction"]["code"] == (
        "LEGACY_ACCEPTED_BEFORE_PRETRADE")


def test_correction_is_deterministic_and_idempotent():
    correction = _correction()
    first = correction.correct_decisions(
        copy.deepcopy(LEGACY), cycle_evidence=copy.deepcopy(EVIDENCE),
        broker_orders_by_account=copy.deepcopy(BROKER))
    second = correction.correct_decisions(
        copy.deepcopy(LEGACY), cycle_evidence=copy.deepcopy(EVIDENCE),
        broker_orders_by_account=copy.deepcopy(BROKER))
    third = correction.correct_decisions(
        copy.deepcopy(first), cycle_evidence=copy.deepcopy(EVIDENCE),
        broker_orders_by_account=copy.deepcopy(BROKER))

    assert first == second == third


def test_correction_refuses_to_guess_without_cycle_evidence():
    correction = _correction()

    with pytest.raises(correction.CorrectionEvidenceError,
                       match="cycle evidence"):
        correction.correct_decisions(
            copy.deepcopy(LEGACY), cycle_evidence=[],
            broker_orders_by_account=copy.deepcopy(BROKER))


def test_correction_refuses_when_broker_evidence_disagrees():
    correction = _correction()

    with pytest.raises(correction.CorrectionEvidenceError,
                       match="broker"):
        correction.correct_decisions(
            copy.deepcopy(LEGACY), cycle_evidence=copy.deepcopy(EVIDENCE),
            broker_orders_by_account={
                "competition": [{"id": "unexpected-order"}]})


def test_correction_preserves_a_real_legacy_submission_and_broker_outcome():
    correction = _correction()
    row = copy.deepcopy(LEGACY[0])
    evidence = copy.deepcopy(EVIDENCE[0])
    evidence.update({
        "account_scope": "legacy_test",
        "selected": True,
        "authorized": True,
        "submitted": True,
        "refused_by": [],
        "broker_order_id": "broker-entry",
        "broker_status": "filled",
        "filled_qty": "1",
        "closed_by_order_id": "broker-exit",
        "lifecycle_status": "closed",
    })
    broker = {"legacy_test": [
        {"id": "broker-entry", "status": "filled", "filled_qty": "1"},
        {"id": "broker-exit", "status": "filled"},
    ]}

    corrected = correction.correct_decisions(
        [row], cycle_evidence=[evidence],
        broker_orders_by_account=broker)

    assert corrected[0]["authorized"] is True
    assert corrected[0]["submitted"] is True
    assert corrected[0]["account_scope"] == "legacy_test"
    assert corrected[0]["broker_order_id"] == "broker-entry"
    assert corrected[0]["broker_status"] == "filled"
    assert corrected[0]["lifecycle_status"] == "closed"
    assert corrected[0]["closed_by_order_id"] == "broker-exit"


def test_broker_status_overrides_untrusted_cycle_copy():
    correction = _correction()
    evidence = copy.deepcopy(EVIDENCE[0])
    evidence.update({
        "account_scope": "legacy_test",
        "authorized": True,
        "submitted": True,
        "refused_by": [],
        "broker_order_id": "broker-entry",
        "broker_status": "accepted",
    })

    corrected = correction.correct_decisions(
        copy.deepcopy(LEGACY), cycle_evidence=[evidence],
        broker_orders_by_account={
            "legacy_test": [{"id": "broker-entry", "status": "filled"}]})

    assert corrected[0]["broker_status"] == "filled"


def test_correction_refuses_a_claimed_submission_without_matching_order():
    correction = _correction()
    evidence = copy.deepcopy(EVIDENCE[0])
    evidence.update({
        "account_scope": "legacy_test",
        "authorized": True,
        "submitted": True,
        "refused_by": [],
        "broker_order_id": "missing-order",
    })

    with pytest.raises(correction.CorrectionEvidenceError,
                       match="missing-order"):
        correction.correct_decisions(
            copy.deepcopy(LEGACY), cycle_evidence=[evidence],
            broker_orders_by_account={"legacy_test": []})
