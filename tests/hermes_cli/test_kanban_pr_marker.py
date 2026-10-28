"""A pr-superseded marker that names the replacement PR keeps the respawn guard on (Byrd-IT carry)."""

from __future__ import annotations

from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


def test_marker_naming_the_replacement_pr_is_itself_the_newest_pr_comment(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kbc.connect_closing(kb.kanban_db_path(board="pr-marker")) as conn:
        tid = kb.create_task(conn, title="superseded with a URL", assignee="worker")
        kb.add_comment(conn, tid, author="worker", body="Opened https://github.com/o/r/pull/1")
        kb.add_comment(conn, tid, author="operator",
                       body="kanban: pr-superseded by https://github.com/o/r/pull/2")
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"
