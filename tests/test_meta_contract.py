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

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.run_cycle as rc                                  # noqa: E402
from policy.loader import load as load_manifest                 # noqa: E402
from strategy.proposal import OptionLeg, Proposal                # noqa: E402


@pytest.fixture
def meta_path(tmp_path, monkeypatch):
    """Redirect the meta file so no test can touch the real ledger."""
    p = tmp_path / "positions_meta.json"
    monkeypatch.setattr(rc, "META_PATH", p)
    return p


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


def test_group_record_identifies_a_gap_entry(meta_path):
    """v3.1.1 caps the gap continuation at gap_max_entries_total per WINDOW,
    and counts prior entries out of the group meta with

        g["engine"] == "event_macro" and g["structure"] == "single_long"

    Cron gives each cycle a fresh process, so that count is the only thing
    standing between a two-entry rule and an unbounded one. If structure is
    not written, every comparison is None == "single_long" -> the tally is
    always 0, the cap never trips, and nothing anywhere reports a problem.
    """
    rc.record_group(_gap_proposal(), "093500")

    groups = json.loads(meta_path.read_text())["groups"]
    assert len(groups) == 1
    group = next(iter(groups.values()))

    assert group.get("engine") == "event_macro"
    assert group.get("structure") == "single_long", (
        "record_group() must persist the structure the window-cap filters on; "
        f"it wrote {sorted(group)}")


def test_the_window_cap_predicate_actually_tallies(meta_path):
    """The guard's own expression, run against two recorded gap entries.

    Asserting on the predicate rather than on record_group's field list is
    what makes this a behaviour test: it fails if either side of the contract
    moves, which is the failure this file is here to catch.
    """
    manifest = load_manifest()
    gap_max = int(manifest.get("strategies", "event_macro",
                               "gap_max_entries_total", default=2))

    for i, stamp in enumerate(("093500", "094500"), start=1):
        rc.record_group(_gap_proposal(), stamp)
        meta = json.loads(meta_path.read_text())
        tally = sum(1 for g in meta.get("groups", {}).values()
                    if g.get("engine") == "event_macro"
                    and g.get("structure") == "single_long")
        assert tally == i, f"after {i} gap entries the tally read {tally}"

    assert tally >= gap_max, (
        f"{gap_max} entries recorded but the guard would still let another "
        f"through (tally {tally} < cap {gap_max})")


def test_a_non_gap_entry_does_not_consume_the_gap_budget(meta_path):
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
    rc.record_group(strangle, "103500")

    meta = json.loads(meta_path.read_text())
    tally = sum(1 for g in meta.get("groups", {}).values()
                if g.get("engine") == "event_macro"
                and g.get("structure") == "single_long")
    assert tally == 0, "the strangle consumed a gap entry it does not own"
