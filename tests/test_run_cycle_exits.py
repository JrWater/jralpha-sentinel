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
        self.close_orders = {}

    def get_account(self):
        return SimpleNamespace(account_number=self.account_number)

    def submit_order(self, request):
        self.submitted.append(request)
        return SimpleNamespace(
            id=f"paper-order-{len(self.submitted)}", status="accepted")

    def get_order_by_id(self, order_id):
        return self.close_orders.get(
            order_id, SimpleNamespace(status="new", filled_qty="0"))


class FillingPaperBroker(PaperBrokerBoundary):
    """Paper boundary that fills accepted closes into one net position book."""

    def __init__(self, account_number: str, positions: dict[str, int]):
        super().__init__(account_number)
        self.positions = dict(positions)
        self.requests = {}

    def submit_order(self, request):
        order = super().submit_order(request)
        self.requests[str(order.id)] = request
        self.close_orders[str(order.id)] = SimpleNamespace(
            status="accepted", filled_qty="0")
        return order

    def fill_accepted_closes(self) -> None:
        for order_id, request in self.requests.items():
            quantity = int(request.qty)
            if hasattr(request, "legs") and request.legs:
                legs = request.legs
            else:
                legs = [request]
            for leg in legs:
                side = str(leg.side).upper()
                delta = -quantity if side.endswith("SELL") else quantity
                self.positions[leg.symbol] = self.positions.get(leg.symbol, 0) + delta
            self.close_orders[order_id] = SimpleNamespace(
                status="filled", filled_qty=str(quantity))
        self.positions = {symbol: quantity for symbol, quantity in self.positions.items()
                          if quantity}

    def snapshot_positions(self, prices: dict[str, float]):
        return [SimpleNamespace(symbol=symbol, qty=str(quantity),
                                current_price=str(prices[symbol]))
                for symbol, quantity in self.positions.items()]


def test_expired_absent_structure_is_closed_with_an_audit_record(tmp_path):
    """A vanished, expired option structure can reach an explicit terminal state."""
    raw = copy.deepcopy(load_manifest()._raw)
    raw["environment"]["competition_account_id"] = "PAPER-TEST"
    manifest = Manifest(raw)
    meta_path = tmp_path / "positions_meta.json"
    meta_path.write_text(json.dumps({"groups": {
        "trend_directional:TSLA:2026-08-31:190503": {
            "closed": False,
            "engine": "trend_directional",
            "underlying": "TSLA",
            "expiry": "2026-08-31",
            "kind": "debit",
            "legs": {
                "TSLA260831C00367500": {"side": "buy", "qty": 15},
                "TSLA260831C00372500": {"side": "sell", "qty": 15},
            },
        },
    }}))
    recorded = []
    state = MarketState(
        now_utc=datetime(2026, 9, 1, 14, tzinfo=timezone.utc), positions=[])

    assert run_cycle.manage_exits(
        state, manifest, PaperBrokerBoundary("PAPER-TEST"),
        structures=StructureLedger(meta_path), record_decision=recorded.append) == 0

    group = json.loads(meta_path.read_text())["groups"][
        "trend_directional:TSLA:2026-08-31:190503"]
    assert group["closed"] is True
    assert group["terminal_outcome"] == "ENTRY_EXPIRED"
    assert group["terminal_reconciliation"] == {
        "broker_option_legs_observed": [],
        "broker_underlying_positions_observed": [],
        "declared_option_legs": [
            "TSLA260831C00367500", "TSLA260831C00372500",
        ],
        "reconciled_at_utc": "2026-09-01T14:00:00+00:00",
    }
    assert recorded == [{
        "kind": "structure_expired_absent",
        "group": "trend_directional:TSLA:2026-08-31:190503",
        "expiry": "2026-08-31",
        "outcome": "ENTRY_EXPIRED",
    }]


