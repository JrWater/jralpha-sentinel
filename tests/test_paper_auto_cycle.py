from datetime import datetime, timezone

from scripts.test_paper_auto_cycle import (
    DEFAULT_MAX_RISK,
    ROOT,
    _one_marketable_credit_candidate,
    _test_paths,
    build_test_manifest,
)
from strategy.proposal import OptionLeg, Proposal


class _Candidate:
    def __init__(self, proposal):
        self.proposal = proposal


def _credit_candidate(*, short_bid=0.50, long_ask=0.20, width=5.0):
    expiry = datetime(2026, 8, 28, tzinfo=timezone.utc).date()
    return _Candidate(Proposal(
        engine="trend_income", underlying="NVDA", direction="long",
        structure="credit_vertical", expiry=expiry,
        legs=[
            OptionLeg("NVDA-S", "sell", 1, 220.0, "put", expiry,
                      ref_bid=short_bid, ref_ask=short_bid + 0.05),
            OptionLeg("NVDA-L", "buy", 1, 220.0 - width, "put", expiry,
                      ref_bid=max(0.01, long_ask - 0.05), ref_ask=long_ask),
        ],
        limit_price=-0.30, max_loss_dollars=470.0,
    ))


def _raw_policy():
    return {
        "policy_id": "TEST",
        "version": "1",
        "schema_version": 1,
        "environment": {
            "competition_account_id": "PRODUCTION",
            "required_starting_equity": 100000.0,
        },
        "session": {"competition_starts_utc": "2099-01-01T00:00:00+00:00"},
        "agent": {"max_proposals_per_cycle": 3},
        "risk_caps": {
            "max_loss_per_position_fraction": 0.12,
            "at_risk_cap_fraction": 0.40,
            "daily_new_exposure_cap_fraction": 0.30,
        },
        "strategies": {
            "trend_income": {"max_loss_per_trade_fraction": 0.02},
            "event_macro": {
                "max_loss_per_trade_fraction": 0.10,
                "gap_max_loss_fraction": 0.08,
            },
        },
        "order_shapes": [],
        "universe": {"core": [], "satellite": []},
    }


def test_test_manifest_isolated_and_capped_to_one_proposal():
    raw = _raw_policy()
    now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)

    manifest = build_test_manifest(
        raw, account_id="LEGACY", now_utc=now,
        max_risk_dollars=DEFAULT_MAX_RISK)

    assert raw["environment"]["competition_account_id"] == "PRODUCTION"
    assert manifest.get("environment", "competition_account_id") == "LEGACY"
    assert manifest.competition_requires_options_component is False
    assert manifest.get("agent", "max_proposals_per_cycle") == 1
    assert manifest.get("risk_caps", "at_risk_cap_fraction") == 0.005
    assert manifest.get(
        "strategies", "trend_income", "max_loss_per_trade_fraction") == 0.005
    assert manifest.get(
        "strategies", "event_macro", "gap_max_loss_fraction") == 0.005


def test_test_state_paths_never_overlap_production_state():
    paths = _test_paths("RUN")
    assert paths["base"].is_relative_to("/private/tmp")
    assert paths["ledger"] != ROOT / "state" / "ledger.json"
    assert paths["decisions"] != ROOT / "state" / "decisions.jsonl"
    assert paths["wal"] != ROOT / "state" / "submission_wal.jsonl"
    assert paths["lock"] != ROOT / "state" / "cycle.lock"


def test_fill_mode_uses_one_engine_candidate_and_stays_inside_cap():
    candidate = _credit_candidate()

    selected = _one_marketable_credit_candidate([candidate], 500.0)

    assert selected == [candidate]
    assert candidate.proposal.limit_price == -0.01
    assert candidate.proposal.max_loss_dollars == 499.0


def test_fill_mode_refuses_when_safe_credit_is_not_marketable():
    import pytest

    candidate = _credit_candidate(short_bid=0.10, long_ask=0.20, width=10.0)
    with pytest.raises(RuntimeError, match="no engine-produced"):
        _one_marketable_credit_candidate([candidate], 500.0)
