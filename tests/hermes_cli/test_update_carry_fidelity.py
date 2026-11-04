"""Real-git contracts: file replay preserves bytes, types, and rename deletions."""
from pathlib import Path
import subprocess

import pytest

from hermes_cli import update_carry as carry


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True).stdout


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("stashed", [False, True])
def test_file_replay_preserves_snapshot_without_following_links(tmp_path: Path, stashed):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "old.txt").write_bytes(b"old\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "mv", "old.txt", "renamed.txt")
    (repo / "blob.bin").write_bytes(b"\x00binary\xff\n")
    (repo / "notes.txt").write_bytes(b"Binary files is ordinary prose\n")
    (repo / "empty.txt").touch()
    (repo / "unicode-雪\nline.txt").write_bytes(b"line\rbreak\xff\n")
    (repo / "run.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
    (repo / "run.sh").chmod(0o755)
    outside = tmp_path / "private.txt"
    outside.write_bytes(b"must not be copied\n")
    (repo / "link").symlink_to(outside)
    if stashed:
        git(repo, "stash", "push", "-u", "-m", "parked")
    before = git(repo, "status", "--porcelain", "-z")
    plan = carry.plan_repo(repo, stashes=("stash@{0}",) if stashed else ())
    dest = tmp_path / "integration"
    carry.apply_in_worktree(repo, plan, dest, "carry")
    assert (dest / "blob.bin").read_bytes() == b"\x00binary\xff\n"
    assert (dest / "notes.txt").read_bytes() == b"Binary files is ordinary prose\n"
    assert (dest / "empty.txt").read_bytes() == b""
    assert (dest / "unicode-雪\nline.txt").read_bytes() == b"line\rbreak\xff\n"
    assert (dest / "link").is_symlink()
    assert (dest / "link").readlink() == outside
    assert (dest / "run.sh").stat().st_mode & 0o111
    assert not (dest / "old.txt").exists()
    assert (dest / "renamed.txt").read_bytes() == b"old\n"
    assert git(repo, "status", "--porcelain", "-z") == before
    if stashed:
        assert b"parked" in git(repo, "stash", "list")


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("stashed", [False, True])
def test_tracked_modes_and_non_utf8_text_are_preserved(tmp_path, stashed):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "mode.sh").write_bytes(b"same bytes\n")
    (repo / "bytes.txt").write_bytes(b"before\n")
    (repo / "link").symlink_to("before")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "mode.sh").chmod(0o755)
    (repo / "bytes.txt").write_bytes(b"after\xff\r\n")
    (repo / "link").unlink()
    (repo / "link").symlink_to("after")
    if stashed:
        git(repo, "stash", "push", "-u", "-m", "parked")
    plan = carry.plan_repo(repo, stashes=("stash@{0}",) if stashed else ())
    dest = tmp_path / "integration"
    carry.apply_in_worktree(repo, plan, dest, "carry")
    assert (dest / "mode.sh").stat().st_mode & 0o111
    assert (dest / "bytes.txt").read_bytes() == b"after\xff\r\n"
    assert (dest / "link").readlink() == Path("after")