def test_expired_absent_structure_stays_quarantined_when_stock_remains(tmp_path):
    """Option expiry must not erase shares delivered from that structure."""
    raw = copy.deepcopy(load_manifest()._raw)
    raw["environment"]["competition_account_id"] = "PAPER-TEST"
    manifest = Manifest(raw)
    meta_path = tmp_path / "positions_meta.json"
    meta_path.write_text(json.dumps({"groups": {
        "trend_directional:TSLA:2026-08-31:190503": {
            "closed": False,
            "engine": "trend_directional",
            "underlying": "TSLA",
            "expiry": "2026-08-31",
            "kind": "debit",
            "legs": {
                "TSLA260831C00367500": {"side": "buy", "qty": 15},
                "TSLA260831C00372500": {"side": "sell", "qty": 15},
            },
        },
    }}))
    recorded = []
    state = MarketState(
        now_utc=datetime(2026, 9, 1, 14, tzinfo=timezone.utc), positions=[],
        non_option_positions=[SimpleNamespace(symbol="TSLA", qty="500")])

    assert run_cycle.manage_exits(
        state, manifest, PaperBrokerBoundary("PAPER-TEST"),
        structures=StructureLedger(meta_path), record_decision=recorded.append) == 0

    group = json.loads(meta_path.read_text())["groups"][
        "trend_directional:TSLA:2026-08-31:190503"]
    assert group["closed"] is False
    assert group["reconciliation_required"] is True
    assert group["reconciliation_detail"] == "underlying_exposure_after_expiry"
    assert recorded == []


def test_expired_structure_with_missing_underlying_identity_stays_quarantined(tmp_path):
    """A malformed ledger record cannot hide stock delivered at expiry."""
    raw = copy.deepcopy(load_manifest()._raw)
    raw["environment"]["competition_account_id"] = "PAPER-TEST"
    manifest = Manifest(raw)
    meta_path = tmp_path / "positions_meta.json"
    meta_path.write_text(json.dumps({"groups": {
        "legacy-tsla": {
            "closed": False,
            "expiry": "2026-08-31",
            "legs": {
                "TSLA260831C00367500": {"side": "buy", "qty": 15},
                "TSLA260831C00372500": {"side": "sell", "qty": 15},
            },
        },
    }}))
    state = MarketState(
        now_utc=datetime(2026, 9, 1, 14, tzinfo=timezone.utc), positions=[],
        non_option_positions=[SimpleNamespace(symbol="TSLA", qty="500")])

    assert run_cycle.manage_exits(
        state, manifest, PaperBrokerBoundary("PAPER-TEST"),
        structures=StructureLedger(meta_path), record_decision=lambda _: None) == 0

    group = json.loads(meta_path.read_text())["groups"]["legacy-tsla"]
    assert group["closed"] is False
    assert group["reconciliation_required"] is True
    assert group["reconciliation_detail"] == "expired_structure_underlying_unresolved"


def test_expired_structure_with_an_unparseable_leg_stays_quarantined(tmp_path):
    """Every declared leg must be identifiable before an expiry terminal state."""
    raw = copy.deepcopy(load_manifest()._raw)
    raw["environment"]["competition_account_id"] = "PAPER-TEST"
    manifest = Manifest(raw)
    meta_path = tmp_path / "positions_meta.json"
    meta_path.write_text(json.dumps({"groups": {
        "corrupt-tsla": {
            "closed": False,
            "underlying": "TSLA",
            "expiry": "2026-08-31",
            "legs": {
                "TSLA260831C00367500": {"side": "buy", "qty": 15},
                "not-an-occ-symbol": {"side": "sell", "qty": 15},
            },
        },
    }}))
    state = MarketState(
        now_utc=datetime(2026, 9, 1, 14, tzinfo=timezone.utc), positions=[])

    assert run_cycle.manage_exits(
        state, manifest, PaperBrokerBoundary("PAPER-TEST"),
        structures=StructureLedger(meta_path), record_decision=lambda _: None) == 0

    group = json.loads(meta_path.read_text())["groups"]["corrupt-tsla"]
    assert group["closed"] is False
    assert group["reconciliation_required"] is True
    assert group["reconciliation_detail"] == "expired_structure_underlying_unresolved"


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

    # A pending close is the structure's authority. A later snapshot must not
    # submit a duplicate close merely because the broker still shows its legs.
    assert run_cycle.manage_exits(
        state, manifest, Executor(broker, manifest, verbose=False),
        structures=StructureLedger(meta_path)) == 0
    assert len(broker.submitted) == 1

    # A filled close of the structure's own expected quantity completes it.
    broker.close_orders["paper-order-1"] = SimpleNamespace(
        status="filled", filled_qty="1")
    assert run_cycle.manage_exits(
        state, manifest, Executor(broker, manifest, verbose=False),
        structures=StructureLedger(meta_path)) == 0
    confirmed = json.loads(meta_path.read_text())["groups"][
        "trend_income:NVDA:2026-09-04:100000"]
    assert confirmed["closed"] is True
    assert confirmed["close_pending"] is False
    assert recorded[-1]["kind"] == "structure_close_confirmed"


