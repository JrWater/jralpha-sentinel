#!/usr/bin/env python3
"""Tests for agent/executor.py's authority checkpoint.

Executor.submit() is the ONLY code that calls trading_client.submit_order().
manage_exits() calls it directly, bypassing the pretrade gate loop by design
(exits must survive a red entry gate) — which means _refuse_unless_authorized
is the only thing standing between a close order and the competition account
before kickoff. These tests exist because that checkpoint used to not exist:
submit() would build and send the wire order for ANY session, any account,
any time, as long as the order shape was declared.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.executor import Executor                            # noqa: E402
from policy.loader import load as load_manifest                # noqa: E402
from strategy.proposal import OptionLeg, Proposal               # noqa: E402

UTC = timezone.utc
BEFORE_KICKOFF = datetime(2026, 8, 26, 18, 0, tzinfo=UTC)
AFTER_KICKOFF = datetime(2026, 8, 28, 16, 0, tzinfo=UTC)


class FakeClient:
    """Just enough of TradingClient's surface for Executor to talk to."""

    def __init__(self, *, account_number: str, sandbox: bool = True):
        self._sandbox = sandbox
        self._account_number = account_number
        self.submit_calls: list = []

    def get_account(self):
        return SimpleNamespace(account_number=self._account_number)

    def submit_order(self, request):
        self.submit_calls.append(request)
        return SimpleNamespace(id="fake-order-1", status="accepted")


@pytest.fixture
def manifest():
    return load_manifest()


def _proposal(**kw) -> Proposal:
    base = dict(
        engine="test", underlying="SPY", direction="long",
        structure="single_long", expiry=date(2026, 8, 28), dte=0,
        legs=[OptionLeg(symbol="SPY260828C00600000", side="buy", quantity=1,
                        strike=600.0, contract_type="call",
                        expiration=date(2026, 8, 28), ref_bid=1.0, ref_ask=1.2)],
        limit_price=1.10, max_loss_dollars=110.0, thesis="test",
    )
    base.update(kw)
    return Proposal(**base)


# ── the checkpoint that used to not exist ────────────────────────────────────

def test_refuses_a_non_sandbox_client(manifest):
    """trading_client.paper is not True — the manifest's own promise."""
    declared = manifest.get("environment", "competition_account_id")
    client = FakeClient(account_number=declared, sandbox=False)
    ex = Executor(client, manifest, verbose=False)
    with pytest.raises(RuntimeError, match="PAPER-only"):
        ex.submit(_proposal(), now=AFTER_KICKOFF)
    assert client.submit_calls == []


def test_refuses_an_unnamed_account(manifest):
    from policy.loader import Manifest
    import copy
    raw = copy.deepcopy(manifest._raw)
    raw["environment"]["competition_account_id"] = None
    unnamed = Manifest(raw)
    client = FakeClient(account_number="PA000TEST")
    ex = Executor(client, unnamed, verbose=False)
    with pytest.raises(RuntimeError, match="competition_account_id"):
        ex.submit(_proposal(), now=AFTER_KICKOFF)
    assert client.submit_calls == []


def test_refuses_a_different_account(manifest):
    """Order authority is never carried forward to another account — even
    the legacy dev account, which check_competition_window (a pretrade-only
    gate) would wave through. Executor is stricter on purpose: it is the
    backstop for the one path (exits) that skips the pretrade gates."""
    client = FakeClient(account_number="PA31GLG5O9HU")
    ex = Executor(client, manifest, verbose=False)
    with pytest.raises(RuntimeError, match="PA31GLG5O9HU"):
        ex.submit(_proposal(), now=AFTER_KICKOFF)
    assert client.submit_calls == []


def test_refuses_the_competition_account_before_kickoff(manifest):
    declared = manifest.get("environment", "competition_account_id")
    client = FakeClient(account_number=declared)
    ex = Executor(client, manifest, verbose=False)
    with pytest.raises(RuntimeError, match="pristine"):
        ex.submit(_proposal(), now=BEFORE_KICKOFF)
    assert client.submit_calls == []


def test_a_close_order_gets_no_carve_out(manifest):
    """The exact regression this file exists to prevent: manage_exits()
    calls submit(closing=True) directly, with no gate evaluation upstream.
    A close order on the competition account before kickoff must be refused
    exactly like an open order — there is no closing=True exception."""
    declared = manifest.get("environment", "competition_account_id")
    client = FakeClient(account_number=declared)
    ex = Executor(client, manifest, verbose=False)
    with pytest.raises(RuntimeError, match="pristine"):
        ex.submit(_proposal(), closing=True, now=BEFORE_KICKOFF)
    assert client.submit_calls == []


def test_close_position_by_limits_also_gets_no_carve_out(manifest):
    """Same regression, through the actual method manage_exits() calls."""
    declared = manifest.get("environment", "competition_account_id")
    client = FakeClient(account_number=declared)
    ex = Executor(client, manifest, verbose=False)
    leg = OptionLeg(symbol="SPY260828C00600000", side="sell", quantity=1,
                    strike=600.0, contract_type="call",
                    expiration=date(2026, 8, 28))
    # close_position_by_limits() doesn't take `now`, so this exercises the
    # real (unmocked) clock. Safe today only because "today" is genuinely
    # before kickoff; see the submit()-level tests above for the version
    # that doesn't expire once the competition starts.
    with pytest.raises(RuntimeError, match="pristine|manifest declares"):
        ex.close_position_by_limits([leg], net_limit=-1.10, reason="test exit")
    assert client.submit_calls == []


def test_the_declared_account_after_kickoff_is_allowed(manifest, monkeypatch):
    """The happy path still works once every check clears."""
    recorded = []
    monkeypatch.setattr("agent.executor.append_decision",
                        lambda record: recorded.append(record))
    declared = manifest.get("environment", "competition_account_id")
    client = FakeClient(account_number=declared)
    ex = Executor(client, manifest, verbose=False)
    order = ex.submit(_proposal(), now=AFTER_KICKOFF)
    assert order.id == "fake-order-1"
    assert len(client.submit_calls) == 1
    assert len(recorded) == 1


def test_undeclared_shape_is_still_refused_after_authority_clears(manifest):
    """The new checkpoint runs first, but the old one still runs after."""
    declared = manifest.get("environment", "competition_account_id")
    client = FakeClient(account_number=declared)
    ex = Executor(client, manifest, verbose=False)
    three_legs = [OptionLeg(symbol=f"SPY260828C0060{i}000", side="buy",
                            quantity=1, strike=600.0 + i, contract_type="call",
                            expiration=date(2026, 8, 28))
                 for i in range(3)]
    with pytest.raises(RuntimeError, match="undeclared shape"):
        ex.submit(_proposal(legs=three_legs, limit_price=1.0), now=AFTER_KICKOFF)
    assert client.submit_calls == []
