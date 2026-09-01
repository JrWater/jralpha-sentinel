"""Red tests for the crash-safe entry submission journal.

These tests describe the smallest safety patch agreed for the competition
window.  The journal does not exist yet: the first run of this file is
supposed to be red.
"""
from __future__ import annotations

import json
import os
import stat
from datetime import date

import pytest


def _wal():
    from agent import submission_wal

    return submission_wal


def _reservation(wal, logical_id: str = "logical-1"):
    return wal.Reservation(
        logical_submission_id=logical_id,
        client_order_id=f"sentinel-{logical_id}",
        account_id="PAPER-TEST",
        trading_date_et=date(2026, 8, 28),
        cycle_id="cycle-1",
        intent_fingerprint="intent-fingerprint",
        manifest_sha="manifest-sha",
        git_head="a" * 40,
        max_loss_cents=800_000,
        fire_keys=("event_macro:NVDA:2026-08-28",),
        gap_counters=(("event_macro:gap", 1),),
    )


def test_client_order_id_separates_content_from_logical_submission():
    wal = _wal()

    first = wal.make_client_order_id(
        manifest_sha="manifest", intent_fingerprint="same-intent",
        logical_submission_id="attempt-1")
    retry = wal.make_client_order_id(
        manifest_sha="manifest", intent_fingerprint="same-intent",
        logical_submission_id="attempt-1")
    second_entry = wal.make_client_order_id(
        manifest_sha="manifest", intent_fingerprint="same-intent",
        logical_submission_id="attempt-2")

    assert first == retry
    assert first != second_entry


def test_lifecycle_record_is_durable_before_the_broker_call(tmp_path):
    wal = _wal()
    journal = wal.SubmissionJournal(tmp_path / "submissions.jsonl")
    observed: list[tuple[str, str]] = []

    def persist(reservation):
        observed.append(("persist", reservation.client_order_id))

    def submit(client_order_id):
        assert observed == [("persist", client_order_id)]
        return {"id": "broker-1", "status": "accepted"}

    wal.dispatch_entry(journal, _reservation(wal), submit,
                       before_broker=persist)

    assert journal.replay().by_submission["logical-1"].state == wal.COMMITTED


def test_structure_admission_handoff_precedes_broker_and_fails_closed(
        tmp_path):
    from types import SimpleNamespace

    from agent.entry_submission import StructureAdmission
    from strategy.proposal import Proposal

    wal = _wal()
    journal = wal.SubmissionJournal(tmp_path / "submissions.jsonl")
    proposal = Proposal(engine="trend_income", underlying="SPY",
                        direction="neutral", structure="credit_vertical")
    observed = []
    structures = SimpleNamespace(
        record_pending_entry=lambda *_args, **_kwargs: observed.append("pending"))
    manifest = SimpleNamespace(
        sha="manifest-sha",
        exit_intent_for=lambda *_args: SimpleNamespace(
            take_profit=0.5, stop_loss_factor=2.0))
    class Executor:
        def submit(self, _proposal, *, client_order_id):
            assert observed == ["pending"]
            observed.append("broker")
            return {"id": "broker-1", "status": "accepted",
                    "client_order_id": client_order_id}

    admission = StructureAdmission(
        manifest, structures, journal, Executor(), account_id="account",
        trading_date=date(2026, 8, 28), cycle_id="cycle", entered_at="101500")

    reservation, _order = admission.admit(
        proposal, fire_keys=(), gap_counters=())
    assert observed == ["pending", "broker"]
    assert journal.replay().by_submission[
        reservation.logical_submission_id].state == wal.COMMITTED


def test_structure_admission_failure_keeps_dispatch_unresolved(tmp_path):
    from types import SimpleNamespace

    from agent.entry_submission import AdmissionDispatchError, StructureAdmission
    from strategy.proposal import Proposal

    wal = _wal()
    journal = wal.SubmissionJournal(tmp_path / "submissions.jsonl")
    proposal = Proposal(engine="trend_income", underlying="SPY",
                        direction="neutral", structure="credit_vertical")
    structures = SimpleNamespace(
        record_pending_entry=lambda *_args, **_kwargs:
        (_ for _ in ()).throw(OSError("ledger unavailable")))
    manifest = SimpleNamespace(
        sha="manifest-sha",
        exit_intent_for=lambda *_args: SimpleNamespace(
            take_profit=0.5, stop_loss_factor=2.0))
    class Executor:
        def submit(self, *_args, **_kwargs):
            calls.append("broker")

    calls = []
    admission = StructureAdmission(
        manifest, structures, journal, Executor(), account_id="account",
        trading_date=date(2026, 8, 28), cycle_id="cycle", entered_at="101500")

    with pytest.raises(AdmissionDispatchError) as error:
        admission.admit(proposal, fire_keys=(), gap_counters=())

    assert calls == []
    reservation = error.value.reservation
    assert journal.replay().by_submission[reservation.logical_submission_id].state == \
        wal.DISPATCHING