def test_shared_contract_groups_close_independently_by_group_quantity(
        tmp_path, monkeypatch):
    """A filled spread close cannot consume a same-symbol single-long exit."""
    raw = copy.deepcopy(load_manifest()._raw)
    raw["environment"]["competition_account_id"] = "PAPER-TEST"
    raw["session"]["competition_starts_utc"] = "2020-01-01T00:00:00+00:00"
    manifest = Manifest(raw)

    symbol = "NVDA260904C00225000"
    other = "NVDA260904C00230000"
    meta_path = tmp_path / "positions_meta.json"
    meta_path.write_text(json.dumps({"groups": {
        "trend_directional:NVDA:2026-09-04:153002": {
            "closed": False, "close_pending": True,
            "close_order_id": "spread-close", "engine": "trend_directional",
            "underlying": "NVDA", "expiry": "2026-09-04", "kind": "debit",
            "entry_net": 0.89, "ref_amount": 0.89,
            "take_profit_fraction": 0.4, "stop_loss_fraction": 0.5,
            "legs": {symbol: {"side": "buy", "qty": 22},
                     other: {"side": "sell", "qty": 22}},
        },
        "trend_single:NVDA:2026-09-04:153002": {
            "closed": False, "engine": "trend_single", "underlying": "NVDA",
            "expiry": "2026-09-04", "kind": "debit", "entry_net": 0.95,
            "ref_amount": 0.95, "take_profit_fraction": 1.0,
            "stop_loss_fraction": 0.5,
            "legs": {symbol: {"side": "buy", "qty": 31}},
        },
    }}))
    recorded = []
    monkeypatch.setattr(run_cycle, "append_decision", recorded.append)
    monkeypatch.setattr("agent.executor.append_decision", recorded.append)
    state = MarketState(
        equity=100000.0,
        now_utc=datetime(2026, 9, 4, 20, 30, tzinfo=timezone.utc),
        positions=[SimpleNamespace(symbol=symbol, qty="31", current_price="0.14")],
    )
    state.chains["NVDA"] = [
        ChainContract(symbol, date(2026, 9, 4), "call", 225.0,
                      0.14, 0.15, 0.20, 0.30, None),
    ]
    broker = PaperBrokerBoundary("PAPER-TEST")
    broker.close_orders["spread-close"] = SimpleNamespace(
        status="filled", filled_qty="22")

    closed = run_cycle.manage_exits(
        state, manifest, Executor(broker, manifest, verbose=False),
        structures=StructureLedger(meta_path))

    assert closed == 1
    assert len(broker.submitted) == 1
    request = broker.submitted[0]
    assert request.symbol == symbol
    assert request.qty == 31
    assert request.limit_price == 0.14
    groups = json.loads(meta_path.read_text())["groups"]
    assert groups["trend_directional:NVDA:2026-09-04:153002"]["closed"] is True
    assert groups["trend_single:NVDA:2026-09-04:153002"]["close_pending"] is True

    # On the next broker snapshot the single's *own* close order, not the
    # absence/presence of a shared symbol, completes its lifecycle.  This is
    # the end-to-end ownership contract: both records close and the broker
    # has no net contract left.
    broker.close_orders["paper-order-1"] = SimpleNamespace(
        status="filled", filled_qty="31")
    state.positions = []
    assert run_cycle.manage_exits(
        state, manifest, Executor(broker, manifest, verbose=False),
        structures=StructureLedger(meta_path)) == 0
    groups = json.loads(meta_path.read_text())["groups"]
    assert groups["trend_directional:NVDA:2026-09-04:153002"]["closed"] is True
    assert groups["trend_single:NVDA:2026-09-04:153002"]["closed"] is True


