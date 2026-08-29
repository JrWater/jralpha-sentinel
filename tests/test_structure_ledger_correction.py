"""Regression tests for the one-time pre-fill ledger correction."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.reconcile_structure_ledger import reconcile_meta  # noqa: E402


def _group(*, engine: str, underlying: str, qty: int, structure: str,
           close_order_id: str | None = None) -> dict:
    group = {
        "engine": engine, "underlying": underlying, "structure": structure,
        "closed": False,
        "legs": {"NVDA260828C00225000": {"side": "buy", "qty": qty}},
    }
    if close_order_id:
        group["close_pending"] = True
        group["close_order_id"] = close_order_id
    return group


def test_legacy_entry_without_an_explicit_mapping_is_a_blocker_not_a_guess():
    meta = {"groups": {
        "trend_income:NFLX:2026-08-28:150003": _group(
            engine="trend_income", underlying="NFLX", qty=4,
            structure="credit_vertical"),
    }}
    decisions = [{
        "kind": "order_submitted", "engine": "trend_income",
        "underlying": "NFLX", "structure": "credit_vertical",
        "at_utc": "2026-08-28T15:00:36+00:00", "order_id": "entry-1",
        "client_order_id": "client-1",
    }]
    orders = {"entry-1": {"id": "entry-1", "status": "canceled",
                            "filled_qty": "0"}}

    corrected, report = reconcile_meta(meta, decisions, orders)

    assert corrected == meta
    assert report["changed"] == []
    assert report["blockers"] == [{
        "code": "LEGACY_ENTRY_MAPPING_REQUIRED",
        "group": "trend_income:NFLX:2026-08-28:150003",
    }]
    assert report["mapping_candidates"] == {
        "trend_income:NFLX:2026-08-28:150003": {
            "order_id": "entry-1", "client_order_id": "client-1"}}


def test_explicit_mapping_must_agree_with_decision_and_broker_order():
    meta = {"groups": {
        "trend_income:NFLX:2026-08-28:150003": _group(
            engine="trend_income", underlying="NFLX", qty=4,
            structure="credit_vertical"),
    }}
    decisions = [{
        "kind": "order_submitted", "engine": "trend_income",
        "underlying": "NFLX", "structure": "credit_vertical",
        "at_utc": "2026-08-28T15:00:36+00:00", "order_id": "entry-1",
        "client_order_id": "client-1",
    }]
    orders = {"entry-1": {"id": "entry-1", "client_order_id": "client-1",
                            "symbol": "NVDA260828C00225000", "side": "buy",
                            "qty": "4", "status": "canceled", "filled_qty": "0"}}

    group_id = "trend_income:NFLX:2026-08-28:150003"
    corrected, report = reconcile_meta(
        meta, decisions, orders,
        mappings={group_id: {
            "order_id": "entry-1", "client_order_id": "client-1"}})

    group = corrected["groups"]["trend_income:NFLX:2026-08-28:150003"]
    assert group["closed"] is True
    assert group["terminal_outcome"] == "ENTRY_NOT_FILLED"
    assert report["blockers"] == []


def test_mapping_refuses_a_broker_object_with_a_different_order_id():
    group_id = "trend_income:NFLX:2026-08-28:150003"
    meta = {"groups": {group_id: _group(
        engine="trend_income", underlying="NFLX", qty=4,
        structure="credit_vertical")}}
    decisions = [{
        "kind": "order_submitted", "engine": "trend_income",
        "underlying": "NFLX", "structure": "credit_vertical",
        "at_utc": "2026-08-28T15:00:36+00:00", "order_id": "entry-1",
        "client_order_id": "client-1",
    }]
    orders = {"entry-1": {
        "id": "substituted-order", "client_order_id": "client-1",
        "symbol": "NVDA260828C00225000", "side": "buy", "qty": "4",
        "status": "canceled", "filled_qty": "0"}}

    corrected, report = reconcile_meta(
        meta, decisions, orders,
        mappings={group_id: {"order_id": "entry-1", "client_order_id": "client-1"}})

    assert corrected == meta
    assert report["blockers"] == [{
        "code": "ENTRY_ORDER_MAPPING_MISMATCH", "group": group_id}]


def test_own_filled_close_marks_group_closed_despite_shared_symbol():
    meta = {"groups": {
        "trend_directional:NVDA:2026-08-28:153002": _group(
            engine="trend_directional", underlying="NVDA", qty=22,
            structure="debit_vertical", close_order_id="close-1"),
    }}

    corrected, report = reconcile_meta(
        meta, [], {"close-1": {"id": "close-1", "status": "filled",
                                 "filled_qty": "22"}})

    group = corrected["groups"]["trend_directional:NVDA:2026-08-28:153002"]
    assert group["closed"] is True
    assert group["close_pending"] is False
    assert group["terminal_outcome"] == "CLOSE_FILLED"
    assert report["blockers"] == []


def test_real_full_entry_without_a_close_remains_open():
    meta = {"groups": {
        "trend_single:NVDA:2026-08-28:153002": _group(
            engine="trend_single", underlying="NVDA", qty=31,
            structure="single_long"),
    }}
    decisions = [{
        "kind": "order_submitted", "engine": "trend_single",
        "underlying": "NVDA", "structure": "single_long",
        "at_utc": "2026-08-28T15:30:31+00:00", "order_id": "entry-1",
        "client_order_id": "client-1",
    }]
    orders = {"entry-1": {"id": "entry-1", "client_order_id": "client-1", "symbol": "NVDA260828C00225000", "side": "buy", "qty": "31", "status": "filled",
                            "filled_qty": "31"}}

    corrected, report = reconcile_meta(
        meta, decisions, orders,
        mappings={"trend_single:NVDA:2026-08-28:153002": {
            "order_id": "entry-1", "client_order_id": "client-1"}})

    group = corrected["groups"]["trend_single:NVDA:2026-08-28:153002"]
    assert group["closed"] is False
    assert group["entry_order_id"] == "entry-1"
    assert group["entry_filled_qty"] == 31
    assert report["blockers"] == []


def test_expired_filled_entry_absent_from_broker_is_terminal():
    """An exact historical fill may end through option expiration, not a close."""
    group_id = "trend_single:NVDA:2026-08-28:153002"
    group = _group(engine="trend_single", underlying="NVDA", qty=31,
                   structure="single_long")
    group["expiry"] = "2026-08-28"
    meta = {"groups": {group_id: group}}
    decisions = [{
        "kind": "order_submitted", "engine": "trend_single",
        "underlying": "NVDA", "structure": "single_long",
        "at_utc": "2026-08-28T15:30:31+00:00", "order_id": "entry-1",
        "client_order_id": "client-1",
    }]
    orders = {"entry-1": {
        "id": "entry-1", "client_order_id": "client-1",
        "symbol": "NVDA260828C00225000", "side": "buy", "qty": "31",
        "status": "filled", "filled_qty": "31"}}

    corrected, report = reconcile_meta(
        meta, decisions, orders,
        mappings={group_id: {"order_id": "entry-1", "client_order_id": "client-1"}},
        broker_symbols=set(), as_of=date(2026, 8, 29))

    terminal = corrected["groups"][group_id]
    assert terminal["closed"] is True
    assert terminal["terminal_outcome"] == "ENTRY_EXPIRED"
    assert report["blockers"] == []


def test_ambiguous_entry_mapping_is_a_blocker_not_a_guess():
    meta = {"groups": {
        "trend_income:NFLX:2026-08-28:150003": _group(
            engine="trend_income", underlying="NFLX", qty=4,
            structure="credit_vertical"),
    }}
    decisions = [{
        "kind": "order_submitted", "engine": "trend_income",
        "underlying": "NFLX", "structure": "credit_vertical",
        "at_utc": "2026-08-28T15:00:36+00:00", "order_id": "entry-1",
    }, {
        "kind": "order_submitted", "engine": "trend_income",
        "underlying": "NFLX", "structure": "credit_vertical",
        "at_utc": "2026-08-28T15:00:40+00:00", "order_id": "entry-2",
    }]

    corrected, report = reconcile_meta(
        meta, decisions, {},
        mappings={"trend_income:NFLX:2026-08-28:150003": {
            "order_id": "entry-1", "client_order_id": "client-1"}})

    assert corrected == meta
    assert report["changed"] == []
    assert report["blockers"] == [{
        "code": "ENTRY_ORDER_AMBIGUOUS",
        "group": "trend_income:NFLX:2026-08-28:150003",
    }]
