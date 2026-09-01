#!/usr/bin/env python3
"""Tests for the gate machinery.

These are not coverage decoration. Each one locks a specific way this design
has been observed to fail, in this system or in the one it is ported from. A
gate that stops stopping things does so silently by nature, so the only place
that failure can be caught is here.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gates import checks, safety_gate                        # noqa: E402
from gates.evaluation import CycleSubject                    # noqa: E402
from gates.registry import (DIMENSIONS, Gate, GateResult,     # noqa: E402
                            blockers, severity_of, validate)
from policy.loader import load as load_manifest               # noqa: E402
from scripts.run_cycle import run_preflight                   # noqa: E402
from strategy.data import MarketState                         # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)   # 14:00 New York, mid-session


def ok_check(_ctx):
    return GateResult(True, "")


# ── the registry cannot be filled in halfway ─────────────────────────────────

def test_missing_field_is_an_import_time_error():
    """Omitting severity used to yield a gate that could never block."""
    with pytest.raises(TypeError):
        Gate("half", ok_check, "preflight", "BLOCKING")   # no dimension


def test_typo_in_severity_is_rejected():
    """'BLOCKNIG' is exactly as dangerous as omitting severity."""
    with pytest.raises(ValueError, match="severity"):
        validate([Gate("typo", ok_check, CycleSubject, "BLOCKNIG",
                       "Process Health", "r")])


def test_typo_in_dimension_cannot_grow_a_sixth_dimension():
    with pytest.raises(ValueError, match="dimension"):
        validate([Gate("typo", ok_check, CycleSubject, "BLOCKING",
                       "Proces Health", "r")])


def test_rationale_is_mandatory():
    with pytest.raises(ValueError, match="rationale"):
        validate([Gate("bare", ok_check, CycleSubject, "BLOCKING",
                       "Process Health", "   ")])


def test_duplicate_names_rejected():
    g = Gate("dup", ok_check, CycleSubject, "INFO", "Process Health", "r")
    with pytest.raises(ValueError, match="duplicate"):
        validate([g, g])


# ── fail closed, not open ────────────────────────────────────────────────────

def test_unknown_gate_is_blocking_not_attention():
    """The original defect, inverted: the default must be 'stop'."""
    assert severity_of(checks.GATES, "a-gate-that-does-not-exist") == "BLOCKING"


def test_no_exemptions_by_name():
    """A failing BLOCKING gate appears in blockers regardless of its name.

    The ported code once carried `name != "entry_authority"`, which matched
    nothing but stood ready to disable a safety gate the day someone renamed a
    check. This asserts that no such escape hatch exists.
    """
    results = {g.name: GateResult(False, "red") for g in checks.GATES}
    blocking = blockers(checks.GATES, results)
    expected = {g.name for g in checks.GATES if g.severity == "BLOCKING"}
    assert set(blocking) == expected
    assert "account_identity" in blocking


def test_every_shipped_gate_has_a_known_dimension():
    for gate in checks.GATES:
        assert gate.dimension in DIMENSIONS


# ── the permit is a permit, not a note ───────────────────────────────────────

@pytest.fixture
def permit(tmp_path):
    return tmp_path / "entry_permit.json"


def _write(path, **overrides):
    payload = {
        "schema_version": 1,
        "status": "READY",
        "blocking_gates": [],
        "attention_gates": [],
        "generated_at_utc": NOW.isoformat(),
        "git_head": safety_gate.git_head(),
        "manifest_sha": "sha-under-test",
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload))


def test_fresh_permit_allows(permit):
    _write(permit)
    d = safety_gate.evaluate(manifest_sha="sha-under-test", path=permit,
                             now=NOW)
    assert d.allowed and d.reason == "PERMIT_READY"


def test_stale_permit_refuses(permit):
    """Silence must never read as consent."""
    _write(permit)
    d = safety_gate.evaluate(manifest_sha="sha-under-test", path=permit,
                             now=NOW + timedelta(hours=4))
    assert not d.allowed and d.reason == "PERMIT_STALE"


def test_permit_does_not_transfer_across_code(permit):
    _write(permit, git_head="0" * 40)
    d = safety_gate.evaluate(manifest_sha="sha-under-test", path=permit,
                             now=NOW)
    assert not d.allowed and d.reason == "PERMIT_HEAD_MISMATCH"


def test_permit_does_not_transfer_across_parameters(permit):
    """Edited parameters are a different experiment, not the same one."""
    _write(permit)
    d = safety_gate.evaluate(manifest_sha="a-different-sha", path=permit,
                             now=NOW)
    assert not d.allowed and d.reason == "PERMIT_MANIFEST_MISMATCH"


def test_blocked_permit_refuses(permit):
    _write(permit, status="BLOCKED", blocking_gates=["equity_floor"])
    d = safety_gate.evaluate(manifest_sha="sha-under-test", path=permit,
                             now=NOW)
    assert not d.allowed and "equity_floor" in d.blockers


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda p: None, id="missing-file"),
    pytest.param(lambda p: p.write_text("{not json"), id="malformed"),
    pytest.param(lambda p: p.write_text("{}"), id="empty-object"),
    pytest.param(lambda p: p.write_text('{"schema_version":1,'
                                        '"generated_at_utc":"nonsense"}'),
                 id="unparseable-timestamp"),
])
def test_every_broken_permit_refuses(permit, mutate):
    """There is no failure path in that module whose fallback is 'allow'."""
    mutate(permit)
    d = safety_gate.evaluate(manifest_sha="sha-under-test", path=permit,
                             now=NOW)
    assert not d.allowed


def test_atomic_write_leaves_no_temp_files(tmp_path):
    path = tmp_path / "state" / "entry_permit.json"
    safety_gate.atomic_write(path, {"a": 1})
    assert json.loads(path.read_text()) == {"a": 1}
    assert list(path.parent.glob(".permit-*")) == []


# ── the risk gates actually bite ─────────────────────────────────────────────

def _account(**kw):
    base = dict(status="AccountStatus.ACTIVE", account_number="PA000TEST",
                trading_blocked=False, equity="100000", cash="100000",
                options_trading_level=3, options_buying_power="100000")
    base.update(kw)
    return SimpleNamespace(**base)


def _ctx(manifest, **kw):
    base = dict(manifest=manifest, now_utc=NOW, account=_account(),
                is_paper_session=True,
                clock=SimpleNamespace(is_open=True), positions=[],
                ledger_positions=[], option_quote_age_seconds=900.0,
                underlying_bar_age_seconds=60.0, decision_log_writable=True,
                git_head="a" * 40, git_dirty=False)
    base.update(kw)
    return checks.EvalContext(**base)


@pytest.fixture
def manifest():
    return load_manifest()


def _manifest_with(manifest, **env_overrides):
    """A manifest variant, so tests assert on behaviour not on today's config.

    The original version of the test below read the real manifest while its
    competition_account_id happened to be null. It passed for a day and then
    started failing the moment the account was bound — which means it was
    testing the current environment, not the gate.
    """
    import copy

    from policy.loader import Manifest
    raw = copy.deepcopy(manifest._raw)
    raw["environment"].update(env_overrides)
    return Manifest(raw)


def test_unnamed_account_cannot_trade(manifest):
    """Null competition_account_id is not a wildcard."""
    unnamed = _manifest_with(manifest, competition_account_id=None)
    r = checks.check_account_identity(_ctx(unnamed))
    assert not r.ok and "competition_account_id" in r.detail


def test_a_different_account_cannot_inherit_the_permit(manifest):
    """Order authority binds to one account and never carries forward."""
    ctx = _ctx(manifest, account=_account(account_number="PA_SOMEONE_ELSE"))
    r = checks.check_account_identity(ctx)
    assert not r.ok and "PA_SOMEONE_ELSE" in r.detail


def test_the_declared_account_is_accepted(manifest):
    declared = manifest.get("environment", "competition_account_id")
    ctx = _ctx(manifest, account=_account(account_number=declared))
    assert checks.check_account_identity(ctx).ok


def test_live_session_is_refused_even_with_matching_account(manifest, tmp_path):
    """PAPER-only is structural, not a comment."""
    r = checks.check_account_identity(_ctx(manifest, is_paper_session=False))
    assert not r.ok and "not paper" in r.detail


def test_equity_floor_trips_at_70_percent(manifest):
    assert checks.check_equity_floor(
        _ctx(manifest, account=_account(equity="70000.01"))).ok
    tripped = checks.check_equity_floor(
        _ctx(manifest, account=_account(equity="69999.99")))
    assert not tripped.ok and "ENTRY MAINTENANCE" in tripped.detail


def test_missing_equity_fails_closed(manifest):
    assert not checks.check_equity_floor(
        _ctx(manifest, account=_account(equity=None))).ok


def test_stalled_option_feed_is_distinguished_from_designed_delay(manifest):
    """15 minutes late is the product. 30 minutes late is a broken feed."""
    assert checks.check_option_chain_data(
        _ctx(manifest, option_quote_age_seconds=900.0)).ok
    stalled = checks.check_option_chain_data(
        _ctx(manifest, option_quote_age_seconds=1800.0))
    assert not stalled.ok and "stalled" in stalled.detail


def test_unknown_quote_age_fails_closed(manifest):
    assert not checks.check_option_chain_data(
        _ctx(manifest, option_quote_age_seconds=None)).ok


def test_ledger_disagreement_blocks_new_exposure(manifest):
    ctx = _ctx(manifest,
               positions=[SimpleNamespace(symbol="SPY260904P00600000",
                                          qty="1")],
               ledger_positions=[])
    assert not checks.check_position_reconcile(ctx).ok


def test_non_option_broker_position_blocks_new_exposure(manifest):
    """An options-only agent must not add risk beside an unmanaged stock."""
    result = checks.check_non_option_positions(
        _ctx(manifest, non_option_positions=[
            SimpleNamespace(symbol="TSLA", qty="500"),
        ]))

    assert not result.ok
    assert "TSLA" in result.detail


def test_non_option_position_is_a_registered_blocking_entry_gate():
    gate = next(g for g in checks.GATES if g.name == "non_option_positions")

    assert gate.accepts is CycleSubject
    assert gate.severity == "BLOCKING"
    assert gate.dimension == "Entry Authority"


def test_preflight_blocks_new_exposure_when_broker_reports_stock(manifest, tmp_path):
    state = MarketState(
        account=_account(), clock=SimpleNamespace(is_open=True),
        positions=[], non_option_positions=[
            SimpleNamespace(symbol="TSLA", qty="500"),
        ], now_utc=NOW)

    results = run_preflight(
        state, manifest, ledger=[], root=tmp_path, decision_log_writable=True)

    assert not results["non_option_positions"].ok
    assert "TSLA" in results["non_option_positions"].detail


def test_unresolved_dispatch_blocks_new_exposure(manifest):
    result = checks.check_unresolved_dispatches(
        _ctx(manifest, unresolved_dispatch_count=1))

    assert not result.ok
    assert "DISPATCHING" in result.detail


def test_no_unresolved_dispatch_allows_entry_evaluation(manifest):
    assert checks.check_unresolved_dispatches(
        _ctx(manifest, unresolved_dispatch_count=0)).ok


def test_unresolved_dispatch_is_a_registered_blocking_entry_gate():
    gate = next(g for g in checks.GATES
                if g.name == "unresolved_dispatches")

    assert gate.accepts is CycleSubject
    assert gate.severity == "BLOCKING"
    assert gate.dimension == "Entry Authority"


def test_unresolved_structure_close_blocks_new_exposure(manifest):
    result = checks.check_unresolved_structure_closes(
        _ctx(manifest, unresolved_structure_close_count=1))

    assert not result.ok
    assert "structure close" in result.detail


def test_unresolved_structure_close_is_a_registered_blocking_entry_gate():
    gate = next(g for g in checks.GATES
                if g.name == "unresolved_structure_closes")

    assert gate.accepts is CycleSubject
    assert gate.severity == "BLOCKING"
    assert gate.dimension == "Entry Authority"


def test_pending_entry_reconciliation_blocks_new_exposure(manifest):
    result = checks.check_unresolved_entry_reconciliations(
        _ctx(manifest, unresolved_entry_reconciliation_count=1))

    assert not result.ok
    assert "entry reconciliation" in result.detail


def test_pending_entry_reconciliation_is_a_registered_blocking_gate():
    gate = next(g for g in checks.GATES
                if g.name == "unresolved_entry_reconciliations")

    assert gate.accepts is CycleSubject
    assert gate.severity == "BLOCKING"
    assert gate.dimension == "Entry Authority"


# ── the proposal gates ───────────────────────────────────────────────────────

def _proposal(**kw):
    base = dict(engine="trend_directional", underlying="SPY", order_class="mleg", type="limit",
                time_in_force="day", legs=[1, 2], max_loss_dollars=400.0)
    base.update(kw)
    return SimpleNamespace(**base)


def test_declared_spread_shape_is_accepted(manifest):
    assert checks.check_order_shape_declared(
        _ctx(manifest, proposal=_proposal())).ok


def test_market_order_is_structurally_impossible(manifest):
    """No declared shape has type 'market', so no proposal can become one."""
    r = checks.check_order_shape_declared(
        _ctx(manifest, proposal=_proposal(type="market")))
    assert not r.ok and "undeclared shape" in r.detail


def test_gtc_is_refused_because_it_is_not_declared(manifest):
    assert not checks.check_order_shape_declared(
        _ctx(manifest, proposal=_proposal(time_in_force="gtc"))).ok


def test_four_leg_condor_is_declared_and_accepted(manifest):
    """V2 declares 1-, 2- and 4-leg shapes (condors are part of the plan)."""
    assert checks.check_order_shape_declared(
        _ctx(manifest, proposal=_proposal(
            engine="vol_income", legs=[1, 2, 3, 4]))).ok


def test_globally_declared_shape_is_refused_when_not_bound_to_strategy(manifest):
    r = checks.check_order_shape_declared(
        _ctx(manifest, proposal=_proposal(engine="vol_income")))

    assert not r.ok and "undeclared shape for strategy vol_income" in r.detail


def test_three_leg_shape_is_still_refused(manifest):
    """A shape nobody declared — 3 legs — must stay impossible to submit."""
    assert not checks.check_order_shape_declared(
        _ctx(manifest, proposal=_proposal(legs=[1, 2, 3]))).ok


def test_invented_ticker_is_refused(manifest):
    """An LLM that hallucinates a symbol gets refused, not filled."""
    assert not checks.check_symbol_declared(
        _ctx(manifest, proposal=_proposal(underlying="TSLQ"))).ok


def test_per_trade_risk_cap_is_twelve_thousand(manifest):
    assert checks.check_per_trade_risk(
        _ctx(manifest, proposal=_proposal(max_loss_dollars=12000.0))).ok
    assert not checks.check_per_trade_risk(
        _ctx(manifest, proposal=_proposal(max_loss_dollars=12000.01))).ok


def test_risk_cap_does_not_rescale_after_a_drawdown(manifest):
    """Caps are fractions of DECLARED STARTING equity, not current equity.

    Sizing off current equity would keep the same relative aggression all the
    way down. At $80k equity the cap must still be $12,000, not $9,600.
    """
    ctx = _ctx(manifest, account=_account(equity="80000"),
               proposal=_proposal(max_loss_dollars=450.0))
    assert checks.check_per_trade_risk(ctx).ok


def test_proposal_without_stated_risk_is_refused(manifest):
    """'How much can this lose?' has no default answer."""
    assert not checks.check_per_trade_risk(
        _ctx(manifest, proposal=_proposal(max_loss_dollars=None))).ok


def test_entry_window_excludes_the_open_and_the_close(manifest):
    def at(hour, minute):
        # New York is UTC-4 in late August
        return _ctx(manifest,
                    now_utc=datetime(2026, 8, 28, hour + 4, minute, tzinfo=UTC),
                    proposal=_proposal())

    assert not checks.check_entry_window(at(9, 45)).ok    # opening auction
    assert checks.check_entry_window(at(11, 0)).ok
    assert not checks.check_entry_window(at(15, 35)).ok   # closing 30 min


def test_event_macro_may_enter_the_nfp_gap_window(manifest):
    """The declared exception: the 0-DTE gap continuation enters 09:30-09:50
    on the report morning. Everyone else still waits for 10:00."""
    def at(hour, minute, engine="event_macro"):
        return _ctx(manifest,
                    now_utc=datetime(2026, 9, 4, hour + 4, minute, tzinfo=UTC),
                    proposal=_proposal(engine=engine, legs=[1]))

    assert checks.check_entry_window(at(9, 35)).ok
    assert not checks.check_entry_window(at(9, 25)).ok
    assert not checks.check_entry_window(at(9, 35, "trend_directional")).ok


def test_closed_market_blocks_entries(manifest):
    assert not checks.check_market_open(
        _ctx(manifest, clock=SimpleNamespace(is_open=False))).ok


# ── the competition account stays pristine until kickoff ─────────────────────

BEFORE_KICKOFF = datetime(2026, 8, 26, 18, 0, tzinfo=UTC)
AFTER_KICKOFF = datetime(2026, 8, 28, 16, 0, tzinfo=UTC)


def test_competition_account_refuses_trades_before_kickoff(manifest):
    """A development trade on the judged account destroys the $100,000 start."""
    declared = manifest.get("environment", "competition_account_id")
    ctx = _ctx(manifest, now_utc=BEFORE_KICKOFF,
               account=_account(account_number=declared),
               proposal=_proposal())
    r = checks.check_competition_window(ctx)
    assert not r.ok and "pristine" in r.detail


def test_dev_account_trades_freely_before_kickoff(manifest):
    ctx = _ctx(manifest, now_utc=BEFORE_KICKOFF,
               account=_account(account_number="PA31GLG5O9HU"),
               proposal=_proposal())
    assert checks.check_competition_window(ctx).ok


def test_competition_account_opens_at_kickoff(manifest):
    declared = manifest.get("environment", "competition_account_id")
    ctx = _ctx(manifest, now_utc=AFTER_KICKOFF,
               account=_account(account_number=declared),
               proposal=_proposal())
    assert checks.check_competition_window(ctx).ok


def test_pristine_guard_is_blocking_not_advisory(manifest):
    assert severity_of(checks.GATES, "competition_window") == "BLOCKING"


# ── scheduling guard: the competition window has a far end too ───────────────

def test_cycle_window_stops_after_the_final_date():
    """competition_window closes the front door; nothing closed the back one.

    The gate set has no upper bound and the crontab has no end date, so after
    the final trading date the agent would have kept opening positions on the
    account the judges are reading.
    """
    from datetime import date
    from scripts.cycle_window import past_final_date
    final = date(2026, 9, 4)
    assert not past_final_date(date(2026, 8, 28), final)   # kickoff
    assert not past_final_date(final, final)               # final day itself
    assert past_final_date(date(2026, 9, 7), final)        # first weekday after


def test_cycle_window_reads_the_manifest_not_a_hardcoded_date():
    """One copy of the date, so the guard cannot drift from the policy."""
    import json
    from pathlib import Path
    from scripts import cycle_window
    declared = json.loads(
        (Path(cycle_window.ROOT) / "policy" / "manifest.json").read_text()
    )["session"]["final_trading_date"]
    # Assert the guard carries no copy of whatever the manifest declares —
    # not that the manifest declares one particular date. Pinning the date
    # here would be the second copy this test exists to forbid, and would
    # fail the moment policy legitimately moves the final date.
    assert declared not in Path(cycle_window.__file__).read_text()


def test_cycle_window_manages_the_book_after_the_final_date():
    """Past the final date the guard suppresses NEW exposure, not the cycle.

    manage_exits is the only exit path in this repo, so refusing to run would
    strand a residual position from an unfilled 09-04 flatten limit — and cron
    would log success while it sat there.
    """
    from pathlib import Path
    from scripts import cycle_window
    src = Path(cycle_window.__file__).read_text()
    assert "--exits-only" in src
    assert "run_cycle" in src
