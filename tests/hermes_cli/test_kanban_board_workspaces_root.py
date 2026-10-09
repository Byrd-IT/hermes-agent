"""A board's persistent ``workspaces_root`` (board.json) is honoured by every process.

Regression: scratch workspaces created under a relocated root
(``HERMES_KANBAN_WORKSPACES_ROOT`` pinned only into the gateway and its
workers) were refused by the completion/archive cleanup guard in any process
without that env var — a terminal ``hermes kanban archive``, the dashboard —
and piled up on disk ("Refusing to remove out-of-scratch workspace"). The
root now lives in ``board.json`` so the guard sees it everywhere, while the
#28818 protections (root itself, source trees, paths outside) still hold.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw

_WORKTREE = Path(__file__).resolve().parents[2]
_KANBAN_ENV = (
    "HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_HOME",
    "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE",
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in _KANBAN_ENV:
        monkeypatch.delenv(var, raising=False)
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "_cached_default_hermes_root", None, raising=False)
    kb._INITIALIZED_PATHS.clear()
    return hermes_home


@pytest.fixture
def ops_root(home, tmp_path):
    """Board ``ops`` whose scratch root is configured outside the kanban home."""
    root = tmp_path / "hermes-workspaces" / "ops"
    kb.create_board("ops")
    kb.write_board_metadata("ops", workspaces_root=str(root))
    return root


def _scratch_task(board: str = "ops") -> tuple[str, Path]:
    with kbc.connect_closing(board=board) as conn:
        tid = kb.create_task(conn, title="scratch work", board=board)
        ws = kbw.resolve_workspace(kb.get_task(conn, tid), board=board)
        kbw.set_workspace_path(conn, tid, ws)
    (ws / "out.txt").write_text("x", encoding="utf-8")
    return tid, ws


def _set_path(tid: str, path: Path, board: str = "ops") -> None:
    with kbc.connect_closing(board=board) as conn:
        kbw.set_workspace_path(conn, tid, path)


def test_board_json_root_is_the_scratch_root_without_env(ops_root):
    assert kb.workspaces_root(board="ops") == ops_root
    assert kb.read_board_metadata("ops")["workspaces_root"] == str(ops_root)
    # Other boards keep their built-in root.
    assert kb.workspaces_root(board="default") == kb.kanban_home() / "kanban" / "workspaces"


def test_env_pin_still_wins_over_board_json(ops_root, tmp_path, monkeypatch):
    pinned = tmp_path / "pinned"
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(pinned))
    assert kb.workspaces_root() == pinned


def test_complete_removes_configured_root_scratch_without_env_pin(ops_root):
    tid, ws = _scratch_task()
    assert ws.parent == ops_root
    assert "HERMES_KANBAN_WORKSPACES_ROOT" not in os.environ
    with kbc.connect_closing(board="ops") as conn:
        assert kb.complete_task(conn, tid, result="done")
    assert not ws.exists()
    assert ops_root.is_dir(), "the root itself is never removed"


def test_archive_removes_configured_root_scratch_without_env_pin(ops_root):
    tid, ws = _scratch_task()
    with kbc.connect_closing(board="ops") as conn:
        assert kb.archive_task(conn, tid)
    assert not ws.exists()


def test_root_itself_is_never_managed(ops_root):
    tid, ws = _scratch_task()
    _set_path(tid, ops_root)
    with kbc.connect_closing(board="ops") as conn:
        assert kb.complete_task(conn, tid, result="done")
    assert ops_root.is_dir() and ws.is_dir()


def test_default_workdir_source_tree_under_root_is_refused(ops_root):
    """#28818: a board ``default_workdir`` inside the configured root is a source
    tree, so neither it, anything inside it, nor anything containing it goes."""
    src = ops_root / "repo"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "keep.py").write_text("keep", encoding="utf-8")
    kb.write_board_metadata("ops", default_workdir=str(src))
    for target in (src, src / "pkg"):
        tid, _ = _scratch_task()
        _set_path(tid, target)
        with kbc.connect_closing(board="ops") as conn:
            assert kb.complete_task(conn, tid, result="done")
        assert (src / "pkg" / "keep.py").is_file()
    assert not kbw._is_managed_scratch_path(ops_root)
    # Ordinary task dirs beside the source tree are still managed.
    tid, ws = _scratch_task()
    with kbc.connect_closing(board="ops") as conn:
        assert kb.complete_task(conn, tid, result="done")
    assert not ws.exists()


def test_path_outside_every_root_is_refused(ops_root, tmp_path):
    outside = tmp_path / "hermes-workspaces" / "repos" / "project"
    outside.mkdir(parents=True)
    tid, _ = _scratch_task()
    _set_path(tid, outside)
    with kbc.connect_closing(board="ops") as conn:
        assert kb.complete_task(conn, tid, result="done")
    assert outside.is_dir()


@pytest.mark.parametrize("bad", ["relative/dir", "/", "HOME", "KANBAN", "KANBAN_SUB"])
def test_unsafe_roots_are_rejected_on_write_and_ignored_when_hand_edited(home, tmp_path, bad):
    value = {
        "HOME": str(tmp_path),
        "KANBAN": str(home),
        "KANBAN_SUB": str(home / "kanban"),
    }.get(bad, bad)
    kb.create_board("ops")
    with pytest.raises(ValueError):
        kb.write_board_metadata("ops", workspaces_root=value)
    meta_path = kb.board_metadata_path("ops")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["workspaces_root"] = value
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    assert kb.workspaces_root(board="ops") == kb.board_dir("ops") / "workspaces"
    if Path(value).is_absolute():
        victim = Path(value) / "victim-dir"
        assert not kbw._is_managed_scratch_path(victim)


def test_export_does_not_carry_the_machine_local_root(ops_root, tmp_path):
    from hermes_cli import kanban_transfer
    import tarfile

    archive = tmp_path / "ops.tar.gz"
    kanban_transfer.export_board("ops", str(archive))
    with tarfile.open(archive) as tar:
        member = next(m for m in tar.getmembers() if m.name.endswith("board.json"))
        meta = json.loads(tar.extractfile(member).read())
    assert "workspaces_root" not in meta


def _cli(args: list[str], hermes_home: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    """``hermes kanban …`` in a fresh process carrying NO kanban env pin."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_KANBAN_")}
    env.update(PYTHONPATH=str(_WORKTREE), HERMES_HOME=str(hermes_home), HOME=str(tmp_path))
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", *args],
        env=env, capture_output=True, text=True, cwd=str(_WORKTREE), timeout=60,
    )


