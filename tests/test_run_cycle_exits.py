"""Integration tests for the public structure-exit cycle."""
from __future__ import annotations

import copy
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

from agent.executor import Executor
from agent.ledger import StructureLedger
from policy.loader import Manifest, load as load_manifest
from scripts import run_cycle
from strategy.data import ChainContract, MarketState


class PaperBrokerBoundary:
    """In-memory stand-in for Alpaca's paper TradingClient boundary."""

    _sandbox = True

    def __init__(self, account_number: str):
        self.account_number = account_number
        self.submitted = []

    def get_account(self):
        return SimpleNamespace(account_number=self.account_number)

    def submit_order(self, request):
        self.submitted.append(request)
        return SimpleNamespace(
            id=f"paper-order-{len(self.submitted)}", status="accepted")


def test_registered_structure_exit_is_not_resubmitted_as_orphan(
        tmp_path, monkeypatch):
    """One registered spread produces exactly one broker close order.

    This is the public behavior observed by the broker and the competition
    audit trail.  A structure remains registered for the whole invocation,
    even after its close order has been accepted; its individual legs must
    not be reclassified as orphans against the same position snapshot.
    """
    raw = copy.deepcopy(load_manifest()._raw)
    raw["environment"]["competition_account_id"] = "PAPER-TEST"
    raw["session"]["competition_starts_utc"] = "2020-01-01T00:00:00+00:00"
    manifest = Manifest(raw)

    short_symbol = "NVDA260904P00222500"
    long_symbol = "NVDA260904P00217500"
    meta_path = tmp_path / "positions_meta.json"
    meta_path.write_text(json.dumps({"groups": {
        "trend_income:NVDA:2026-09-04:100000": {
            "closed": False,
            "engine": "trend_income",
            "underlying": "NVDA",
            "expiry": "2026-09-04",
            "kind": "credit",
            "entry_net": -0.64,
            "ref_amount": 0.64,
            "take_profit_fraction": 0.5,
            "stop_loss_fraction": 2.0,
            "legs": {
                short_symbol: {"side": "sell", "qty": 1},
                long_symbol: {"side": "buy", "qty": 1},
            },
        }
    }}))
    recorded = []
    monkeypatch.setattr(run_cycle, "append_decision", recorded.append)
    monkeypatch.setattr("agent.executor.append_decision", recorded.append)

    state = MarketState(
        equity=100000.0,
        now_utc=datetime(2026, 9, 4, 15, 0, tzinfo=timezone.utc),
        positions=[
            SimpleNamespace(symbol=short_symbol, qty="-1", current_price="0.85"),
            SimpleNamespace(symbol=long_symbol, qty="1", current_price="0.20"),
        ],
    )
    state.chains["NVDA"] = [
        ChainContract(short_symbol, date(2026, 9, 4), "put", 222.5,
                      0.80, 0.85, -0.40, 0.30, None),
        ChainContract(long_symbol, date(2026, 9, 4), "put", 217.5,
                      0.20, 0.25, -0.20, 0.30, None),
    ]
    broker = PaperBrokerBoundary("PAPER-TEST")

    closed = run_cycle.manage_exits(
        state, manifest, Executor(broker, manifest, verbose=False),
        structures=StructureLedger(meta_path))

    assert closed == 1
    assert len(broker.submitted) == 1
    assert len(broker.submitted[0].legs) == 2
    persisted = json.loads(meta_path.read_text())["groups"][
        "trend_income:NVDA:2026-09-04:100000"]
    assert persisted["closed"] is False
    assert persisted["close_pending"] is True
    assert persisted["close_order_id"] == "paper-order-1"
    assert [row["kind"] for row in recorded] == [
        "order_submitted", "structure_close_submitted"]

    # A later snapshot with residual legs retries the registered structure as
    # one structure; it still must not degrade into two orphan orders.
    assert run_cycle.manage_exits(
        state, manifest, Executor(broker, manifest, verbose=False),
        structures=StructureLedger(meta_path)) == 1
    assert len(broker.submitted) == 2
    assert all(len(request.legs) == 2 for request in broker.submitted)

    # If one leg filled first, the remaining registered leg is reduced with
    # one close request.  It is still owned by the group, never by orphan
    # classification, and no already-filled leg is resubmitted.
    state.positions = [state.positions[0]]
    assert run_cycle.manage_exits(
        state, manifest, Executor(broker, manifest, verbose=False),
        structures=StructureLedger(meta_path)) == 1
    assert len(broker.submitted) == 3
    assert broker.submitted[-1].symbol == short_symbol

    # Only broker-confirmed absence of every leg completes the audit state.
    state.positions = []
    assert run_cycle.manage_exits(
        state, manifest, Executor(broker, manifest, verbose=False),
        structures=StructureLedger(meta_path)) == 0
    confirmed = json.loads(meta_path.read_text())["groups"][
        "trend_income:NVDA:2026-09-04:100000"]
    assert confirmed["closed"] is True
    assert confirmed["close_pending"] is False
    assert recorded[-1]["kind"] == "structure_close_confirmed"


def test_position_without_any_active_group_is_closed_as_an_orphan(
        tmp_path, monkeypatch):
    """The duplicate guard must not disable the defensive orphan exit."""
    raw = copy.deepcopy(load_manifest()._raw)
    raw["environment"]["competition_account_id"] = "PAPER-TEST"
    raw["session"]["competition_starts_utc"] = "2020-01-01T00:00:00+00:00"
    manifest = Manifest(raw)
    symbol = "NVDA260904C00225000"
    meta_path = tmp_path / "positions_meta.json"
    meta_path.write_text('{"groups": {}}')
    recorded = []
    monkeypatch.setattr(run_cycle, "append_decision", recorded.append)
    monkeypatch.setattr("agent.executor.append_decision", recorded.append)
    state = MarketState(
        equity=100000.0,
        now_utc=datetime(2026, 9, 4, 15, 0, tzinfo=timezone.utc),
        positions=[SimpleNamespace(
            symbol=symbol, qty="1", current_price="0.50")],
    )
    state.chains["NVDA"] = [
        ChainContract(symbol, date(2026, 9, 4), "call", 225.0,
                      0.45, 0.50, 0.40, 0.30, None),
    ]
    broker = PaperBrokerBoundary("PAPER-TEST")

    closed = run_cycle.manage_exits(
        state, manifest, Executor(broker, manifest, verbose=False),
        structures=StructureLedger(meta_path))

    assert closed == 1
    assert len(broker.submitted) == 1
    assert getattr(broker.submitted[0], "symbol") == symbol
    assert recorded[-1]["kind"] == "orphan_closed"
