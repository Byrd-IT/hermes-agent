"""The scratch-workspace repo warning reads card text only; it never probes the filesystem."""

from pathlib import Path

from tools.kanban_tools import _scratch_repo_workspace_warning as warn


def test_branch_work_on_a_scratch_card_warns_and_echoes_the_authors_path(tmp_path):
    out = warn("Fix", f"Branch off main in {tmp_path}/repo and commit.", "scratch")
    assert out and f"{tmp_path}/repo" in out and "workspace_kind='worktree'" in out


def test_no_filesystem_probe_of_paths_in_card_text(monkeypatch):
    def boom(self, *a, **k):
        raise AssertionError("card text must not be stat()ed")
    for name in ("is_dir", "exists", "is_file", "stat"):
        monkeypatch.setattr(Path, name, boom)
    assert warn("Fix", "Branch off main in /mnt/some/network/share/repo.", "scratch")


def test_a_path_alone_is_not_a_repo_task():
    assert warn("Look at logs", "See /var/log and /tmp for output.", "scratch") is None


def test_worktree_and_dir_cards_never_warn():
    assert warn("Fix", "Branch off main in /srv/repo", "worktree") is None
    assert warn("Fix", "Branch off main in /srv/repo", "dir") is None


def test_branch_hint_without_a_path_still_warns():
    out = warn("Fix", "commit to the release branch", "scratch")
    assert out and "workspace_kind='worktree'" in out