def test_structure_admission_journal_failure_is_not_a_dispatch(tmp_path):
    from types import SimpleNamespace

    from agent.entry_submission import StructureAdmission
    from strategy.proposal import Proposal

    wal = _wal()
    journal = wal.SubmissionJournal(tmp_path / "submissions.jsonl")
    proposal = Proposal(engine="trend_income", underlying="SPY",
                        direction="neutral", structure="credit_vertical")
    manifest = SimpleNamespace(
        sha="manifest-sha",
        exit_intent_for=lambda *_args: SimpleNamespace(
            take_profit=0.5, stop_loss_factor=2.0))
    structures = SimpleNamespace(record_pending_entry=lambda *_args, **_kwargs:
                                 pytest.fail("pending record must not run"))
    calls = []

    class Executor:
        def submit(self, *_args, **_kwargs):
            calls.append("broker")

    def unavailable(_reservation):
        raise OSError("journal unavailable")

    journal.hold = unavailable
    admission = StructureAdmission(
        manifest, structures, journal, Executor(), account_id="account",
        trading_date=date(2026, 8, 28), cycle_id="cycle", entered_at="101500")

    with pytest.raises(OSError, match="journal unavailable"):
        admission.admit(proposal, fire_keys=(), gap_counters=())

    assert calls == []
    assert journal.replay().by_submission == {}


def test_new_wal_fsyncs_the_file_and_parent_directory(tmp_path, monkeypatch):
    wal = _wal()
    path = tmp_path / "new-day" / "submissions.jsonl"
    fsynced_kinds: list[str] = []
    real_fsync = wal.os.fsync

    def recording_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        fsynced_kinds.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(wal.os, "fsync", recording_fsync)

    wal.SubmissionJournal(path).hold(_reservation(wal))

    assert "file" in fsynced_kinds
    assert "directory" in fsynced_kinds


def test_lookup_keys_are_written_before_the_rest_of_each_line(tmp_path):
    wal = _wal()
    path = tmp_path / "submissions.jsonl"
    wal.SubmissionJournal(path).hold(_reservation(wal))

    line = path.read_text().splitlines()[0]

    assert line.index('"event"') < line.index('"logical_submission_id"')
    assert line.index('"logical_submission_id"') < line.index(
        '"client_order_id"')
    assert line.index('"client_order_id"') < line.index('"schema_version"')


def test_torn_tail_with_recoverable_keys_is_unresolved_dispatch(tmp_path):
    wal = _wal()
    path = tmp_path / "submissions.jsonl"
    journal = wal.SubmissionJournal(path)
    journal.hold(_reservation(wal))
    with path.open("ab") as handle:
        handle.write(
            b'{"event":"DISPATCHING","logical_submission_id":"logical-1",'
            b'"client_order_id":"sentinel-logical-1"')
        handle.flush()
        os.fsync(handle.fileno())

    view = journal.replay()

    assert not view.integrity_ok
    assert view.unresolved_dispatches == ("logical-1",)
    assert view.by_submission["logical-1"].state == "DISPATCHING"


def test_torn_tail_preserves_the_client_id_for_reconciliation(tmp_path):
    wal = _wal()
    path = tmp_path / "submissions.jsonl"
    journal = wal.SubmissionJournal(path)
    with path.open("ab") as handle:
        handle.write(
            b'{"event":"DISPATCHING","logical_submission_id":"logical-1",'
            b'"client_order_id":"sentinel-logical-1"')
        handle.flush()
        os.fsync(handle.fileno())
    queried: list[str] = []

    class Broker:
        def get_order_by_client_id(self, client_id: str):
            queried.append(client_id)
            raise ConnectionError("stay unresolved")

    wal.reconcile_unresolved(journal, Broker())

    assert queried == ["sentinel-logical-1"]


