"""The one live state boundary owns the advisory cycle lock."""
from __future__ import annotations

import multiprocessing

import pytest


def _cycle_lock():
    from agent import cycle_lock

    return cycle_lock


def _try_lock_in_child(path: str, queue) -> None:
    from agent.cycle_lock import CycleAlreadyRunning, cycle_lock

    try:
        with cycle_lock(path, blocking=False):
            queue.put("acquired")
    except CycleAlreadyRunning:
        queue.put("blocked")


def test_a_second_process_cannot_enter_the_same_cycle(tmp_path):
    locking = _cycle_lock()
    path = tmp_path / "cycle.lock"
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()

    with locking.cycle_lock(path, blocking=False):
        child = context.Process(target=_try_lock_in_child,
                                args=(str(path), queue))
        child.start()
        child.join(timeout=5)

    assert child.exitcode == 0
    assert queue.get(timeout=1) == "blocked"


def test_nonblocking_lock_reports_the_existing_owner(tmp_path):
    locking = _cycle_lock()
    path = tmp_path / "cycle.lock"

    with locking.cycle_lock(path, blocking=False):
        with pytest.raises(locking.CycleAlreadyRunning):
            with locking.cycle_lock(path, blocking=False):
                pass
