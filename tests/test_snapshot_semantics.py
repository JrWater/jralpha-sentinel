"""The public snapshot must refuse the legacy pre-pretrade `accepted` lie."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import snapshot


def test_write_uses_the_runtime_snapshot_path(tmp_path, monkeypatch):
    """Paper harnesses redirect SNAPSHOT after module import."""
    isolated = tmp_path / "isolated" / "snapshot.json"
    monkeypatch.setattr(snapshot, "SNAPSHOT", isolated)

    snapshot.write({"schema_version": 1, "decisions": []})

    assert json.loads(isolated.read_text()) == {
        "schema_version": 1, "decisions": []}


class Manifest:
    identity = "TEST/V1"
    policy_id = "TEST"
    version = "1"
    sha = "manifest-sha"

    def get(self, *keys, default=None):
        if keys == ("environment", "required_starting_equity"):
            return 100_000.0
        return default


def _build(decisions):
    return snapshot.build(
        manifest=Manifest(),
        account=SimpleNamespace(
            equity="100000", cash="100000", options_buying_power="100000",
            options_trading_level=3, account_number="PAPER-TEST"),
        clock=SimpleNamespace(is_open=True),
        gate_results={}, gates=(), permit_status="READY", blockers=(),
        positions=[], decisions=decisions, git_head="a" * 40,
        git_dirty=False,
        now_utc=datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc),
    )


def test_snapshot_rejects_new_legacy_accepted_records(monkeypatch):
    monkeypatch.setattr(snapshot, "_read_previous", lambda: {})
    legacy = [{
        "at": "2026-08-28T16:00:00+00:00",
        "engine": "trend_income",
        "underlying": "NVDA",
        "structure": "credit_vertical",
        "accepted": True,
    }]

    with pytest.raises(snapshot.DecisionSchemaError, match="accepted"):
        _build(legacy)


def test_snapshot_rejects_blindly_accumulated_legacy_history(monkeypatch):
    monkeypatch.setattr(snapshot, "_read_previous", lambda: {
        "decisions": [{
            "at": "2026-08-27T16:00:00+00:00",
            "engine": "trend_income",
            "underlying": "NVDA",
            "structure": "credit_vertical",
            "accepted": True,
        }]
    })

    with pytest.raises(snapshot.DecisionSchemaError, match="accepted"):
        _build([])


def test_snapshot_preserves_broker_acceptance_and_fill_as_distinct_facts(
        monkeypatch):
    monkeypatch.setattr(snapshot, "_read_previous", lambda: {})
    decision = {
        "at": "2026-08-28T16:00:00+00:00",
        "engine": "trend_income",
        "underlying": "NVDA",
        "structure": "credit_vertical",
        "selected": True,
        "authorized": True,
        "submitted": True,
        "client_order_id": "sentinel-logical-1",
        "broker_order_id": "broker-1",
        "broker_status": "accepted",
        "filled_qty": 0,
        "filled_avg_price": None,
        "refused_by": [],
    }

    payload = _build([decision])

    public = payload["decisions"][0]
    assert public["submitted"] is True
    assert public["broker_status"] == "accepted"
    assert public["filled_qty"] == 0
    assert "accepted" not in public
    assert payload["schema_version"] == 2


def test_snapshot_applies_reconciled_status_to_accumulated_history(
        monkeypatch):
    monkeypatch.setattr(snapshot, "_read_previous", lambda: {
        "decisions": [{
            "client_order_id": "sentinel-logical-1",
            "submitted": True,
            "broker_status": "accepted",
            "filled_qty": 0,
            "refused_by": [],
        }]
    })

    payload = snapshot.build(
        manifest=Manifest(),
        account=SimpleNamespace(
            equity="100000", cash="100000", options_buying_power="100000",
            options_trading_level=3, account_number="PAPER-TEST"),
        clock=SimpleNamespace(is_open=True), gate_results={}, gates=(),
        permit_status="READY", blockers=(), positions=[], decisions=[],
        git_head="a" * 40, git_dirty=False,
        decision_updates={"sentinel-logical-1": {
            "broker_status": "filled", "filled_qty": 1.0,
            "filled_avg_price": "-0.63"}},
        now_utc=datetime(2026, 8, 28, 16, 30, tzinfo=timezone.utc),
    )

    assert payload["decisions"][0]["broker_status"] == "filled"
    assert payload["decisions"][0]["filled_qty"] == 1.0