def test_torn_dispatch_with_held_reservation_can_reconcile_to_committed(
        tmp_path):
    wal = _wal()
    path = tmp_path / "submissions.jsonl"
    journal = wal.SubmissionJournal(path)
    journal.hold(_reservation(wal))
    with path.open("ab") as handle:
        handle.write(
            b'{"event":"DISPATCHING","logical_submission_id":"logical-1",'
            b'"client_order_id":"sentinel-logical-1"')
        handle.flush()
        os.fsync(handle.fileno())

    class Broker:
        def get_order_by_client_id(self, client_id: str):
            return {"id": "broker-after-torn", "status": "accepted"}

    view = wal.reconcile_unresolved(journal, Broker())

    assert view.integrity_ok
    assert view.entries_allowed
    assert view.by_submission["logical-1"].state == "COMMITTED"
    assert view.by_submission["logical-1"].broker_order_id == (
        "broker-after-torn")


def test_torn_dispatch_without_held_reservation_stays_blocking_after_lookup(
        tmp_path):
    wal = _wal()
    path = tmp_path / "submissions.jsonl"
    journal = wal.SubmissionJournal(path)
    path.write_bytes(
        b'{"event":"DISPATCHING","logical_submission_id":"logical-1",'
        b'"client_order_id":"sentinel-logical-1"')

    class Broker:
        def get_order_by_client_id(self, client_id: str):
            return {"id": "broker-without-risk-record", "status": "accepted"}

    view = wal.reconcile_unresolved(journal, Broker())

    assert not view.integrity_ok
    assert not view.entries_allowed
    assert view.unresolved_dispatches == ("logical-1",)


def test_malformed_wal_never_fails_open(tmp_path):
    wal = _wal()
    path = tmp_path / "submissions.jsonl"
    path.write_text("this is not a recoverable journal line\n")

    view = wal.SubmissionJournal(path).replay()

    assert not view.integrity_ok
    assert not view.entries_allowed


@pytest.mark.parametrize("mutator", [
    pytest.param(
        lambda row: row.update(schema_version=999), id="unknown-schema"),
    pytest.param(
        lambda row: row.update(event="MADE_UP_STATE"), id="unknown-event"),
    pytest.param(
        lambda row: row.update(client_order_id="different-client-id"),
        id="client-id-drift"),
])
def test_parseable_policy_corruption_never_fails_open(tmp_path, mutator):
    wal = _wal()
    path = tmp_path / "submissions.jsonl"
    journal = wal.SubmissionJournal(path)
    journal.hold(_reservation(wal))
    row = {
        "event": "DISPATCHING",
        "logical_submission_id": "logical-1",
        "client_order_id": "sentinel-logical-1",
        "schema_version": 1,
        "at_utc": "2026-08-28T16:00:00+00:00",
    }
    mutator(row)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")

    view = journal.replay()

    assert not view.integrity_ok
    assert not view.entries_allowed


def test_duplicate_held_record_is_policy_corruption(tmp_path):
    wal = _wal()
    path = tmp_path / "submissions.jsonl"
    journal = wal.SubmissionJournal(path)
    reservation = _reservation(wal)
    journal.hold(reservation)
    first = path.read_text()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(first)

    view = journal.replay()

    assert not view.integrity_ok
    assert not view.entries_allowed


def test_dispatch_must_be_durable_before_commit(tmp_path):
    wal = _wal()
    journal = wal.SubmissionJournal(tmp_path / "submissions.jsonl")
    journal.hold(_reservation(wal))

    with pytest.raises(wal.InvalidTransition):
        journal.commit("logical-1", broker_order_id="broker-1",
                       broker_status="accepted")


def test_same_terminal_transition_is_idempotent_but_conflict_is_fatal(tmp_path):
    wal = _wal()
    journal = wal.SubmissionJournal(tmp_path / "submissions.jsonl")
    journal.hold(_reservation(wal))
    journal.mark_dispatching("logical-1")
    journal.commit("logical-1", broker_order_id="broker-1",
                   broker_status="accepted")

    journal.commit("logical-1", broker_order_id="broker-1",
                   broker_status="accepted")
    with pytest.raises(wal.InvalidTransition):
        journal.release("logical-1", reason_code="BROKER_REJECTED")