def test_shared_open_contract_cannot_be_closed_for_more_than_broker_net(
        tmp_path, monkeypatch):
    """Two ledger claims for 53 calls cannot sell a 31-call broker net."""
    raw = copy.deepcopy(load_manifest()._raw)
    raw["environment"]["competition_account_id"] = "PAPER-TEST"
    raw["session"]["competition_starts_utc"] = "2020-01-01T00:00:00+00:00"
    manifest = Manifest(raw)
    symbol = "NVDA260904C00225000"
    meta_path = tmp_path / "positions_meta.json"
    meta_path.write_text(json.dumps({"groups": {
        "spread": {
            "closed": False, "engine": "trend_directional",
            "underlying": "NVDA", "expiry": "2026-09-04", "kind": "debit",
            "entry_net": 0.89, "ref_amount": 0.89,
            "take_profit_fraction": 0.4, "stop_loss_fraction": 0.5,
            "legs": {symbol: {"side": "buy", "qty": 22}},
        },
        "single": {
            "closed": False, "engine": "trend_single", "underlying": "NVDA",
            "expiry": "2026-09-04", "kind": "debit", "entry_net": 0.95,
            "ref_amount": 0.95, "take_profit_fraction": 1.0,
            "stop_loss_fraction": 0.5,
            "legs": {symbol: {"side": "buy", "qty": 31}},
        },
    }}))
    monkeypatch.setattr(run_cycle, "append_decision", lambda _: None)
    monkeypatch.setattr("agent.executor.append_decision", lambda _: None)
    state = MarketState(
        equity=100000.0,
        now_utc=datetime(2026, 9, 4, 20, 30, tzinfo=timezone.utc),
        positions=[SimpleNamespace(symbol=symbol, qty="31", current_price="0.14")],
    )
    broker = PaperBrokerBoundary("PAPER-TEST")

    assert run_cycle.manage_exits(
        state, manifest, Executor(broker, manifest, verbose=False),
        structures=StructureLedger(meta_path)) == 0
    assert broker.submitted == []
    groups = json.loads(meta_path.read_text())["groups"]
    assert all(group["reconciliation_required"] for group in groups.values())


