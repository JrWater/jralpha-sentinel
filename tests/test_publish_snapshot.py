"""The dashboard publisher must never turn a runtime snapshot into a broad push."""
from __future__ import annotations

import subprocess
import signal

import pytest

from scripts import publish_snapshot


def test_publish_refuses_an_unrelated_worktree_change(monkeypatch):
    calls = []

    def fake_git(*args):
        calls.append(args)
        if args[:2] == ("status", "--porcelain=v1"):
            return " M docs/snapshot.json\n M policy/manifest.json\n"
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr(publish_snapshot, "git", fake_git)

    with pytest.raises(publish_snapshot.SnapshotSyncError,
                       match="changes other than"):
        publish_snapshot.publish()

    assert calls == [("status", "--porcelain=v1", "--untracked-files=all")]


def test_publish_delegates_writes_to_an_isolated_clone(monkeypatch):
    calls = []
    published = []

    def fake_git(*args):
        calls.append(args)
        responses = {
            ("status", "--porcelain=v1", "--untracked-files=all"):
                " M docs/snapshot.json\n",
            ("branch", "--show-current"): "main\n",
            ("ls-remote", "origin", "refs/heads/main"): "before\trefs/heads/main\n",
        }
        if args == ("rev-parse", "HEAD"):
            return "before\n"
        if args in responses:
            return responses[args]
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr(publish_snapshot, "git", fake_git)
    monkeypatch.setattr(
        publish_snapshot, "publish_from_disposable_clone",
        lambda local_head, remote_head: published.append((local_head, remote_head)))

    assert publish_snapshot.publish() is True
    assert published == [("before", "before")]
    assert not {"add", "commit", "push"}.intersection(
        command[0] for command in calls)


@pytest.mark.parametrize(
    ("changed_paths", "error"),
    [
        ("docs/snapshot.json\n", None),
        ("docs/snapshot.json\npolicy/manifest.json\npolicy/manifest.json\n",
         "non-snapshot changes"),
    ],
)
def test_remote_history_must_contain_only_snapshot_changes(
        monkeypatch, tmp_path, changed_paths, error):
    clone = tmp_path / "clone"

    def fake_git(*args, cwd=publish_snapshot.ROOT):
        if args == ("merge-base", "--is-ancestor", "local", "remote"):
            return ""
        if args == ("rev-list", "local..remote"):
            return "snapshot-commit\n"
        if args == (
            "diff-tree", "--no-commit-id", "--name-only", "-r", "-m",
            "snapshot-commit",
        ):
            return changed_paths
        raise AssertionError(f"unexpected git command: {args} in {cwd}")

    monkeypatch.setattr(publish_snapshot, "git", fake_git)

    if error:
        with pytest.raises(publish_snapshot.SnapshotSyncError, match=error):
            publish_snapshot.require_snapshot_only_remote_history("local", "remote", clone)
    else:
        publish_snapshot.require_snapshot_only_remote_history("local", "remote", clone)


def test_remote_history_rejects_a_reverted_source_change(tmp_path):
    repo = tmp_path / "repo"

    def run(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args], cwd=repo, check=True, text=True,
            stdout=subprocess.PIPE)
        return completed.stdout.strip()

    repo.mkdir()
    run("init", "--quiet")
    run("config", "user.name", "Sentinel Test")
    run("config", "user.email", "sentinel@example.test")
    (repo / "docs").mkdir()
    (repo / "docs" / "snapshot.json").write_text('{"version": 1}\n')
    (repo / "policy").mkdir()
    (repo / "policy" / "manifest.json").write_text('{"risk": 1}\n')
    run("add", ".")
    run("commit", "--quiet", "-m", "base")
    base = run("rev-parse", "HEAD")

    (repo / "policy" / "manifest.json").write_text('{"risk": 2}\n')
    run("commit", "--quiet", "-am", "source change")
    (repo / "policy" / "manifest.json").write_text('{"risk": 1}\n')
    (repo / "docs" / "snapshot.json").write_text('{"version": 2}\n')
    run("commit", "--quiet", "-am", "revert source and publish snapshot")
    remote = run("rev-parse", "HEAD")

    with pytest.raises(publish_snapshot.SnapshotSyncError,
                       match="non-snapshot changes"):
        publish_snapshot.require_snapshot_only_remote_history(base, remote, repo)


