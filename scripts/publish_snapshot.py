#!/usr/bin/env python3
"""Publish the public snapshot without mutating the trading checkout.

The paper cycle owns this checkout. This publisher verifies its narrow source
state, then commits from a disposable clone, so a GitHub failure cannot leave
the trading process with a staged file, an ahead branch, or a dirty worktree.
"""
from __future__ import annotations

import os
import signal
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = "docs/snapshot.json"
GIT_TIMEOUT_SECONDS = 30
GIT_TERMINATION_GRACE_SECONDS = 2


class SnapshotSyncError(RuntimeError):
    """The repository is not in the narrow state safe for auto-publishing."""


def _stop_process_group(pid: int, termination_signal: int) -> None:
    """Stop a Git process group unless it has already exited on its own."""
    try:
        os.killpg(pid, termination_signal)
    except ProcessLookupError:
        pass


def git(*args: str, cwd: Path = ROOT) -> str:
    """Run a Git command without changing the live checkout by default."""
    process = subprocess.Popen(
        ["git", *args], cwd=cwd, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _stop_process_group(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=GIT_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _stop_process_group(process.pid, signal.SIGKILL)
            process.communicate()
        raise
    if process.returncode:
        raise subprocess.CalledProcessError(
            process.returncode, ["git", *args], stdout, stderr)
    return stdout


def _only_generated_snapshot_changed(status: str) -> bool:
    """Accept only the one unstaged tracked runtime artifact."""
    lines = [line for line in status.splitlines() if line]
    if not lines:
        return False
    if lines != [f" M {SNAPSHOT}"]:
        raise SnapshotSyncError(
            "refusing automatic publish: worktree contains changes other than "
            f"the generated {SNAPSHOT}")
    return True


def _remote_head() -> str:
    """Return origin/main's advertised SHA without updating local refs."""
    fields = git("ls-remote", "origin", "refs/heads/main").split()
    if not fields:
        raise SnapshotSyncError("origin/main is unavailable")
    return fields[0]


def _origin_url() -> str:
    """Resolve the configured remote name before making a disposable clone."""
    url = git("remote", "get-url", "origin").strip()
    if not url:
        raise SnapshotSyncError("origin URL is unavailable")
    return url


def require_snapshot_only_remote_history(local_head: str, remote_head: str,
                                         clone: Path) -> None:
    """Permit only remote commits that changed the generated snapshot."""
    try:
        git("merge-base", "--is-ancestor", local_head, remote_head, cwd=clone)
    except subprocess.CalledProcessError as exc:
        raise SnapshotSyncError(
            "refusing automatic publish: origin/main does not descend from "
            "the live checkout") from exc

    commits = git("rev-list", f"{local_head}..{remote_head}", cwd=clone).splitlines()
    for commit in commits:
        changed = [path for path in git(
            "diff-tree", "--no-commit-id", "--name-only", "-r", "-m", commit,
            cwd=clone
        ).splitlines() if path]
        if any(path != SNAPSHOT for path in changed):
            raise SnapshotSyncError(
                "refusing automatic publish: origin/main contains non-snapshot "
                "changes")


def publish_from_disposable_clone(local_head: str, remote_head: str) -> None:
    """Copy and publish the snapshot from an isolated short-lived checkout."""
    source = ROOT / SNAPSHOT
    with tempfile.TemporaryDirectory(prefix="sentinel-snapshot-publish-") as raw:
        clone = Path(raw) / "checkout"
        git("clone", "--quiet", "--branch", "main",
            _origin_url(), str(clone))
        if git("rev-parse", "HEAD", cwd=clone).strip() != remote_head:
            raise SnapshotSyncError("origin/main changed while preparing snapshot")
        require_snapshot_only_remote_history(local_head, remote_head, clone)

        shutil.copyfile(source, clone / SNAPSHOT)
        git("add", "--", SNAPSHOT, cwd=clone)
        git("commit", "-m", "Publish runtime snapshot", cwd=clone)
        git("push", "origin", "HEAD:main", cwd=clone)

    if _remote_head() == remote_head:
        raise SnapshotSyncError("snapshot push did not advance origin/main")


def publish() -> bool:
    """Publish a fresh public snapshot, or safely do nothing."""
    status = git("status", "--porcelain=v1", "--untracked-files=all")
    if not _only_generated_snapshot_changed(status):
        print("snapshot sync: no generated snapshot change")
        return False

    if git("branch", "--show-current").strip() != "main":
        raise SnapshotSyncError("refusing automatic publish outside main")

    local_head = git("rev-parse", "HEAD").strip()
    publish_from_disposable_clone(local_head, _remote_head())
    print("snapshot sync: published from an isolated checkout")
    return True


def main() -> int:
    try:
        publish()
    except (SnapshotSyncError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as exc:
        print(f"snapshot sync: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
