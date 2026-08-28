"""The dress rehearsal may read live paper state but must never mutate it."""

from pathlib import Path

from scripts import dress_rehearsal as rehearsal


class BrokerMutationTrap:
    def cancel_order_by_id(self, order_id):
        raise AssertionError(f"rehearsal forwarded cancellation {order_id}")


def test_rehearsal_watches_the_real_position_metadata_path():
    assert "state/positions_meta.json" in rehearsal.WATCHED
    assert "state/submission_wal.jsonl" in rehearsal.WATCHED
    assert "state/meta.json" not in rehearsal.WATCHED


def test_rehearsal_captures_cancellation_without_forwarding():
    client = rehearsal.RecordingClient(BrokerMutationTrap())

    client.cancel_order_by_id("OPEN-ORDER-1")

    assert client.cancellations == ["OPEN-ORDER-1"]


def test_all_watched_paths_are_inside_the_repo():
    root = rehearsal.ROOT.resolve()
    for rel in rehearsal.WATCHED:
        assert Path(root, rel).resolve().is_relative_to(root)