def test_daily_risk_is_a_deduplicated_projection_not_a_counter(tmp_path):
    wal = _wal()
    journal = wal.SubmissionJournal(tmp_path / "submissions.jsonl")
    journal.hold(_reservation(wal))
    journal.mark_dispatching("logical-1")
    journal.commit("logical-1", broker_order_id="broker-1",
                   broker_status="accepted")
    # A replay-safe duplicate must not add the same risk twice.
    journal.commit("logical-1", broker_order_id="broker-1",
                   broker_status="accepted")

    risk = journal.replay().risk_for(date(2026, 8, 28))

    assert risk.committed_cents == 800_000
    assert risk.held_cents == 0
    assert risk.fire_keys == ("event_macro:NVDA:2026-08-28",)
    assert risk.gap_units == {"event_macro:gap": 1}


def test_unresolved_dispatch_keeps_every_reservation_held(tmp_path):
    wal = _wal()
    journal = wal.SubmissionJournal(tmp_path / "submissions.jsonl")
    journal.hold(_reservation(wal))
    journal.mark_dispatching("logical-1")

    risk = journal.replay().risk_for(date(2026, 8, 28))

    assert risk.committed_cents == 0
    assert risk.held_cents == 800_000
    assert risk.fire_keys == ("event_macro:NVDA:2026-08-28",)
    assert risk.gap_units == {"event_macro:gap": 1}


def test_reconciliation_evidence_precedes_the_resolving_transition(tmp_path):
    wal = _wal()
    path = tmp_path / "submissions.jsonl"
    journal = wal.SubmissionJournal(path)
    journal.hold(_reservation(wal))
    journal.mark_dispatching("logical-1")
    journal.observe("logical-1", result="FOUND",
                    detail="broker order broker-1")
    journal.commit("logical-1", broker_order_id="broker-1",
                   broker_status="accepted")

    events = [json.loads(line)["event"] for line in path.read_text().splitlines()]

    assert events[-2:] == ["RECONCILE_OBSERVED", "COMMITTED"]


def test_submit_exception_leaves_a_durable_unresolved_dispatch(tmp_path):
    """A timeout may mean Alpaca accepted the order but lost the response."""
    wal = _wal()
    journal = wal.SubmissionJournal(tmp_path / "submissions.jsonl")

    def timed_out(client_order_id: str):
        assert client_order_id == "sentinel-logical-1"
        raise TimeoutError("response lost after request dispatch")

    with pytest.raises(TimeoutError):
        wal.dispatch_entry(journal, _reservation(wal), timed_out)

    view = journal.replay()
    assert view.unresolved_dispatches == ("logical-1",)
    assert view.risk_for(date(2026, 8, 28)).held_cents == 800_000


def test_successful_submit_commits_only_after_broker_returns(tmp_path):
    wal = _wal()
    journal = wal.SubmissionJournal(tmp_path / "submissions.jsonl")

    class Order:
        id = "broker-1"
        status = "accepted"

    order = wal.dispatch_entry(
        journal, _reservation(wal), lambda client_order_id: Order())

    assert order.id == "broker-1"
    assert journal.replay().by_submission["logical-1"].state == "COMMITTED"


def test_broker_response_without_order_id_stays_unresolved(tmp_path):
    wal = _wal()
    journal = wal.SubmissionJournal(tmp_path / "submissions.jsonl")
    order_without_id = type("Order", (), {"status": "accepted"})()

    with pytest.raises(wal.BrokerProtocolError, match="order id"):
        wal.dispatch_entry(
            journal, _reservation(wal),
            lambda client_order_id: order_without_id)

    assert journal.replay().unresolved_dispatches == ("logical-1",)


def test_startup_reconciler_queries_by_client_order_id(tmp_path):
    wal = _wal()
    path = tmp_path / "submissions.jsonl"
    journal = wal.SubmissionJournal(path)
    journal.hold(_reservation(wal))
    journal.mark_dispatching("logical-1")
    queried: list[str] = []

    class Broker:
        def get_order_by_client_id(self, client_id: str):
            queried.append(client_id)
            return type("Order", (), {"id": "broker-1", "status": "accepted"})()

    wal.reconcile_unresolved(journal, Broker())

    assert queried == ["sentinel-logical-1"]
    assert journal.replay().by_submission["logical-1"].state == "COMMITTED"
    events = [json.loads(line)["event"] for line in path.read_text().splitlines()]
    assert events[-2:] == ["RECONCILE_OBSERVED", "COMMITTED"]


