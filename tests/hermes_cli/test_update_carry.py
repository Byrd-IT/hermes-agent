"""Behaviour contracts for carry-reapply. Real git fixtures, no network."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from hermes_cli.update_carry import (
    CarryError,
    apply_in_worktree,
    apply_pr_state,
    autostash_refs,
    carry_mode,
    decide,
    plan_repo,
    redact,
    verify_accounted,
    _reject,
)
from hermes_cli import update_carry


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


def test_worktree_bytes_matching_upstream_are_dropped(tmp_path: Path):
    repo = _init(tmp_path)
    _commit(repo, "clean.txt", "local\n", "local edit")
    _git(repo, "branch", "up", "HEAD~1")
    _git(repo, "checkout", "up")
    _commit(repo, "clean.txt", "upstream\n", "upstream edit")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "main")
    (repo / "clean.txt").write_text("upstream\n", encoding="utf-8-sig")
    plan = plan_repo(repo, upstream="origin/main")
    item = next(row for row in plan["items"] if row["identity"] == "worktree:clean.txt")
    assert item["action"] == "drop"
    assert item["reason"] == "merged-upstream"


def test_stash_bytes_matching_upstream_are_dropped(tmp_path: Path):
    repo = _init(tmp_path)
    (repo / "clean.txt").write_text("stashed\n", encoding="utf-8-sig")
    _git(repo, "stash", "push", "-m", "park", "--", "clean.txt")
    _git(repo, "checkout", "-B", "advance")
    _commit(repo, "clean.txt", "stashed\n", "upstream has it")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "main")
    plan = plan_repo(repo, upstream="origin/main", stashes=("stash@{0}",))
    item = next(row for row in plan["items"] if str(row["identity"]).startswith("stash:"))
    assert item["action"] == "drop"
    assert item["reason"] == "merged-upstream"
    assert "stash@{0}" in _git(repo, "stash", "list")


def test_log_filter_redacts_userinfo(caplog):
    with caplog.at_level(logging.ERROR):
        try:
            raise CarryError("https://user:pass@example.test/x")
        except CarryError:
            update_carry.logger.exception("carry failed")
    assert "user:pass" not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_untracked_new_file_is_reapplied(tmp_path: Path):
    repo = _init(tmp_path)
    (repo / "newfeature.py").write_text("print('carried')\n", encoding="utf-8")
    plan = plan_repo(repo, upstream="origin/main")
    item = next(row for row in plan["items"] if row["identity"] == "worktree:newfeature.py")
    assert item["action"] == "reapply"
    assert item["reason"] != "empty"
    dest = tmp_path / "integration"
    apply_in_worktree(repo, plan, dest, "carry-new")
    assert (dest / "newfeature.py").read_text(encoding="utf-8") == "print('carried')\n"


def test_untracked_path_with_space_is_not_empty(tmp_path: Path):
    repo = _init(tmp_path)
    (repo / "my file.txt").write_text("spaced\n", encoding="utf-8")
    plan = plan_repo(repo, upstream="origin/main")
    item = next(row for row in plan["items"] if "my file.txt" in row["identity"])
    assert '"' not in item["identity"]
    assert item["action"] == "reapply"
    assert item["reason"] != "empty"


def test_stash_untracked_file_is_carried(tmp_path: Path):
    repo = _init(tmp_path)
    (repo / "clean.txt").write_text("edit\n", encoding="utf-8")
    (repo / "brandnew.py").write_text("x=1\n", encoding="utf-8")
    _git(repo, "stash", "push", "-u", "-m", "hermes-update-autostash-x")
    plan = plan_repo(repo, upstream="origin/main", stashes=("stash@{0}",))
    item = next(row for row in plan["items"] if "brandnew.py" in row["identity"])
    assert item["action"] == "reapply"
    dest = tmp_path / "integration"
    apply_in_worktree(repo, plan, dest, "carry-stash-new")
    assert (dest / "brandnew.py").read_text(encoding="utf-8") == "x=1\n"
    assert "stash@{0}" in _git(repo, "stash", "list")


def test_autostash_refs_ignore_position(tmp_path: Path):
    repo = _init(tmp_path)
    (repo / "clean.txt").write_text("auto\n", encoding="utf-8")
    _git(repo, "stash", "push", "-m", "hermes-update-autostash-x")
    (repo / "extra.txt").write_text("newer\n", encoding="utf-8")
    _git(repo, "stash", "push", "-m", "manual-note")
    refs = autostash_refs(repo)
    assert refs == ["stash@{1}"]
    assert "manual-note" not in " ".join(refs)


def test_apply_state_dir_survives_a_changed_carried_set(tmp_path: Path, capsys):
    repo = _init(tmp_path)
    (repo / "clean.txt").write_text("edit1\n", encoding="utf-8")
    source_head = _git(repo, "rev-parse", "HEAD")
    argv = [
        "--repo", str(repo), "--upstream", "origin/main", "--apply",
        "--worktree", str(tmp_path / "wt"), "--state-dir", str(tmp_path / "state"),
        "--quiet",
    ]
    assert update_carry.main(argv) == 0
    assert update_carry.main(argv) == 0
    assert "unchanged" in capsys.readouterr().out
    (repo / "clean.txt").write_text("edit2\n", encoding="utf-8")
    rc = update_carry.main(argv)
    captured = capsys.readouterr()
    assert rc == 0
    assert "already exists" not in captured.err
    found = [
        path for path in tmp_path.iterdir()
        if path.is_dir() and (path / "clean.txt").is_file()
        and (path / "clean.txt").read_text(encoding="utf-8") == "edit2\n"
    ]
    assert found
    assert _git(repo, "rev-parse", "HEAD") == source_head
    branches = _git(repo, "branch")
    assert "carry-reapply" in branches


def test_apply_pr_state_merged_open_and_lookup_failed():
    base = {
        "patch_equivalent": False, "apply_check": "clean", "identity": "abc",
        "files": ["a.txt"], "sha": "abc",
    }
    dropped = apply_pr_state(base, {
        "lookup": "ok", "state": "MERGED", "number": 9, "match": "sha",
    })
    assert dropped["action"] == "drop"
    assert dropped["reason"] == "merged-upstream"
    assert "no_upstream_pr" not in dropped["flags"]

    opened = apply_pr_state(base, {
        "lookup": "ok", "state": "OPEN", "number": 3, "match": "file",
    })
    assert opened["action"] == "reapply"
    assert opened["reason"] == "still-open"
    assert "no_upstream_pr" not in opened["flags"]

    failed = apply_pr_state(base, {"lookup": "failed"})
    assert failed["action"] == "reapply"
    assert "pr_lookup_failed" in failed["flags"]
    assert "no_upstream_pr" not in failed["flags"]
