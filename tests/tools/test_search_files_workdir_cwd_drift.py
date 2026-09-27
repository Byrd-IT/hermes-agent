"""search_files must resolve a relative ``path`` against the task's workspace
root, not the shared ``env.cwd`` that a ``terminal(workdir=...)`` call moves.

Repro (ops t_5d3058ef): ws/repo/AGENTS.md exists. search_files(path='repo')
works -> terminal('pwd', workdir=ws/repo) -> search_files(path='repo') returned
"Path not found: repo" while read_file('repo/AGENTS.md') still worked, because
the marker parse stamped env.cwd = ws/repo and search_tool handed the RAW
relative path to ShellFileOperations, which resolved it against env.cwd.
"""

import json

import pytest

import tools.terminal_tool as tt
from tools.file_tools import read_file_tool, search_tool


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    workspace = tmp_path / "ws"
    repo = workspace / "repo"
    (repo / "agent").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("# agents\nNEEDLE_TOKEN here\n")
    (repo / "agent" / "core.py").write_text("x = 'NEEDLE_TOKEN'\n")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(workspace))
    monkeypatch.setattr(tt, "_check_all_guards",
                        lambda command, env_type, **kwargs: {"approved": True})
    return workspace


def _run_task_terminal(task_id, command, **kwargs):
    return json.loads(tt.terminal_tool(command=command, task_id=task_id, **kwargs))


def _cleanup(task_id):
    from tools.file_tools import clear_file_ops_cache
    from tools.terminal_tool_lifecycle import cleanup_vm
    try:
        cleanup_vm(task_id)
    except Exception:
        pass
    clear_file_ops_cache(task_id)
    tt.clear_session_cwd(task_id)


@pytest.mark.parametrize("target,pattern", [("files", "*.md"), ("content", "NEEDLE_TOKEN")])
def test_relative_search_survives_workdir_command(ws, target, pattern):
    task_id = f"t-search-workdir-drift-{target}"
    try:
        tt.record_session_cwd(task_id, str(ws))
        # Env bring-up anchored at the workspace root.
        assert _run_task_terminal(task_id, "pwd")["exit_code"] == 0

        before = json.loads(search_tool(pattern, path="repo", target=target, task_id=task_id))
        assert "error" not in before, before

        # A one-off workdir command moves the SHARED env.cwd into ws/repo ...
        moved = _run_task_terminal(task_id, "pwd", workdir=str(ws / "repo"))
        assert moved["exit_code"] == 0
        # ... but never the session's durable cwd.
        assert tt.get_session_cwd(task_id) == str(ws)

        after = json.loads(search_tool(pattern, path="repo", target=target, task_id=task_id))
        assert "error" not in after, after
        blob = json.dumps(after)
        assert "AGENTS.md" in blob
        # read_file was already immune; keep the symmetry pinned.
        read = json.loads(read_file_tool("repo/AGENTS.md", task_id=task_id))
        assert "NEEDLE_TOKEN" in read.get("content", ""), read
    finally:
        _cleanup(task_id)


def test_relative_search_output_paths_stay_workspace_relative(ws):
    """Pinning the search cwd to the workspace root keeps hits relative to it
    (``repo/...``), not to wherever the last workdir command left env.cwd."""
    task_id = "t-search-workdir-drift-relpaths"
    try:
        tt.record_session_cwd(task_id, str(ws))
        _run_task_terminal(task_id, "pwd", workdir=str(ws / "repo" / "agent"))
        r = json.loads(search_tool("*.py", path="repo", target="files", task_id=task_id))
        assert "error" not in r, r
        assert any(f.endswith("repo/agent/core.py") for f in r["files"]), r
    finally:
        _cleanup(task_id)


def test_workdir_miss_is_not_cached_as_a_stale_not_found(ws):
    """The not-found cache is keyed on the workspace-resolved path; the search
    must look at that same path, so a genuine hit is never cached as a miss."""
    task_id = "t-search-workdir-drift-cache"
    try:
        tt.record_session_cwd(task_id, str(ws))
        _run_task_terminal(task_id, "pwd", workdir=str(ws / "repo"))
        first = json.loads(search_tool("*.md", path="repo", target="files", task_id=task_id))
        second = json.loads(search_tool("*.md", path="repo", target="files", task_id=task_id))
        assert "error" not in first and "error" not in second, (first, second)
    finally:
        _cleanup(task_id)