def test_shared_open_contract_groups_close_to_a_zero_broker_net(
        tmp_path, monkeypatch):
    """Two owned close orders consume their 53-call/22-call broker book."""
    raw = copy.deepcopy(load_manifest()._raw)
    raw["environment"]["competition_account_id"] = "PAPER-TEST"
    raw["session"]["competition_starts_utc"] = "2020-01-01T00:00:00+00:00"
    manifest = Manifest(raw)
    call = "NVDA260904C00225000"
    short_call = "NVDA260904C00230000"
    meta_path = tmp_path / "positions_meta.json"
    meta_path.write_text(json.dumps({"groups": {
        "spread": {
            "closed": False, "engine": "trend_directional",
            "underlying": "NVDA", "expiry": "2026-09-04", "kind": "debit",
            "entry_net": 0.89, "ref_amount": 0.89,
            "take_profit_fraction": 0.4, "stop_loss_fraction": 0.5,
            "legs": {call: {"side": "buy", "qty": 22},
                     short_call: {"side": "sell", "qty": 22}},
        },
        "single": {
            "closed": False, "engine": "trend_single", "underlying": "NVDA",
            "expiry": "2026-09-04", "kind": "debit", "entry_net": 0.95,
            "ref_amount": 0.95, "take_profit_fraction": 1.0,
            "stop_loss_fraction": 0.5,
            "legs": {call: {"side": "buy", "qty": 31}},
        },
    }}))
    recorded = []
    monkeypatch.setattr(run_cycle, "append_decision", recorded.append)
    monkeypatch.setattr("agent.executor.append_decision", recorded.append)
    prices = {call: 0.14, short_call: 0.04}
    state = MarketState(
        equity=100000.0,
        now_utc=datetime(2026, 9, 4, 20, 30, tzinfo=timezone.utc),
        positions=[SimpleNamespace(symbol=call, qty="53", current_price="0.14"),
                   SimpleNamespace(symbol=short_call, qty="-22", current_price="0.04")],
    )
    state.chains["NVDA"] = [
        ChainContract(call, date(2026, 9, 4), "call", 225.0,
                      0.14, 0.15, 0.20, 0.30, None),
        ChainContract(short_call, date(2026, 9, 4), "call", 230.0,
                      0.04, 0.05, -0.20, 0.30, None),
    ]
    broker = FillingPaperBroker("PAPER-TEST", {call: 53, short_call: -22})

    assert run_cycle.manage_exits(
        state, manifest, Executor(broker, manifest, verbose=False),
        structures=StructureLedger(meta_path)) == 2
    assert sorted(request.qty for request in broker.submitted) == [22, 31]
    assert [row["group"] for row in recorded
            if row["kind"] == "structure_close_submitted"] == ["spread", "single"]

    broker.fill_accepted_closes()
    state.positions = broker.snapshot_positions(prices)
    assert state.positions == []
    assert run_cycle.manage_exits(
        state, manifest, Executor(broker, manifest, verbose=False),
        structures=StructureLedger(meta_path)) == 0
    groups = json.loads(meta_path.read_text())["groups"]
    assert all(group["closed"] for group in groups.values())
    assert {row["group"] for row in recorded
            if row["kind"] == "structure_close_confirmed"} == {"spread", "single"}


def test_partial_close_requires_structure_reconciliation(tmp_path, monkeypatch):
    """A close with the wrong filled quantity blocks automatic retries."""
    raw = copy.deepcopy(load_manifest()._raw)
    raw["environment"]["competition_account_id"] = "PAPER-TEST"
    raw["session"]["competition_starts_utc"] = "2020-01-01T00:00:00+00:00"
    manifest = Manifest(raw)
    symbol = "NVDA260904C00225000"
    meta_path = tmp_path / "positions_meta.json"
    meta_path.write_text(json.dumps({"groups": {
        "trend_single:NVDA:2026-09-04:153002": {
            "closed": False, "close_pending": True,
            "close_order_id": "partial-close", "engine": "trend_single",
            "underlying": "NVDA", "expiry": "2026-09-04", "kind": "debit",
            "entry_net": 0.95, "ref_amount": 0.95,
            "take_profit_fraction": 1.0, "stop_loss_fraction": 0.5,
            "legs": {symbol: {"side": "buy", "qty": 31}},
        },
    }}))
    monkeypatch.setattr(run_cycle, "append_decision", lambda _: None)
    monkeypatch.setattr("agent.executor.append_decision", lambda _: None)
    state = MarketState(
        equity=100000.0,
        now_utc=datetime(2026, 9, 4, 20, 30, tzinfo=timezone.utc),
        positions=[SimpleNamespace(symbol=symbol, qty="31", current_price="0.14")],
    )
    broker = PaperBrokerBoundary("PAPER-TEST")
    broker.close_orders["partial-close"] = SimpleNamespace(
        status="filled", filled_qty="30")

    assert run_cycle.manage_exits(
        state, manifest, Executor(broker, manifest, verbose=False),
        structures=StructureLedger(meta_path)) == 0
    group = json.loads(meta_path.read_text())["groups"][
        "trend_single:NVDA:2026-09-04:153002"]
    assert group["reconciliation_required"] is True
    assert group["closed"] is False

    # The cycle-level evaluator consumes the durable lifecycle result after
    # exits, so the same invocation cannot admit another entry against this
    # unresolved exposure.
    journal_view = SimpleNamespace(
        integrity_ok=True, unresolved_dispatches=())
    results = run_cycle.run_preflight(
        state, manifest, [], journal_view,
        structures=StructureLedger(meta_path))
    assert results["unresolved_structure_closes"].ok is False


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
