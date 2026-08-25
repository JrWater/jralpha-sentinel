#!/usr/bin/env python3
"""Durable, fail-closed bridge from gate results to entry permission.

The gates run on a cycle. The executor runs on a different cycle. Between them
sits this file: a snapshot on disk saying whether new exposure is currently
permitted, who said so, when, and against which code and which manifest.

Three things make it a permit rather than a note:

*Staleness expires it.* A permit older than ``MAX_AGE`` is not "probably still
fine", it is unavailable. Silence must never read as consent.

*Code binds it.* The snapshot records the git HEAD and the manifest SHA that
produced it. If either has moved, the permit does not transfer — code that was
never evaluated does not inherit permission earned by code that was.

*Every failure path returns "no".* Missing file, malformed JSON, unreadable
timestamp, unknown schema: all of them land on refusal. There is no exception
handler in this module whose fallback is to allow trading.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = ROOT / "state" / "entry_permit.json"

# Two cycles of tolerance. Long enough that one slow cycle does not halt the
# book, short enough that an agent which died overnight cannot wake up holding
# yesterday's permission.
MAX_AGE = timedelta(minutes=90)
SCHEMA_VERSION = 1


class PermitDecision(NamedTuple):
    allowed: bool
    reason: str
    blockers: tuple = ()


def git_head(repo: Path = ROOT) -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                            capture_output=True, text=True, timeout=10,
                            check=True)
    return result.stdout.strip()


def atomic_write(path: Path, payload: dict) -> None:
    """Write or do not write. A half-written permit is a permit.

    os.replace is atomic on POSIX, so a reader either sees the old snapshot or
    the new one, never a truncated file that happens to parse.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=".permit-", suffix=".tmp",
                               dir=str(path.parent))
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True,
                      indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_permit(results, gates, *, manifest_sha: str,
                 path: Path = DEFAULT_STATE, repo: Path = ROOT,
                 now=None) -> dict:
    """Turn this cycle's gate results into a durable permit."""
    from gates.registry import blockers as blocking_names

    now = now or datetime.now(timezone.utc)
    blockers = blocking_names(gates, results)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "status": "READY" if not blockers else "BLOCKED",
        "blocking_gates": list(blockers),
        "attention_gates": sorted(
            name for name, r in results.items()
            if not r.ok and name not in blockers),
        "generated_at_utc": now.astimezone(timezone.utc).isoformat(),
        "git_head": git_head(repo),
        "manifest_sha": manifest_sha,
    }
    atomic_write(path, payload)
    return payload


def evaluate(*, manifest_sha: str, path: Path = DEFAULT_STATE,
             repo: Path = ROOT, now=None,
             max_age: timedelta = MAX_AGE) -> PermitDecision:
    """Read the permit. Every path that is not an explicit YES is a NO."""
    now = now or datetime.now(timezone.utc)
    try:
        payload = json.loads(path.read_text())
        blockers = tuple(str(x) for x in payload.get("blocking_gates", []))

        if payload.get("schema_version") != SCHEMA_VERSION:
            return PermitDecision(False, "PERMIT_SCHEMA", blockers)

        generated = datetime.fromisoformat(
            str(payload["generated_at_utc"]).replace("Z", "+00:00"))
        if generated.tzinfo is None:
            raise ValueError("naive permit timestamp")

        if payload.get("git_head") != git_head(repo):
            return PermitDecision(False, "PERMIT_HEAD_MISMATCH", blockers)

        if payload.get("manifest_sha") != manifest_sha:
            return PermitDecision(False, "PERMIT_MANIFEST_MISMATCH", blockers)

        if now.astimezone(timezone.utc) - generated > max_age:
            return PermitDecision(False, "PERMIT_STALE", blockers)

        if payload.get("status") != "READY" or blockers:
            return PermitDecision(False, "PERMIT_BLOCKED", blockers)

        return PermitDecision(True, "PERMIT_READY")

    except (OSError, ValueError, KeyError, TypeError,
            json.JSONDecodeError, subprocess.SubprocessError) as exc:
        # Deliberately broad, and deliberately landing on refusal. Anything we
        # failed to understand about the permit is a reason not to trade.
        return PermitDecision(False, f"PERMIT_UNAVAILABLE:{type(exc).__name__}")
