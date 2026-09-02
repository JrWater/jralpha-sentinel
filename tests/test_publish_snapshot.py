"""The dashboard publisher must never turn a runtime snapshot into a broad push."""
from __future__ import annotations

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
        lambda expected_head: published.append(expected_head))

    assert publish_snapshot.publish() is True
    assert published == ["before"]
    assert not {"add", "commit", "push"}.intersection(
        command[0] for command in calls)


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
        lambda _head: (_ for _ in ()).throw(
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
        publish_snapshot.publish_from_disposable_clone("expected")

    assert commands[0][0] == ("remote", "get-url", "origin")
    assert commands[1][0][0:6] == (
        "clone", "--quiet", "--depth", "1", "--branch", "main")
    assert commands[1][0][6] == "git@github.com:JrWater/jralpha-sentinel.git"


def test_publish_treats_a_clean_worktree_as_a_noop(monkeypatch):
    monkeypatch.setattr(
        publish_snapshot, "git",
        lambda *args: "" if args[:2] == ("status", "--porcelain=v1") else
        pytest.fail(f"unexpected git command: {args}"),
    )

    assert publish_snapshot.publish() is False
