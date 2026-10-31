"""Kanban continuity: prior-run digest, claim-time export, status line, one switch."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
from pathlib import Path

import pytest

import hermes_constants
from hermes_cli import kanban_continuity as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

SECRET = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef"
BEARER = "Bearer abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"
USERINFO = "https://deploy:hunter2hunter2@example.com/repo.git"
APIKEY = "zq9Xv3LmN8pRt2Ws5YbK7cDfGh1JaE4u"        # bare token, only the header / query-string context reveals it
HEADER_CMD = f"curl -H 'X-Api-Key: {APIKEY}' 'https://api.example.com/v1?api_key={APIKEY}&x=1'"
ALL_SECRETS = (SECRET, "abcdefghijklmnopqrstuvwxyz0123456789ABCDEF", "hunter2hunter2", APIKEY, "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    (root / "profiles" / "coder").mkdir(parents=True)
    (root / "config.yaml").write_text("{}\n", encoding="utf-8")
    (root / "profiles" / "coder" / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kc, "_cfg_cache", None)
    monkeypatch.setattr(kc, "_checked", {})
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda **_k: root)
    kb.init_db()
    return root


@pytest.fixture
def on(home, monkeypatch):
    monkeypatch.setattr(kc, "enabled", lambda: True)


def _state_db(root: Path, sid: str, msgs: list[tuple], profile="coder") -> str:
    path = root / "profiles" / profile / "state.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, tool_call_count INT, message_count INT)")
    c.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
              "content TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL, active INT DEFAULT 1)")
    now = time.time()
    n_tools = 0
    for i, (role, content, calls) in enumerate(msgs):
        tc = json.dumps([{"function": {"name": n, "arguments": json.dumps(a)}} for n, a in calls]) if calls else None
        n_tools += len(calls or [])
        c.execute("INSERT INTO messages (session_id, role, content, tool_calls, timestamp) VALUES (?,?,?,?,?)",
                  (sid, role, content, tc, now + i))
    c.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?)", (sid, n_tools, len(msgs)))
    c.commit()
    c.close()
    return str(path)


def _convo():
    return [
        ("user", "work kanban task", None),
        ("assistant", "Plan: patch the dispatcher then run tests.", [("write_file", {"path": "/w/a.py", "content": "x"})]),
        ("tool", json.dumps({"bytes_written": 1}), None),
        ("assistant", None, [("terminal", {"command": f"git commit -m x && curl -H 'Authorization: {BEARER}' {USERINFO}"}),
                             ("terminal", {"command": f"git push && {HEADER_CMD}"}),
                             ("terminal", {"command": f"export K='{APIKEY}'; systemctl restart x"})]),
        ("tool", json.dumps({"output": "boom", "exit_code": 2}), None),
        ("assistant", f"Test failed; token was {SECRET}. Next I will fix b.py.",
         [("kanban_comment", {"body": "PHASE: 1/3 | done: patch | next: tests"}), ("patch", {"path": "/w/b.py"})]),
    ]


def _crashed_task(home, *, msgs=None, outcome="crashed", db_override=None, sid="local-" + "a" * 64):
    """A ready card whose previous run ended abnormally with a saved owner session."""
    conn = kbc.connect()
    tid = kb.create_task(conn, title="t", assignee="coder")
    claimed = kb.claim_task(conn, tid)
    run_id = claimed.current_run_id
    db = db_override or _state_db(home, sid, msgs if msgs is not None else _convo())
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "owner_admitted", {"db": db, "session_id": sid, "request_id": "r"}, run_id=run_id)
        conn.execute("UPDATE task_runs SET ended_at=?, outcome=?, status=? WHERE id=?", (int(time.time()), outcome, outcome, run_id))
        conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, claim_expires=NULL WHERE id=?", (tid,))
    return conn, tid, run_id


# ------------------------------------------------------------------ switch

def test_switch_default_off_and_config(home, monkeypatch):
    assert kc.enabled() is False
    (home / "config.yaml").write_text("kanban:\n  continuity:\n    enabled: true\n", encoding="utf-8")
    monkeypatch.setattr(kc, "_cfg_cache", None)
    assert kc.enabled() is True
    (home / "config.yaml").write_text("kanban: {continuity: {enabled: false}}\n", encoding="utf-8")
    os.utime(home / "config.yaml", (time.time() + 5, time.time() + 5))
    monkeypatch.setattr(kc, "_cfg_cache", None)
    assert kc.enabled() is False
    (home / "config.yaml").write_text("kanban: [broken", encoding="utf-8")
    monkeypatch.setattr(kc, "_cfg_cache", None)
    assert kc.enabled() is False


def test_context_unchanged_when_off(home, monkeypatch):
    conn, tid, _ = _crashed_task(home)
    before = kb.build_worker_context(conn, tid)
    (home / "config.yaml").write_text("kanban: {continuity: {enabled: false}}\n", encoding="utf-8")
    monkeypatch.setattr(kc, "_cfg_cache", None)
    assert kb.build_worker_context(conn, tid) == before  # absent == explicit false
    monkeypatch.setattr(kc, "enabled", lambda: False)
    assert kb.build_worker_context(conn, tid) == before
    assert "## Prior conversation digest" not in before and "Progress protocol" not in before
    assert kc.on_claim(conn, tid) == 0
    assert kc.progress_tick(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=?", (tid,)).fetchone()[0] == 0


# ------------------------------------------------------------------ digest + context

def test_context_has_digest_protocol_and_no_secret(home, on):
    conn, tid, run_id = _crashed_task(home)
    ctx = kb.build_worker_context(conn, tid)
    assert "## Prior conversation digest" in ctx
    assert f"Run {run_id} (crashed)" in ctx
    assert "/w/a.py" in ctx and "/w/b.py" in ctx
    assert "git commit" in ctx                       # key command kept
    assert "kanban_comment" in ctx                   # card action kept
    assert "exit_code" in ctx                        # error kept
    assert "Next I will fix b.py" in ctx             # last words kept
    assert "## Progress protocol" in ctx and "CHECKPOINT:" in ctx and "PHASE:" in ctx
    for secret in ALL_SECRETS:
        assert secret not in ctx


def test_digest_is_bounded(home, on):
    msgs = [("user", "go", None)] + [("assistant", "n" * 5000, [("terminal", {"command": "git commit " + "x" * 400})]) for _ in range(80)]
    conn, tid, _ = _crashed_task(home, msgs=msgs)
    ctx = kb.build_worker_context(conn, tid)
    digest = ctx.split("## Prior conversation digest", 1)[1].split("## Progress protocol", 1)[0]
    assert len(digest) <= kc.DIGEST_MAX_CHARS + 400


def test_no_digest_for_normal_outcomes_tiny_runs_or_forged_paths(home, on, tmp_path):
    conn, tid, _ = _crashed_task(home, outcome="completed")
    assert "## Prior conversation digest" not in kb.build_worker_context(conn, tid)
    conn, tid, _ = _crashed_task(home, msgs=[("user", "go", None)], sid="local-" + "b" * 64)
    assert "## Prior conversation digest" not in kb.build_worker_context(conn, tid)
    evil = tmp_path / "evil" / "state.db"
    evil.parent.mkdir()
    sqlite3.connect(evil).close()
    conn, tid, _ = _crashed_task(home, db_override=str(evil), sid="local-" + "c" * 64)
    assert "## Prior conversation digest" not in kb.build_worker_context(conn, tid)
    other = home / "profiles" / "coder" / "not-state.sqlite"
    other.write_bytes(b"")
    conn, tid, _ = _crashed_task(home, db_override=str(other), sid="local-" + "d" * 64)
    assert "## Prior conversation digest" not in kb.build_worker_context(conn, tid)


def test_unreadable_state_db_never_breaks_context(home, on):
    conn, tid, _ = _crashed_task(home)
    (home / "profiles" / "coder" / "state.db").write_bytes(b"not a database")
    ctx = kb.build_worker_context(conn, tid)
    assert "## Prior conversation digest" not in ctx and "## Progress protocol" in ctx


def test_latest_checkpoint_surfaced(home, on):
    conn, tid, _ = _crashed_task(home)
    kb.add_comment(conn, tid, "coder", "CHECKPOINT: state: tests red | next: fix b.py | files: b.py")
    ctx = kb.build_worker_context(conn, tid)
    assert "## Latest progress notes" in ctx and "next: fix b.py" in ctx


def test_secret_at_a_cut_boundary_leaves_no_prefix(home, on):
    """A secret that straddles the 110/160-char cut must not leave its first characters behind."""
    pw = "Zq9xVb3LmN8pRt2Ws5YbK7cD"
    cmd = "git commit -m " + "x" * 80 + f" https://u:{pw}@example.com/r.git"
    conn, tid, _ = _crashed_task(home, msgs=[("user", "go", None), ("assistant", "a", [("terminal", {"command": cmd})]),
                                             ("tool", "{}", None), ("assistant", "b", [("patch", {"path": "/w/x"})])])
    ctx = kb.build_worker_context(conn, tid)
    assert pw[:6] not in ctx and "Zq9xVb" not in ctx
    assert "..." not in ctx.split("## Prior conversation digest", 1)[1].split("##", 2)[1].replace("…", "")  # no head/tail masks


# ------------------------------------------------------------------ export at claim

def test_on_claim_exports_private_files_and_one_comment(home, on):
    conn, tid, run_id = _crashed_task(home)
    assert kc.on_claim(conn, tid, None) == 1
    base = kc.continuity_dir(tid, None)
    digest, full = base / f"run-{run_id}.digest.md", base / f"run-{run_id}.full.md"
    assert stat.S_IMODE(digest.stat().st_mode) == 0o600 and stat.S_IMODE(full.stat().st_mode) == 0o600
    assert stat.S_IMODE(base.stat().st_mode) == 0o700
    for f in (digest, full):
        text = f.read_text()
        assert not any(secret in text for secret in ALL_SECRETS)
    comments = conn.execute("SELECT author, body FROM task_comments WHERE task_id=?", (tid,)).fetchall()
    assert len(comments) == 1 and comments[0][1].startswith("TRANSCRIPT: ") and str(digest) in comments[0][1]
    assert SECRET not in comments[0][1]
    assert kc.on_claim(conn, tid, None) == 0         # idempotent
    assert conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=?", (tid,)).fetchone()[0] == 1


def test_export_goes_outside_workspace(home, on):
    conn, tid, _ = _crashed_task(home)
    kc.on_claim(conn, tid, None)
    ws = kb.workspaces_root() / tid
    assert not str(kc.continuity_dir(tid, None)).startswith(str(ws))


def test_dispatch_claim_runs_export(home, on):
    conn, tid, run_id = _crashed_task(home)
    seen = []
    kbd.dispatch_once(conn, spawn_fn=lambda task, ws, board=None: seen.append(task.id) or None)
    assert seen == [tid]
    bodies = [r[0] for r in conn.execute("SELECT body FROM task_comments WHERE task_id=?", (tid,))]
    assert any(b.startswith("TRANSCRIPT: ") and f"run {run_id} crashed" in b for b in bodies)


# ------------------------------------------------------------------ status line

def _running_task(home, sid="local-" + "e" * 64):
    conn = kbc.connect()
    tid = kb.create_task(conn, title="t", assignee="coder")
    claimed = kb.claim_task(conn, tid)
    db = _state_db(home, sid, _convo())
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "owner_admitted", {"db": db, "session_id": sid, "request_id": "r"}, run_id=claimed.current_run_id)
    return conn, tid, claimed.current_run_id, db, sid


def test_progress_tick_rate_limited_and_only_on_change(home, on):
    conn, tid, _run_id, db, sid = _running_task(home)
    t0 = int(time.time())
    assert kc.progress_tick(conn, now=t0) == 1
    ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='progress'", (tid,)).fetchall()
    note = json.loads(ev[0][0])["note"]
    assert note.startswith("step 6 | 6 msgs | now: patch")
    assert SECRET not in note
    assert kc.progress_tick(conn, now=t0 + 60) == 0                         # inside the interval
    assert kc.progress_tick(conn, now=t0 + 400) == 0                        # unchanged: stalled worker adds nothing
    c = sqlite3.connect(db)
    c.execute("INSERT INTO messages (session_id, role, content, tool_calls, timestamp) VALUES (?,?,?,?,?)",
              (sid, "assistant", None, json.dumps([{"function": {"name": "terminal", "arguments": json.dumps({"command": "ls"})}}]), time.time()))
    c.execute("UPDATE sessions SET tool_call_count=7, message_count=7 WHERE id=?", (sid,))
    c.commit()
    c.close()
    assert kc.progress_tick(conn, now=t0 + 800) == 1
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='progress'", (tid,)).fetchone()[0] == 2


def test_progress_tick_does_not_touch_liveness(home, on):
    conn, tid, *_ = _running_task(home)
    before = conn.execute("SELECT last_heartbeat_at FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    kc.progress_tick(conn)
    assert conn.execute("SELECT last_heartbeat_at FROM tasks WHERE id=?", (tid,)).fetchone()[0] == before
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='heartbeat'", (tid,)).fetchone()[0] == 0


# ------------------------------------------------------------------ review round 1

def test_stale_crash_before_a_normal_run_gives_no_digest(home, on):
    conn, tid, _run1 = _crashed_task(home)
    with kb.write_txn(conn):  # a later run completed normally; the card was then reopened
        conn.execute("INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) VALUES (?, 'completed', 'completed', ?, ?)",
                     (tid, int(time.time()), int(time.time())))
    assert "## Prior conversation digest" not in kb.build_worker_context(conn, tid)
    assert kc.on_claim(conn, tid) == 0
    assert conn.execute("SELECT COUNT(*) FROM task_comments WHERE body LIKE 'TRANSCRIPT:%'").fetchone()[0] == 0


def test_large_session_is_bounded(home, on):
    msgs = [("user", "go", None)] + [("assistant", f"step {i} " + "word " * 400, [("terminal", {"command": f"echo {i}"})]) for i in range(3000)]
    conn, tid, _ = _crashed_task(home, msgs=msgs)
    t0 = time.monotonic()
    assert kc.on_claim(conn, tid) == 1
    assert time.monotonic() - t0 < 15
    full = next(Path(str(home)).rglob("run-*.full.md")).read_text()
    assert full.count("## assistant") <= kc._MAX_ROWS


def test_continuity_dirs_private_and_comment_failure_retried(home, on, monkeypatch):
    conn, tid, _ = _crashed_task(home)
    real = kb.add_comment
    monkeypatch.setattr(kb, "add_comment", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("busy")))
    assert kc.on_claim(conn, tid) == 0           # best effort: nothing raised, marker NOT written
    monkeypatch.setattr(kb, "add_comment", real)
    assert kc.on_claim(conn, tid) == 1           # retried on the next claim
    d = next(Path(str(home)).rglob("continuity"))
    assert oct(d.stat().st_mode & 0o777) == "0o700"


def test_progress_tick_runs_in_dispatch_once_and_dry_run_writes_nothing(home, on):
    conn = kbc.connect()
    tid = kb.create_task(conn, title="p", assignee="coder")
    run_id = kb.claim_task(conn, tid).current_run_id
    db = _state_db(home, "local-" + "b" * 64, _convo())
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "owner_admitted", {"db": db, "session_id": "local-" + "b" * 64, "request_id": "r"}, run_id=run_id)
    n = lambda: conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='progress'").fetchone()[0]
    assert kc.progress_tick(conn, dry_run=True) == 0 and n() == 0
    kbd.dispatch_once(conn, dry_run=True, spawn_fn=lambda *a, **k: None)
    assert n() == 0
    kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None)
    assert n() == 1


def test_base64_style_secret_with_slash_is_swept():
    secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYzZ9aB3cDeFgH12"
    assert secret[:10] not in kc._redact(f"aws key {secret} end")


def test_multi_megabyte_tool_rows_are_cheap_and_leak_nothing(home, on):
    big = ("A1b2C3d4" * 300_000)  # 2.4 MB opaque blob, never a secret shape the vendor patterns know
    msgs = [("user", "go", None)] + [("assistant", big, [("terminal", {"command": "cat " + big})]) for _ in range(40)]
    conn, tid, _ = _crashed_task(home, msgs=msgs)
    t0 = time.monotonic()
    assert kc.on_claim(conn, tid) == 1
    assert time.monotonic() - t0 < 10
    assert kc.on_claim(conn, tid) == 0  # idempotent: one TRANSCRIPT comment
    assert conn.execute("SELECT COUNT(*) FROM task_comments WHERE body LIKE 'TRANSCRIPT:%'").fetchone()[0] == 1


def test_secret_straddling_the_precut_boundary_leaves_no_prefix():
    pre = 4000 * 2 + kc._PRECUT_MARGIN
    token = "ghp_" + "Qz8Lm2Xv9Rt4Wk7Yb3Nc5Dh6"
    for fn in (lambda t: kc._cut_keep_lines(t, 4000), lambda t: kc._cut(t, 4000)):
        for blob_len in (pre - 14, pre - 9, pre - 3):  # the token starts a few chars before the slice boundary
            text = ("A1b2C3d4" * 2000)[:blob_len] + " " + token + " tail"
            assert "ghp_Qz" not in fn(text) and "Qz8Lm" not in fn(text)


def test_operator_reset_run_is_not_resumed_but_dispatcher_reclaim_is(home, on):
    conn, tid, run_id = _crashed_task(home, outcome="reclaimed")
    with kb.write_txn(conn):  # dispatcher reclaim: has an error, no summary
        conn.execute("UPDATE task_runs SET error='worker lost' WHERE id=?", (run_id,))
    assert "## Prior conversation digest" in kb.build_worker_context(conn, tid)
    with kb.write_txn(conn):  # operator reset (ancestor reopened): summary, no error
        conn.execute("UPDATE task_runs SET error=NULL, summary='ancestor t_x reopened' WHERE id=?", (run_id,))
    assert "## Prior conversation digest" not in kb.build_worker_context(conn, tid)
    assert kc.on_claim(conn, tid) == 0


def test_worker_written_transcript_comment_does_not_suppress_the_export(home, on):
    conn, tid, run_id = _crashed_task(home)
    kb.add_comment(conn, tid, "coder", f"TRANSCRIPT: /fake | run {run_id} crashed")
    assert kc.on_claim(conn, tid) == 1


def test_unquoted_secret_assignments_never_reach_context_files_or_note(home, on):
    pw = "Zq9xVb3LmN8pRt2Ws5YbK7cDx"
    cmds = [f"mysql --password={pw} -e x", f"tool --token={pw}", f"export K={pw}; systemctl restart y", f"curl https://h/p/{pw}/x"]
    msgs = [("user", "go", None), ("assistant", "a", [("terminal", {"command": cmd}) for cmd in cmds]),
            ("tool", "{}", None), ("assistant", "b", [("terminal", {"command": cmds[0]})])]
    conn, tid, _ = _crashed_task(home, msgs=msgs)
    assert pw[:8] not in kb.build_worker_context(conn, tid)
    assert kc.on_claim(conn, tid) == 1
    for f in Path(str(home)).rglob("run-*.md"):
        assert pw[:8] not in f.read_text()
    assert pw[:8] not in kc._progress_note(*kc._owner_session(conn, tid, conn.execute("SELECT id FROM task_runs").fetchone()[0]))
    assert pw[:8] not in conn.execute("SELECT group_concat(body) FROM task_comments").fetchone()[0]


def test_real_session_schema_digest(home, on, monkeypatch):
    """Guard against schema drift: a session written through the real SessionDB still produces a digest."""
    from hermes_state import SessionDB
    sdb = SessionDB(db_path=home / "profiles" / "coder" / "state.db")
    sid = "local-" + "c" * 64
    sdb.create_session(sid, source="kanban")
    for role, content in (("user", "go"), ("assistant", "one"), ("assistant", "two"), ("assistant", "three")):
        sdb.append_message(sid, role, content)
    conn = kbc.connect()
    tid = kb.create_task(conn, title="t", assignee="coder")
    run_id = kb.claim_task(conn, tid).current_run_id
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "owner_admitted", {"db": str(home / "profiles" / "coder" / "state.db"), "session_id": sid, "request_id": "r"}, run_id=run_id)
        conn.execute("UPDATE task_runs SET ended_at=?, outcome='crashed', status='crashed' WHERE id=?", (int(time.time()), run_id))
        conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, claim_expires=NULL WHERE id=?", (tid,))
    assert "## Prior conversation digest" in kb.build_worker_context(conn, tid)
