"""Behaviour contracts for carry-reapply. Real git fixtures, no network."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli.update_carry import (
    CarryError,
    apply_in_worktree,
    carry_mode,
    decide,
    plan_repo,
    redact,
    verify_accounted,
    _reject,
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


def _init(tmp: Path) -> Path:
    repo = tmp / "src"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Dev")
    (repo / "clean.txt").write_text("base\n", encoding="utf-8-sig")
    (repo / "conflict.txt").write_text("base\n", encoding="utf-8-sig")
    (repo / "equiv.txt").write_text("base\n", encoding="utf-8-sig")
    (repo / "extra.txt").write_text("base\n", encoding="utf-8-sig")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def _commit(repo: Path, path: str, text: str, message: str) -> str:
    (repo / path).write_text(text, encoding="utf-8-sig")
    _git(repo, "add", path)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def test_decide_merged_open_conflict_and_no_pr():
    dropped = decide({"patch_equivalent": True, "apply_check": "conflict", "upstream_pr": None})
    assert dropped["action"] == "drop"
    assert "no_upstream_pr" not in dropped["flags"]

    opened = decide({
        "patch_equivalent": False, "apply_check": "clean",
        "upstream_pr": {"state": "OPEN", "number": 1}, "pr_match": "sha",
    })
    assert opened["action"] == "reapply"
    assert opened["reason"] == "still-open"
    assert "no_upstream_pr" not in opened["flags"]

    conflicted = decide({"patch_equivalent": False, "apply_check": "conflict"})
    assert conflicted["action"] == "conflict"
    assert "no_upstream_pr" in conflicted["flags"]

    unknown = decide({"pr_lookup": "failed", "apply_check": "clean"})
    assert unknown["action"] == "reapply"
    assert "pr_lookup_failed" in unknown["flags"]
    assert "no_upstream_pr" not in unknown["flags"]

    closed = decide({
        "apply_check": "clean", "upstream_pr": {"state": "CLOSED"}, "pr_match": "sha",
    })
    assert closed["action"] == "reapply"
    assert "pr_closed_unmerged" in closed["flags"]


def test_carry_mode_defaults_to_dry_run():
    assert carry_mode({}) == "dry-run"
    assert carry_mode({"carry_reapply": "off"}) == "off"
    assert carry_mode({"carry_reapply": "apply"}) == "apply"
    assert carry_mode({"carry_reapply": "nope"}) == "dry-run"


def test_redact_hides_credential_shapes():
    raw = (
        "Authorization: Bearer abcdefghijklmnop "
        "https://user:pass@example.test/x "
        "X-Api-Key: secret-value "
        "https://example.test/q?api_key=query-secret"
    )
    masked = redact(raw)
    assert "abcdefghijklmnop" not in masked
    assert "user:pass" not in masked
    assert "secret-value" not in masked
    assert "query-secret" not in masked
    assert "[REDACTED]" in masked


def test_reject_reset_and_stash_drop():
    with pytest.raises(CarryError):
        _reject(["reset", "--hard", "HEAD"])
    with pytest.raises(CarryError):
        _reject(["stash", "drop", "stash@{0}"])
    with pytest.raises(CarryError):
        _reject(["push", "origin", "main"])


def test_plan_and_worktree_replay_leaves_source_untouched(tmp_path: Path):
    repo = _init(tmp_path)
    clean = _commit(repo, "clean.txt", "local-clean\n", "carry clean")
    conflict = _commit(repo, "conflict.txt", "local-conflict\n", "carry conflict")
    equiv = _commit(repo, "equiv.txt", "local-equiv\n", "carry equivalent")
    # Upstream has the equivalent patch under a different sha, plus a conflicting edit.
    _git(repo, "branch", "upstream-advance", "HEAD~3")
    _git(repo, "checkout", "upstream-advance")
    _commit(repo, "equiv.txt", "local-equiv\n", "upstream equivalent")
    _commit(repo, "conflict.txt", "upstream-conflict\n", "upstream conflict")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "main")
    (repo / "extra.txt").write_text("dirty-ok\n", encoding="utf-8-sig")
    (repo / "conflict.txt").write_text("dirty-bad\n", encoding="utf-8-sig")
    source_head = _git(repo, "rev-parse", "HEAD")
    source_status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout

    plan = plan_repo(repo, upstream="origin/main")
    by_id = {item["identity"]: item for item in plan["items"]}
    assert by_id[equiv]["action"] == "drop"
    assert by_id[clean]["action"] == "reapply"
    assert by_id[conflict]["action"] == "conflict"
    assert by_id["worktree:extra.txt"]["action"] == "reapply"
    assert by_id["worktree:conflict.txt"]["action"] == "conflict"
    assert verify_accounted(repo, plan) == []

    dest = tmp_path / "integration"
    applied = apply_in_worktree(repo, plan, dest, "carry-reapply-test")
    assert _git(repo, "rev-parse", "HEAD") == source_head
    after_status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    assert after_status == source_status
    assert (dest / "clean.txt").read_text(encoding="utf-8-sig") == "local-clean\n"
    assert (dest / "extra.txt").read_text(encoding="utf-8-sig") == "dirty-ok\n"
    assert (dest / "conflict.txt").read_text(encoding="utf-8-sig") == "upstream-conflict\n"
    assert (dest / "equiv.txt").read_text(encoding="utf-8-sig") == "local-equiv\n"
    assert any(row["applied"] == "conflict" for row in applied["results"])
    with pytest.raises(CarryError):
        apply_in_worktree(repo, plan, repo / "nested", "nope")
