"""Tests must never resolve the LIVE kanban DB.

Regression: a fixture built HERMES_HOME with ``tempfile.mkdtemp()`` while the
agent-exported ``TMPDIR`` sat inside ``~/.hermes/profiles/<p>/cache/scratch``;
``get_default_hermes_root()`` folded that home back to the real root and the
fixture's alpha/beta cards were written to the live ops board. The resolver
itself now refuses a live path under test context, so no fixture shape
(module eviction, re-import, custom homes) can reach the real board.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from hermes_cli import kanban_db
from hermes_cli import kanban_db_connect


def _real_root() -> Path:
    root = kanban_db._live_kanban_root()
    if root is None:
        pytest.skip("no passwd entry for the current user")
    return root


def test_home_nested_under_real_root_is_refused(monkeypatch):
    """The exact escape: HERMES_HOME under <root>/profiles/<p>/cache/scratch."""
    home = _real_root() / "profiles" / "no-such-profile" / "cache" / "scratch" / "fixture_home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    with pytest.raises(RuntimeError, match="LIVE kanban DB"):
        kanban_db.kanban_db_path()


@pytest.mark.parametrize("rel", ["kanban.db", "kanban/boards/ops/kanban.db"])
def test_pinned_live_db_is_refused(monkeypatch, rel):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(_real_root() / rel))
    with pytest.raises(RuntimeError, match="LIVE kanban DB"):
        kanban_db.kanban_db_path()


def test_explicit_live_db_path_is_refused_before_touching_disk():
    live = _real_root() / "kanban" / "boards" / "ops" / "kanban.db"
    with pytest.raises(RuntimeError):
        kanban_db_connect.connect(db_path=live)


def test_bypass_env_allows_resolution(monkeypatch):
    live = _real_root() / "kanban" / "boards" / "ops" / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(live))
    monkeypatch.setenv("HERMES_STATE_DB_GUARD_BYPASS", "1")
    assert kanban_db.kanban_db_path() == live


def test_tmp_path_home_resolves_and_writes(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    path = kanban_db.kanban_db_path()
    assert not path.resolve().is_relative_to(_real_root())
    with kanban_db_connect.connect_closing() as conn:
        tid = kanban_db.create_task(conn, title="t", assignee="alpha")
    with kanban_db_connect.connect_closing() as conn:
        assert conn.execute("select count(*) from tasks where id=?", (tid,)).fetchone()[0] == 1


def test_session_tempdir_is_outside_real_root():
    """conftest moves an agent-exported TMPDIR out of ~/.hermes, so
    tempfile.mkdtemp()-built homes can't fold back to the live root."""
    assert not Path(tempfile.gettempdir()).resolve().is_relative_to(_real_root())