def test_cli_set_root_then_archive_in_unpinned_process(home, tmp_path):
    root = tmp_path / "hermes-workspaces" / "ops"
    assert _cli(["boards", "create", "ops"], home, tmp_path).returncode == 0
    r = _cli(["boards", "set-workspaces-root", "ops", str(root)], home, tmp_path)
    assert r.returncode == 0, r.stderr
    r = _cli(["boards", "set-workspaces-root", "ops", "relative"], home, tmp_path)
    assert r.returncode == 2 and "absolute" in r.stderr
    kb._INITIALIZED_PATHS.clear()
    tid, ws = _scratch_task()
    assert ws.parent == root
    r = _cli(["--board", "ops", "archive", tid], home, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "Refusing to remove" not in r.stderr
    assert not ws.exists()


# --- Overlapping managed roots -------------------------------------------
# Board roots may nest (board a's root an ancestor of board b's) or overlap the
# env pin. A path equal to ANY managed root, or containing one, must never be
# managed, even though another (outer) root contains it.


@pytest.fixture
def nested_roots(home, tmp_path):
    """Board ``a`` root ``T/scratch``; board ``b`` root ``T/scratch/group/b``,
    holding another task's dir with a marker file."""
    a_root = tmp_path / "T" / "scratch"
    b_root = a_root / "group" / "b"
    for slug, root in (("a", a_root), ("b", b_root)):
        kb.create_board(slug)
        kb.write_board_metadata(slug, workspaces_root=str(root))
    marker = b_root / "t_other" / "keep.txt"
    marker.parent.mkdir(parents=True)
    marker.write_text("keep", encoding="utf-8")
    return a_root, b_root, marker


def test_nested_configured_root_and_its_ancestors_are_not_managed(nested_roots):
    a_root, b_root, _ = nested_roots
    for target in (b_root, b_root.parent, a_root):
        assert kbw._managed_scratch_path_info(target) == (False, None), target
    # Lexical aliases of the inner root.
    assert not kbw._is_managed_scratch_path(b_root / "t_other" / "..")
    assert not kbw._is_managed_scratch_path(Path(str(b_root) + "/"))
    # Ordinary task dirs: the innermost root owns the path.
    assert kbw._managed_scratch_path_info(b_root / "t_other") == (True, "b")
    assert kbw._managed_scratch_path_info(a_root / "t_x") == (True, "a")
    assert kbw._managed_scratch_path_info(b_root.parent / "t_y") == (True, "a")


def test_symlink_aliases_of_a_nested_root_are_not_managed(nested_roots, tmp_path):
    a_root, b_root, _ = nested_roots
    # A link inside the outer root pointing at the inner root / its parent.
    to_b = a_root / "link-to-b"
    to_b.symlink_to(b_root, target_is_directory=True)
    to_group = a_root / "link-to-group"
    to_group.symlink_to(b_root.parent, target_is_directory=True)
    for target in (to_b, to_group, to_group / "b"):
        assert not kbw._is_managed_scratch_path(target), target
    # The inner root configured through a symlinked spelling.
    real = tmp_path / "real-c"
    real.mkdir()
    link = a_root / "c-link"
    link.symlink_to(real, target_is_directory=True)
    kb.create_board("c")
    kb.write_board_metadata("c", workspaces_root=str(link))
    for target in (link, real):
        assert not kbw._is_managed_scratch_path(target), target
    assert kbw._managed_scratch_path_info(link / "t_z") == (True, "c")


@pytest.mark.parametrize("env_side", ["outer", "inner"])
def test_env_root_overlapping_a_configured_root(home, tmp_path, monkeypatch, env_side):
    outer = tmp_path / "W"
    inner = outer / "ops"
    env_root, cfg_root = (outer, inner) if env_side == "outer" else (inner, outer)
    kb.create_board("ops")
    kb.write_board_metadata("ops", workspaces_root=str(cfg_root))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(env_root))
    for target in (outer, inner):
        assert not kbw._is_managed_scratch_path(target), target
    assert kbw._is_managed_scratch_path(inner / "t_1")
    assert kbw._is_managed_scratch_path(outer / "t_2")


@pytest.mark.parametrize("op", ["complete", "archive"])
@pytest.mark.parametrize("which", ["inner_root", "inner_parent", "outer_root"])
def test_complete_and_archive_preserve_nested_root_marker(nested_roots, op, which):
    a_root, b_root, marker = nested_roots
    target = {"inner_root": b_root, "inner_parent": b_root.parent, "outer_root": a_root}[which]
    tid, own = _scratch_task("a")
    _set_path(tid, target, board="a")
    with kbc.connect_closing(board="a") as conn:
        if op == "complete":
            assert kb.complete_task(conn, tid, result="done")
        else:
            assert kb.archive_task(conn, tid)
    assert marker.is_file()
    assert own.is_dir()


@pytest.mark.parametrize("op", ["complete", "archive"])
def test_ordinary_task_dirs_still_cleaned_with_nested_roots(nested_roots, op):
    _, b_root, marker = nested_roots
    for board in ("a", "b"):
        tid, ws = _scratch_task(board)
        with kbc.connect_closing(board=board) as conn:
            if op == "complete":
                assert kb.complete_task(conn, tid, result="done")
            else:
                assert kb.archive_task(conn, tid)
        assert not ws.exists(), (board, ws)
    assert marker.is_file() and b_root.is_dir()
