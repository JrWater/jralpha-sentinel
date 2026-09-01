"""Tests for the semantic policy projection seam."""
from __future__ import annotations

import copy
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from policy.loader import LossBudget, Manifest


ROOT = Path(__file__).resolve().parents[1]


def _manifest() -> Manifest:
    raw = json.loads((ROOT / "policy" / "manifest.json").read_text())
    raw = copy.deepcopy(raw)
    raw["environment"]["required_starting_equity"] = 1_000.0
    raw["risk_caps"].update({
        "at_risk_cap_fraction": 0.40,
        "daily_new_exposure_cap_fraction": 0.30,
        "max_loss_per_position_fraction": 0.12,
        "equity_floor_fraction": 0.70,
    })
    raw["session"].update({
        "timezone": "America/New_York",
        "no_new_exposure_before": "10:00",
        "no_new_exposure_after": "15:30",
        "flatten_all_at": "10:45",
        "final_trading_date": "2026-09-04",
        "no_new_exposure_on_final_date": True,
        "final_day_event_exception": "nfp_gap",
    })
    raw["strategies"]["event_macro"]["entry_open_override"] = "09:30"
    raw["strategies"]["event_macro"].update({
        "take_profit_fraction": 0.60,
        "stop_loss_fraction": 0.50,
    })
    raw["strategies"]["vol_income"].update({
        "take_profit_fraction": 0.50,
        "stop_loss_multiple": 2.0,
    })
    return Manifest(raw)


def test_semantic_projection_owns_entry_risk_and_exit_meaning() -> None:
    manifest = _manifest()

    regular = manifest.entry_window_for("trend_directional")
    event = manifest.entry_window_for("event_macro")
    assert regular.opens_at == "10:00"
    assert event.opens_at == "09:30"
    assert event.closes_at == "15:30"
    assert regular.contains(datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc))
    assert not regular.contains(datetime(2026, 9, 3, 13, 59, tzinfo=timezone.utc))

    limits = manifest.risk_limits()
    assert limits.starting_equity == 1_000.0
    assert limits.at_risk_cap == 400.0
    assert limits.daily_new_exposure_cap == 300.0
    assert limits.per_position_loss_cap == 120.0
    assert limits.equity_floor == 700.0
    assert manifest.engine_loss_cap(
        "catalyst", budget=LossBudget.PRE_EVENT) == 120.0
    assert manifest.event_gap_entry_limit() == 2

    final_day = manifest.final_day_rules()
    assert final_day.is_entry_frozen(date(2026, 9, 4))
    assert not final_day.is_entry_frozen(date(2026, 9, 3))
    assert final_day.allows_event_candidate(date(2026, 9, 4), "event-nfp-gap")
    assert not final_day.allows_event_candidate(date(2026, 9, 4), "event-unlisted")
    assert final_day.flatten_at == "10:45"

    credit_exit = manifest.exit_intent_for("vol_income", "iron_condor")
    debit_exit = manifest.exit_intent_for("event_macro", "debit_vertical")
    assert (credit_exit.take_profit, credit_exit.stop_loss_factor) == (0.50, 2.0)
    assert (debit_exit.take_profit, debit_exit.stop_loss_factor) == (0.60, 0.50)


def test_any_strategy_may_declare_its_own_entry_window_override() -> None:
    raw = json.loads((ROOT / "policy" / "manifest.json").read_text())
    raw["strategies"]["trend_directional"]["entry_open_override"] = "09:45"

    assert Manifest(raw).entry_window_for("trend_directional").opens_at == "09:45"


def test_exit_intent_refuses_an_undeclared_structure() -> None:
    with pytest.raises(ValueError, match="does not declare structure"):
        _manifest().exit_intent_for("vol_income", "straddle")


def test_final_day_candidate_exception_must_be_manifest_policy() -> None:
    raw = json.loads((ROOT / "policy" / "manifest.json").read_text())
    raw["session"].pop("final_day_event_exception", None)

    with pytest.raises(KeyError, match="final_day_event_exception"):
        Manifest(raw)


def test_final_day_event_exception_must_be_a_known_policy_concept() -> None:
    raw = json.loads((ROOT / "policy" / "manifest.json").read_text())
    raw["session"]["final_day_event_exception"] = "unlisted"
    with pytest.raises(ValueError, match="final_day_event_exception"):
        Manifest(raw)


@pytest.mark.parametrize(
    ("key", "value"),
    [("timezone", "Not/AZone"), ("no_new_exposure_before", "bad-time"),
     ("no_new_exposure_after", "09:00")],
)
def test_manifest_rejects_an_invalid_entry_window(key, value) -> None:
    raw = json.loads((ROOT / "policy" / "manifest.json").read_text())
    raw["session"][key] = value
    with pytest.raises(ValueError):
        Manifest(raw)


@pytest.mark.parametrize(
    ("remove", "project"),
    [
        (("session", "no_new_exposure_before"), "entry_window"),
        (("risk_caps", "at_risk_cap_fraction"), "risk_limits"),
        (("strategies", "catalyst", "max_loss_per_trade_fraction"), "engine_loss"),
        (("strategies", "event_macro", "take_profit_fraction"), "exit_intent"),
        (("strategies", "event_macro", "stop_loss_fraction"), "exit_intent"),
    ],
)
def test_semantic_policy_facts_refuse_missing_required_declarations(
        remove, project) -> None:
    raw = json.loads((ROOT / "policy" / "manifest.json").read_text())
    node = raw
    for key in remove[:-1]:
        node = node[key]
    node.pop(remove[-1])
    with pytest.raises(KeyError):
        Manifest(raw)
