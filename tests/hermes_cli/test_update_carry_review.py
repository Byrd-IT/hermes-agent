"""Review contracts for atomic rename replay and credential-safe boundaries."""
from pathlib import Path
import subprocess

import pytest

from hermes_cli import update_carry as carry
from tests.hermes_cli.test_update_carry_fidelity import git


@pytest.mark.parametrize("stashed", [False, True])
@pytest.mark.parametrize("late", [False, True])
def test_conflicting_rename_keeps_source_and_independent_change(tmp_path, monkeypatch, stashed, late):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "old.txt").write_bytes(b"carried content\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD").decode().strip()
    if not late:
        (repo / "new.txt").write_bytes(b"upstream content\n")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "upstream obstruction")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "checkout", "--detach", base)
    git(repo, "mv", "old.txt", "new.txt")
    (repo / "independent.txt").write_bytes(b"independent\n")
    if stashed:
        git(repo, "stash", "push", "-u")
    plan = carry.plan_repo(repo, stashes=("stash@{0}",) if stashed else ())
    dest = tmp_path / "integration"
    if late:
        real_apply = carry._apply_patch

        def obstruct(path, patch, **kwargs):
            (path / "new.txt").write_bytes(b"upstream content\n")
            return real_apply(path, patch, **kwargs)

        monkeypatch.setattr(carry, "_apply_patch", obstruct)
    carry.apply_in_worktree(repo, plan, dest, "carry")
    assert (dest / "old.txt").read_bytes() == b"carried content\n"
    assert (dest / "new.txt").read_bytes() == b"upstream content\n"
    assert (dest / "independent.txt").read_bytes() == b"independent\n"


def test_apply_transport_is_byte_exact(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    patch = "diff --git a/a.txt b/a.txt\nnew file mode 100644\n--- /dev/null\n+++ b/a.txt\n@@ -0,0 +1 @@\n+bytes\r\n"
    real_run = subprocess.run
    payloads = []

    def record(*args, **kwargs):
        payloads.append(kwargs.get("input"))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", record)
    assert carry._apply_patch(repo, patch, git_bin="git") == "reapply"
    assert payloads == [patch.encode(), patch.encode()]
    assert (repo / "a.txt").read_bytes() == b"bytes\r\n"


def test_repo_resolution_failure_is_opaque(monkeypatch, capsys):
    secret = "SYNTH-RESOLUTION-NOT-A-REAL-TOKEN-ABC123"

    def fail(_self):
        raise OSError(secret)

    monkeypatch.setattr(Path, "resolve", fail)
    assert carry.main(["--repo", secret]) == 1
    out = capsys.readouterr()
    assert secret not in out.out + out.err
    assert "failed" in out.err