def test_clone_failure_cannot_mutate_the_live_checkout(monkeypatch):
    calls = []

    def fake_git(*args):
        calls.append(args)
        responses = {
            ("status", "--porcelain=v1", "--untracked-files=all"):
                " M docs/snapshot.json\n",
            ("branch", "--show-current"): "main\n",
            ("ls-remote", "origin", "refs/heads/main"): "before\trefs/heads/main\n",
            ("rev-parse", "HEAD"): "before\n",
        }
        return responses[args]

    monkeypatch.setattr(publish_snapshot, "git", fake_git)
    monkeypatch.setattr(
        publish_snapshot, "publish_from_disposable_clone",
        lambda _local_head, _remote_head: (_ for _ in ()).throw(
            publish_snapshot.SnapshotSyncError("isolated push failed")))

    with pytest.raises(publish_snapshot.SnapshotSyncError, match="isolated push failed"):
        publish_snapshot.publish()

    assert not {"add", "commit", "push"}.intersection(
        command[0] for command in calls)


def test_disposable_clone_uses_the_configured_origin_url(monkeypatch):
    commands = []

    def fake_git(*args, cwd=publish_snapshot.ROOT):
        commands.append((args, cwd))
        if args == ("remote", "get-url", "origin"):
            return "git@github.com:JrWater/jralpha-sentinel.git\n"
        raise publish_snapshot.SnapshotSyncError("stop after clone command")

    monkeypatch.setattr(publish_snapshot, "git", fake_git)

    with pytest.raises(publish_snapshot.SnapshotSyncError,
                       match="stop after clone command"):
        publish_snapshot.publish_from_disposable_clone("local", "remote")

    assert commands[0][0] == ("remote", "get-url", "origin")
    assert commands[1][0][0:5] == (
        "clone", "--quiet", "--branch", "main", "git@github.com:JrWater/jralpha-sentinel.git")


def test_publish_treats_a_clean_worktree_as_a_noop(monkeypatch):
    monkeypatch.setattr(
        publish_snapshot, "git",
        lambda *args: "" if args[:2] == ("status", "--porcelain=v1") else
        pytest.fail(f"unexpected git command: {args}"),
    )

    assert publish_snapshot.publish() is False


def test_git_commands_have_a_bounded_timeout(monkeypatch):
    captured = {}

    class FakeProcess:
        pid = 123
        returncode = 0

        def communicate(self, timeout):
            captured["timeout"] = timeout
            return "", ""

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(publish_snapshot.subprocess, "Popen", fake_popen)

    publish_snapshot.git("status")

    assert captured["timeout"] == publish_snapshot.GIT_TIMEOUT_SECONDS
    assert captured["start_new_session"] is True


def test_git_timeout_terminates_the_entire_process_group(monkeypatch):
    calls = []

    class FakeProcess:
        pid = 456

        def communicate(self, timeout):
            calls.append(("communicate", timeout))
            if len(calls) == 1:
                raise subprocess.TimeoutExpired("git clone", timeout)
            self.returncode = -signal.SIGTERM
            return "", ""

    monkeypatch.setattr(publish_snapshot.subprocess, "Popen",
                        lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(publish_snapshot.os, "killpg",
                        lambda pid, sig: calls.append(("killpg", pid, sig)))

    with pytest.raises(subprocess.TimeoutExpired):
        publish_snapshot.git("clone", "origin", "checkout")

    assert ("killpg", 456, signal.SIGTERM) in calls


def test_main_reports_git_timeouts_without_a_traceback(monkeypatch, capsys):
    monkeypatch.setattr(
        publish_snapshot, "publish",
        lambda: (_ for _ in ()).throw(subprocess.TimeoutExpired("git clone", 30)),
    )

    assert publish_snapshot.main() == 1
    assert "timed out" in capsys.readouterr().out
