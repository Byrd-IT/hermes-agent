"""Behaviour contracts for the update hooks of carry-reapply. Real git fixtures, no network."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import update_carry
from hermes_cli.update_carry import (
    CarryError,
    _cherry_pick,
    _reject,
    _status_entries,
    reapply_after_diverged_reset,
    reapply_parked_stash,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _init(tmp: Path, name: str = "src") -> Path:
    repo = tmp / name
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Dev")
    for file in ("clean.txt", "conflict.txt", "up.txt"):
        (repo / file).write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    return repo


def _commit(repo: Path, path: str, text: str, message: str) -> str:
    (repo / path).write_text(text, encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _diverged(tmp: Path) -> tuple[Path, str]:
    """A checkout the updater already reset onto upstream; returns it and the parked tip."""
    repo = _init(tmp)
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _commit(repo, "clean.txt", "local-clean\n", "carry clean")
    _commit(repo, "conflict.txt", "local-conflict\n", "carry conflict")
    tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-B", "upstream-advance", "origin/main")
    _commit(repo, "conflict.txt", "upstream-conflict\n", "upstream conflict")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-q", "-B", "main", "origin/main")
    return repo, tip


def _mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setattr(update_carry, "_updates_section", lambda: {"carry_reapply": mode})


def test_staged_rename_reports_the_new_path(tmp_path: Path):
    repo = _init(tmp_path)
    _git(repo, "mv", "clean.txt", "renamed.txt")
    entries = _status_entries(repo, git_bin="git")
    assert [path for xy, path in entries if xy.startswith("R")] == ["renamed.txt"]


def test_dry_run_hook_reports_and_leaves_head_alone(tmp_path: Path, monkeypatch, capsys):
    repo, tip = _diverged(tmp_path)
    _mode(monkeypatch, "dry-run")
    head = _git(repo, "rev-parse", "HEAD")
    reapply_after_diverged_reset(["git"], repo, pre_pull_sha=tip, upstream_ref="origin/main")
    out = capsys.readouterr().out
    assert "carry clean" in out and "carry conflict" in out
    assert _git(repo, "rev-parse", "HEAD") == head
    assert (repo / "clean.txt").read_text(encoding="utf-8") == "base\n"


def test_apply_hook_replays_clean_and_aborts_the_conflicting_pick(tmp_path: Path, monkeypatch):
    repo, tip = _diverged(tmp_path)
    _mode(monkeypatch, "apply")
    reapply_after_diverged_reset(["git"], repo, pre_pull_sha=tip, upstream_ref="origin/main")
    assert (repo / "clean.txt").read_text(encoding="utf-8") == "local-clean\n"
    assert (repo / "conflict.txt").read_text(encoding="utf-8") == "upstream-conflict\n"
    assert _git(repo, "status", "--porcelain") == ""
    assert not (repo / ".git" / "CHERRY_PICK_HEAD").exists()


def test_orphan_divergence_is_skipped_with_one_line(tmp_path: Path, monkeypatch, capsys):
    repo = _init(tmp_path)
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-q", "--orphan", "other")
    _commit(repo, "orphan.txt", "x\n", "unrelated history")
    tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-B", "main", "origin/main")
    _mode(monkeypatch, "dry-run")
    reapply_after_diverged_reset(["git"], repo, pre_pull_sha=tip, upstream_ref="origin/main")
    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["carry-reapply: no common history with the update target; skipped"]


def test_planner_failure_never_fails_the_update(tmp_path: Path, monkeypatch, capsys):
    repo, tip = _diverged(tmp_path)
    _mode(monkeypatch, "dry-run")

    def boom(*args, **kwargs):
        raise RuntimeError("planner exploded")

    monkeypatch.setattr(update_carry, "plan_repo", boom)
    reapply_after_diverged_reset(["git"], repo, pre_pull_sha=tip, upstream_ref="origin/main")
    assert "the update result is unchanged" in capsys.readouterr().out


def test_stash_hook_in_apply_mode_keeps_the_stash(tmp_path: Path, monkeypatch):
    repo = _init(tmp_path)
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "clean.txt").write_text("stashed\n", encoding="utf-8")
    _git(repo, "stash", "push", "-m", "hermes-update-autostash-test")
    _mode(monkeypatch, "apply")
    reapply_parked_stash(["git"], repo, "stash@{0}")
    assert (repo / "clean.txt").read_text(encoding="utf-8") == "stashed\n"
    assert "hermes-update-autostash-test" in _git(repo, "stash", "list")


def test_reject_sees_through_leading_config_pairs():
    with pytest.raises(CarryError):
        _reject(["-c", "core.x=1", "reset", "--hard"])
    with pytest.raises(CarryError):
        _reject(["-c", "core.x=1"])
    _reject(["-c", "user.name=a", "-c", "user.email=b", "commit", "-m", "m"])


def test_conflict_in_a_file_named_empty_is_still_a_conflict(tmp_path: Path):
    repo = _init(tmp_path)
    (repo / "test_empty.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add file")
    _git(repo, "checkout", "-q", "-b", "side")
    sha = _commit(repo, "test_empty.txt", "side\n", "side edit")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "test_empty.txt", "main\n", "main edit")
    assert _cherry_pick(repo, sha, git_bin="git") == "conflict"
    assert _git(repo, "status", "--porcelain") == ""


def test_carry_mode_is_registered_and_read_through_load_config(tmp_path: Path, monkeypatch):
    from hermes_cli.config import DEFAULT_CONFIG
    from hermes_cli.web_server_config import CONFIG_SCHEMA

    assert DEFAULT_CONFIG["updates"]["carry_reapply"] == "dry-run"
    assert CONFIG_SCHEMA["updates.carry_reapply"]["type"] == "select"
    assert update_carry.carry_mode() == "dry-run"

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("updates:\n  carry_reapply: apply\n", encoding="utf-8")
    assert update_carry.carry_mode() == "apply"
