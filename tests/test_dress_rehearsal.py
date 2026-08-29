"""The dress rehearsal may read live paper state but must never mutate it."""

from pathlib import Path

from agent.ledger import StructureLedger
from scripts import dress_rehearsal as rehearsal


class BrokerMutationTrap:
    def cancel_order_by_id(self, order_id):
        raise AssertionError(f"rehearsal forwarded cancellation {order_id}")


def test_rehearsal_watches_the_real_position_metadata_path():
    assert "state/positions_meta.json" in rehearsal.WATCHED
    assert "state/submission_wal.jsonl" in rehearsal.WATCHED
    assert "state/cycle.lock" in rehearsal.WATCHED
    assert "state/meta.json" not in rehearsal.WATCHED


def test_rehearsal_captures_cancellation_without_forwarding():
    client = rehearsal.RecordingClient(BrokerMutationTrap())

    client.cancel_order_by_id("OPEN-ORDER-1")

    assert client.cancellations == ["OPEN-ORDER-1"]


def test_all_watched_paths_are_inside_the_repo():
    root = rehearsal.ROOT.resolve()
    for rel in rehearsal.WATCHED:
        assert Path(root, rel).resolve().is_relative_to(root)


def test_rehearsal_uses_a_temporary_structure_ledger(tmp_path):
    """A would-be entry must not write the production structure metadata."""
    ledger = rehearsal.rehearsal_structures(tmp_path)

    assert isinstance(ledger, StructureLedger)
    assert ledger.path == tmp_path / "positions_meta.json"
    assert ledger.path != rehearsal.ROOT / "state" / "positions_meta.json"


def test_rehearsal_uses_a_temporary_cycle_lock(tmp_path):
    lock = rehearsal.rehearsal_cycle_lock(tmp_path)

    assert lock == tmp_path / "cycle.lock"
    assert lock != rehearsal.ROOT / "state" / "cycle.lock"


def test_rehearsal_uses_a_temporary_broker_mirror_ledger(tmp_path):
    path = rehearsal.rehearsal_ledger_path(tmp_path)

    assert path == tmp_path / "ledger.json"
    assert path != rehearsal.ROOT / "state" / "ledger.json"


def test_rehearsal_checks_decision_log_writability_only_in_temp_state(tmp_path):
    assert rehearsal.rehearsal_decision_log_writable(tmp_path) is True
    assert (tmp_path / "state" / "decisions.jsonl").exists()
    assert not (rehearsal.ROOT / "state" / "decision-log-rehearsal-test").exists()
