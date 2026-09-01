#!/usr/bin/env python3
"""The contract between what record_group() writes and what later cycles read.

state/positions_meta.json is the only memory the agent has across cycles: cron
starts a fresh process every 30 minutes, so anything a guard needs to know
about an earlier entry has to have been written into a group record. A guard
that filters on a field record_group() never stores is not a strict guard —
it is an absent one, and it fails open silently, because a missing key reads
as None rather than raising.

This is the second instance of exactly that shape in this codebase. The first
was the portfolio at-risk cap: fixed_quantity() read PortfolioState
.max_loss_total and nothing ever wrote it. These tests exist so the third one
gets caught here instead of during the competition window.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.ledger import StructureLedger                         # noqa: E402
from policy.loader import load as load_manifest                 # noqa: E402
from strategy.proposal import OptionLeg, Proposal                # noqa: E402


@pytest.fixture
def structures(tmp_path):
    """An isolated structure-ledger adapter, without cycle global patches."""
    return StructureLedger(tmp_path / "positions_meta.json")


def _gap_proposal() -> Proposal:
    """Shaped exactly like what _nfp_gap_play() produces: the 0-DTE SPY
    single-leg continuation, engine event_macro."""
    return Proposal(
        engine="event_macro", underlying="SPY", direction="long",
        structure="single_long", expiry=date(2026, 9, 4), dte=0,
        legs=[OptionLeg(symbol="SPY260904C00770000", side="buy", quantity=4,
                        strike=770.0, contract_type="call",
                        expiration=date(2026, 9, 4), ref_bid=1.0, ref_ask=1.2)],
        limit_price=1.10, max_loss_dollars=440.0,
        thesis="Event Vector: NFP gap continuation")


def test_group_record_identifies_a_gap_entry(structures):
    """v3.1.1 caps the gap continuation at gap_max_entries_total per WINDOW,
    and counts prior entries out of the group meta with

        g["engine"] == "event_macro" and g["structure"] == "single_long"

    Cron gives each cycle a fresh process, so that count is the only thing
    standing between a two-entry rule and an unbounded one. If structure is
    not written, every comparison is None == "single_long" -> the tally is
    always 0, the cap never trips, and nothing anywhere reports a problem.
    """
    structures.record_entry(_gap_proposal(), "093500")

    groups = json.loads(structures.path.read_text())["groups"]
    assert len(groups) == 1
    group = next(iter(groups.values()))

    assert group.get("engine") == "event_macro"
    assert group.get("structure") == "single_long", (
        "record_group() must persist the structure the window-cap filters on; "
        f"it wrote {sorted(group)}")


def test_lifecycle_protected_orders_include_option_and_residual_closes(structures):
    """Cron cleanup cannot cancel either pending lifecycle-owned close."""
    structures.save({"groups": {
        "option": {"close_pending": True, "close_order_id": "option-close"},
        "residual": {
            "residual_equity_close_pending": True,
            "residual_equity_close_order_id": "residual-close",
        },
        "inactive": {"close_pending": False, "close_order_id": "ignore"},
    }})

    assert structures.protected_open_order_ids() == frozenset({
        "option-close", "residual-close"})


def test_pending_entry_persists_its_pre_expiry_stock_baseline(structures):
    """Late same-day entries retain the stock fact needed for expiry recovery."""
    structures.record_pending_entry(
        _gap_proposal(), "153100", "entry-order-1",
        pre_expiry_underlying_qty=-100)

    pending = json.loads(structures.path.read_text())["pending_entries"]
    assert pending["entry-order-1"]["group"]["pre_expiry_underlying_qty"] == -100


def test_pending_entry_client_ids_are_protected_from_cycle_cleanup(structures):
    """A broker-accepted entry remains owned until broker reconciliation."""
    structures.record_pending_entry(_gap_proposal(), "093500", "entry-order-1")

    assert structures.pending_entry_client_order_ids() == frozenset({
        "entry-order-1"})


def test_the_window_cap_predicate_actually_tallies(structures):
    """The guard's own expression, run against two recorded gap entries.

    Asserting on the predicate rather than on record_group's field list is
    what makes this a behaviour test: it fails if either side of the contract
    moves, which is the failure this file is here to catch.
    """
    manifest = load_manifest()
    gap_max = int(manifest.get("strategies", "event_macro",
                               "gap_max_entries_total", default=2))

    for i, stamp in enumerate(("093500", "094500"), start=1):
        structures.record_entry(_gap_proposal(), stamp)
        meta = json.loads(structures.path.read_text())
        tally = sum(1 for g in meta.get("groups", {}).values()
                    if g.get("engine") == "event_macro"
                    and g.get("structure") == "single_long")
        assert tally == i, f"after {i} gap entries the tally read {tally}"

    assert tally >= gap_max, (
        f"{gap_max} entries recorded but the guard would still let another "
        f"through (tally {tally} < cap {gap_max})")


def test_a_non_gap_entry_does_not_consume_the_gap_budget(structures):
    """The NFP strangle is also engine event_macro. It is a different trade
    and must not spend the gap continuation's two entries — which is exactly
    why the predicate tests structure and not engine alone."""
    strangle = Proposal(
        engine="event_macro", underlying="SPY", direction="neutral",
        structure="strangle", expiry=date(2026, 9, 4), dte=1,
        legs=[OptionLeg("SPY260904C00775000", "buy", 1, 775.0, "call",
                        date(2026, 9, 4), 1.0, 1.2),
              OptionLeg("SPY260904P00760000", "buy", 1, 760.0, "put",
                        date(2026, 9, 4), 1.0, 1.2)],
        limit_price=2.2, max_loss_dollars=220.0, thesis="NFP strangle")
    structures.record_entry(strangle, "103500")

    meta = json.loads(structures.path.read_text())
    tally = sum(1 for g in meta.get("groups", {}).values()
                if g.get("engine") == "event_macro"
                and g.get("structure") == "single_long")
    assert tally == 0, "the strangle consumed a gap entry it does not own"


def test_accepted_entry_is_pending_not_an_open_structure(structures):
    """A broker acceptance is not a fill and must not consume position caps."""
    proposal = _gap_proposal()
    structures.record_pending_entry(proposal, "093500", "entry-order-1")

    result = structures.reconcile_pending_entries(
        lambda order_id: SimpleNamespace(status="accepted", filled_qty="0"))

    meta = structures.load()
    assert result.activated == ()
    assert meta["groups"] == {}
    assert meta["pending_entries"]["entry-order-1"]["reconciliation_required"] is False


def test_full_entry_fill_activates_the_structure_with_its_own_order_id(structures):
    """Only an exact fill promotes a pending entry into the exit lifecycle."""
    proposal = _gap_proposal()
    structures.record_pending_entry(proposal, "093500", "entry-order-1")

    result = structures.reconcile_pending_entries(
        lambda order_id: SimpleNamespace(status="filled", filled_qty="4"))

    meta = structures.load()
    assert result.activated == (
        "event_macro:SPY:2026-09-04:093500@entry-order-1",)
    assert meta["pending_entries"] == {}
    group = meta["groups"][result.activated[0]]
    assert group["entry_order_id"] == "entry-order-1"
    assert group["entry_filled_qty"] == 4


def test_same_cycle_same_structure_entries_get_distinct_group_ids(structures):
    """Two broker attempts may share a timestamp but must never overwrite."""
    proposal = _gap_proposal()
    structures.record_pending_entry(proposal, "093500", "client-order-a")
    structures.record_pending_entry(proposal, "093500", "client-order-b")

    result = structures.reconcile_pending_entries(
        lambda _client_id: SimpleNamespace(status="filled", filled_qty="4",
                                           id="broker-order"))

    meta = structures.load()
    assert len(result.activated) == 2
    assert len(set(result.activated)) == 2
    assert len(meta["groups"]) == 2


def test_pending_entry_is_durable_before_the_broker_can_be_called(structures):
    """The pre-dispatch record closes the broker-acceptance crash window."""
    proposal = _gap_proposal()
    structures.record_pending_entry(proposal, "093500", "client-order-a")

    meta = structures.load()
    assert meta["groups"] == {}
    assert "client-order-a" in meta["pending_entries"]


def test_zero_fill_cancel_is_audited_without_creating_a_ghost_group(structures):
    """A cancelled entry must not survive as a false open position."""
    structures.record_pending_entry(_gap_proposal(), "093500", "entry-order-1")

    result = structures.reconcile_pending_entries(
        lambda order_id: SimpleNamespace(status="canceled", filled_qty="0"))

    meta = structures.load()
    assert result.discarded == ("entry-order-1",)
    assert meta["pending_entries"] == {}
    assert meta["groups"] == {}
    assert meta["entry_outcomes"]["entry-order-1"]["code"] == "ENTRY_NOT_FILLED"


def test_explicit_alpaca_order_not_found_discards_pending_entry(structures):
    """A typed 404 proves this pre-dispatch record did not become exposure."""
    structures.record_pending_entry(_gap_proposal(), "093500", "entry-order-1")

    class OrderNotFound(Exception):
        def __str__(self):
            return ('{"code":40410000,"message":'
                    '"order not found for entry-order-1"}')

    def lookup(order_id: str):
        assert order_id == "entry-order-1"
        raise OrderNotFound()

    result = structures.reconcile_pending_entries(lookup)

    meta = structures.load()
    assert result.discarded == ("entry-order-1",)
    assert meta["pending_entries"] == {}
    assert meta["entry_outcomes"]["entry-order-1"]["code"] == (
        "ENTRY_NOT_SUBMITTED")


def test_partial_entry_fill_remains_quarantined_for_reconciliation(structures):
    """A partial entry is real exposure, not a removable ghost record."""
    structures.record_pending_entry(_gap_proposal(), "093500", "entry-order-1")

    result = structures.reconcile_pending_entries(
        lambda order_id: SimpleNamespace(status="filled", filled_qty="3"))

    meta = structures.load()
    assert result.quarantined == ("entry-order-1",)
    pending = meta["pending_entries"]["entry-order-1"]
    assert pending["reconciliation_required"] is True
    assert pending["reconciliation_detail"] == "entry_filled_3_of_4"