def _live_env(task_id):
    from tools.file_tools import _get_file_ops
    return _get_file_ops(task_id).env


def test_search_ignores_env_cwd_moved_by_anything(ws):
    """Search-side fix in isolation: even if env.cwd drifts for any reason
    (not only workdir), a relative root resolves against the workspace."""
    task_id = "t-search-envcwd-direct"
    try:
        tt.record_session_cwd(task_id, str(ws))
        env = _live_env(task_id)
        env.cwd = str(ws / "repo" / "agent")
        r = json.loads(search_tool("NEEDLE_TOKEN", path="repo", task_id=task_id))
        assert "error" not in r, r
        paths = {m["path"] for m in r.get("matches", [])} or set(json.dumps(r).split('"'))
        assert any(p == "repo/AGENTS.md" for p in paths), r
    finally:
        _cleanup(task_id)


def test_missing_relative_root_error_keeps_raw_spelling(ws):
    task_id = "t-search-missing-rel"
    try:
        tt.record_session_cwd(task_id, str(ws))
        r = json.loads(search_tool("x", path="rep", task_id=task_id))
        assert r["error"].startswith("Path not found: rep"), r
        assert str(ws) not in r["error"], r
        assert "Similar paths: ./repo" in r["error"], r
    finally:
        _cleanup(task_id)


def test_workdir_then_durable_cd_still_persists(ws):
    """The fix is search-side only: terminal cwd semantics are unchanged, so a
    plain ``cd`` after a workdir command still moves env.cwd and the session."""
    task_id = "t-search-workdir-then-cd"
    try:
        tt.record_session_cwd(task_id, str(ws))
        _run_task_terminal(task_id, "pwd")
        out = _run_task_terminal(task_id, "pwd", workdir=str(ws / "repo"))
        assert str(ws / "repo") in out["output"]
        assert tt.get_session_cwd(task_id) == str(ws)
        _run_task_terminal(task_id, "cd repo")
        env = _live_env(task_id)
        assert env.cwd == str(ws / "repo")
        assert tt.get_session_cwd(task_id) == str(ws / "repo")
    finally:
        _cleanup(task_id)


def test_interleaved_workdir_command_does_not_clobber_newer_durable_cd(ws, monkeypatch):
    """Review regression (t_83e4c544): session A runs a transient
    workdir=ws/repo command and pauses after its marker is parsed; session B
    runs a durable ``cd repo`` (same path) and completes; A then resumes. The
    shared env.cwd must still hold B's durable ws/repo afterwards — no
    post-command restore may overwrite a newer write whose path happens to
    equal A's observed cwd."""
    import threading

    task_id = "t-search-workdir-interleave"
    paused, release = threading.Event(), threading.Event()
    results = []
    try:
        tt.record_session_cwd(task_id, str(ws))
        env = _live_env(task_id)
        original = env.execute

        def execute(command, *args, **kwargs):
            result = original(command, *args, **kwargs)
            if "TRANSIENT_MARK" in command:
                paused.set()
                assert release.wait(20)
            return result

        monkeypatch.setattr(env, "execute", execute)
        thread = threading.Thread(target=lambda: results.append(
            _run_task_terminal(task_id, "pwd # TRANSIENT_MARK", workdir=str(ws / "repo"))))
        thread.start()
        try:
            assert paused.wait(20)
            durable = _run_task_terminal(task_id, "cd repo")
            assert durable["exit_code"] == 0, durable
            assert tt.get_session_cwd(task_id) == str(ws / "repo")
            assert env.cwd == str(ws / "repo")
        finally:
            release.set()
            thread.join(20)
        assert results and results[0]["exit_code"] == 0, results
        assert env.cwd == str(ws / "repo"), (env.cwd, tt.get_session_cwd(task_id))
        assert tt.get_session_cwd(task_id) == str(ws / "repo")
        # Relative search now anchors at the durable session cwd (ws/repo).
        r = json.loads(search_tool("*.md", path=".", target="files", task_id=task_id))
        assert "error" not in r, r
        assert "AGENTS.md" in json.dumps(r), r
    finally:
        release.set()
        _cleanup(task_id)