def test_reconciler_accepts_the_sdk_dictionary_response(tmp_path):
    wal = _wal()
    journal = wal.SubmissionJournal(tmp_path / "submissions.jsonl")
    journal.hold(_reservation(wal))
    journal.mark_dispatching("logical-1")

    class Broker:
        def get_order_by_client_id(self, client_id: str):
            return {"id": "broker-dict-1", "status": "accepted"}

    wal.reconcile_unresolved(journal, Broker())

    record = journal.replay().by_submission["logical-1"]
    assert record.state == "COMMITTED"
    assert record.broker_order_id == "broker-dict-1"


def test_one_absent_observation_does_not_release_a_dispatch(tmp_path):
    """A single empty lookup can be eventual consistency, not proof."""
    wal = _wal()
    journal = wal.SubmissionJournal(tmp_path / "submissions.jsonl")
    journal.hold(_reservation(wal))
    journal.mark_dispatching("logical-1")

    class Broker:
        def get_order_by_client_id(self, client_id: str):
            return None

    wal.reconcile_unresolved(journal, Broker())

    assert journal.replay().unresolved_dispatches == ("logical-1",)


def test_explicit_alpaca_order_not_found_releases_dispatch(tmp_path):
    """A broker's typed 404 is definitive evidence, unlike a null lookup."""
    wal = _wal()
    journal = wal.SubmissionJournal(tmp_path / "submissions.jsonl")
    journal.hold(_reservation(wal))
    journal.mark_dispatching("logical-1")

    class OrderNotFound(Exception):
        def __str__(self):
            return ('{"code":40410000,"message":'
                    '"order not found for sentinel-logical-1"}')

    class Broker:
        def get_order_by_client_id(self, client_id: str):
            assert client_id == "sentinel-logical-1"
            raise OrderNotFound()

    view = wal.reconcile_unresolved(journal, Broker())

    assert view.entries_allowed
    record = view.by_submission["logical-1"]
    assert record.state == wal.RELEASED
    assert record.reason_code == "broker_confirmed_order_absent"


def test_404_for_a_different_order_never_releases_dispatch(tmp_path):
    wal = _wal()
    journal = wal.SubmissionJournal(tmp_path / "submissions.jsonl")
    journal.hold(_reservation(wal))
    journal.mark_dispatching("logical-1")

    class WrongOrderNotFound(Exception):
        def __str__(self):
            return ('{"code":40410000,"message":'
                    '"order not found for sentinel-other"}')

    class Broker:
        def get_order_by_client_id(self, _client_id: str):
            raise WrongOrderNotFound()

    view = wal.reconcile_unresolved(journal, Broker())

    assert not view.entries_allowed
    assert view.unresolved_dispatches == ("logical-1",)


def test_failed_reconciliation_keeps_dispatch_unresolved(tmp_path):
    wal = _wal()
    journal = wal.SubmissionJournal(tmp_path / "submissions.jsonl")
    journal.hold(_reservation(wal))
    journal.mark_dispatching("logical-1")

    class UnavailableBroker:
        def get_order_by_client_id(self, client_id: str):
            raise ConnectionError("broker unavailable")

    wal.reconcile_unresolved(journal, UnavailableBroker())

    view = journal.replay()
    assert view.unresolved_dispatches == ("logical-1",)
    assert not view.entries_allowed


def test_committed_order_status_is_refreshed_by_client_order_id(tmp_path):
    wal = _wal()
    journal = wal.SubmissionJournal(tmp_path / "submissions.jsonl")
    journal.hold(_reservation(wal))
    journal.mark_dispatching("logical-1")
    journal.commit("logical-1", broker_order_id="broker-1",
                   broker_status="accepted")

    class Broker:
        def get_order_by_client_id(self, client_id: str):
            assert client_id == "sentinel-logical-1"
            return {
                "id": "broker-1", "status": "filled", "filled_qty": "1",
                "filled_avg_price": "-0.63"}

    updates = wal.refresh_committed_orders(journal, Broker())

    assert updates == {"sentinel-logical-1": {
        "broker_order_id": "broker-1",
        "broker_status": "filled",
        "filled_qty": 1.0,
        "filled_avg_price": -0.63,
    }}


def test_status_refresh_failure_preserves_public_history(tmp_path):
    wal = _wal()
    journal = wal.SubmissionJournal(tmp_path / "submissions.jsonl")
    journal.hold(_reservation(wal))
    journal.mark_dispatching("logical-1")
    journal.commit("logical-1", broker_order_id="broker-1",
                   broker_status="accepted")

    class Broker:
        def get_order_by_client_id(self, client_id: str):
            raise ConnectionError("read unavailable")

    assert wal.refresh_committed_orders(journal, Broker()) == {}
