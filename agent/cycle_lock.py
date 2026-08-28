#!/usr/bin/env python3
"""Exclusive ownership of one trading cycle, held by the kernel.

Cron fires every thirty minutes; a cycle blocked on a broker call can outlast
that. Two overlapping runs would read the same persistent state, each decide
budget was free, and each act on it.

`flock` rather than a lease file with a TTL. A TTL cannot tell a dead holder
from a slow one, so it eventually preempts a process that is still mid-call —
the one moment when a second writer is most dangerous. The kernel releases an
`flock` when the holder dies and not before, which is exactly the question a
TTL is guessing at.

The lock answers *who owns this cycle now*. It says nothing about risk taken
by a run that has since died: that is `agent/submission_wal.py`, which
survives the lock being released.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import socket
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class CycleAlreadyRunning(RuntimeError):
    """Another run holds the cycle lock. Not an error — a correct refusal."""


def _describe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip() or "<no owner record>"
    except OSError:
        return "<unreadable owner record>"


@contextmanager
def cycle_lock(path: str | os.PathLike, *, blocking: bool = False):
    """Hold the cycle lock for the duration of the block.

    The owner record inside the file is diagnostics only; the lock itself is
    the flock, so a stale record can never grant or deny entry.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = target.open("a+", encoding="utf-8")
    flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    try:
        try:
            fcntl.flock(handle.fileno(), flags)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise CycleAlreadyRunning(
                    f"cycle lock {target} is held by {_describe(target)}"
                ) from exc
            raise
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "acquired_utc": datetime.now(timezone.utc).isoformat(),
        }, separators=(",", ":")))
        handle.flush()
        os.fsync(handle.fileno())
        yield target
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
