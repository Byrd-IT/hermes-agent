"""The kanban worker client must not import a hermes-agent checkout that is its cwd.

``_default_spawn`` runs the worker with ``cwd=<task workspace>``. Plain ``python -m`` puts the cwd
first on ``sys.path`` (ahead of PYTHONPATH), so a worktree card on the hermes-agent repo made the
client import *that* checkout's ``hermes_bootstrap``, which resolved a PM install key with no
committed environment and died before its first turn ("no dependency environment is committed for
this install; run `hermes pm repair`"). The worker argv carries ``-P`` (safe path) and relies on the
install root that ``pin_hermes_tree_on_pythonpath`` puts on the worker's PYTHONPATH.

These tests spawn the real captured command, not a stub of it.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd

_FOREIGN_MARK = "FOREIGN-CHECKOUT-IMPORTED"
# The fixture replaces ``subprocess.Popen`` to capture the dispatcher's launch; keep the real one.
_REAL_POPEN = subprocess.Popen


def _foreign_checkout(root: Path) -> Path:
    """A directory shaped like another hermes-agent checkout whose modules announce themselves."""
    root.mkdir()
    boom = f"import sys\nsys.stderr.write({_FOREIGN_MARK!r} + '\\n')\nsys.exit(97)\n"
    (root / "hermes_bootstrap.py").write_text(boom, encoding="utf-8")
    (root / "hermes_constants.py").write_text(boom, encoding="utf-8")
    pkg = root / "hermes_cli"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(boom, encoding="utf-8")
    (pkg / "kanban_worker_client.py").write_text(boom, encoding="utf-8")
    return root


@pytest.fixture
def captured_spawn(tmp_path, monkeypatch):
    """Run the real ``_default_spawn`` with a fake-checkout workspace; capture what it would launch."""
    from tools import process_registry

    home = tmp_path / ".hermes"
    home.mkdir()
    workspace = _foreign_checkout(tmp_path / "worktree")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in ("HERMES_KANBAN_HOME", "PYTHONPATH", "HERMES_BIN", "INVOCATION_ID", "PYTHONSAFEPATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: False)

    captured = {}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            captured.update(cmd=list(cmd), env=dict(kwargs["env"]), cwd=kwargs.get("cwd"))
            self.pid = 4242

    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    task = kb.Task(
        id="t_cwd_shadow", title="x", body=None, assignee="coder", status="ready",
        priority=0, created_by=None, created_at=0, started_at=None, completed_at=None,
        workspace_kind="worktree", workspace_path=str(workspace), claim_lock=None,
        claim_expires=None, tenant=None, branch_name=None,
    )
    kbd._default_spawn(task, str(workspace))
    assert captured["cwd"] == str(workspace)
    # Without a task id the real client refuses before contacting any owner: a deterministic,
    # side-effect-free exit that still proves which tree was imported.
    captured["env"].pop("HERMES_KANBAN_TASK", None)
    return captured


def _run(cmd, captured) -> subprocess.CompletedProcess:
    with _REAL_POPEN(cmd, cwd=captured["cwd"], env=captured["env"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
        stdout, stderr = proc.communicate(timeout=120)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def test_worker_argv_is_safe_path_module_form(captured_spawn):
    argv = kbd._worker_argv(None, "coder", None)  # type: ignore[arg-type]  # task is unused
    assert argv == [sys.executable, "-P", "-m", "hermes_cli.kanban_worker_client"]


def test_spawned_env_pins_this_install_root(captured_spawn):
    root = str(Path(kbd.__file__).resolve().parents[1])
    assert captured_spawn["env"]["PYTHONPATH"].split(os.pathsep)[0] == root


def test_fixture_reproduces_cwd_shadowing_without_safe_path(captured_spawn):
    """Control: the pre-fix argv (no ``-P``) loads the cwd checkout even with the root pinned."""
    cmd = [part for part in captured_spawn["cmd"] if part != "-P"]
    result = _run(cmd, captured_spawn)
    assert result.returncode == 97, result.stderr
    assert _FOREIGN_MARK in result.stderr


def test_worker_runs_this_install_from_a_foreign_checkout_cwd(captured_spawn):
    result = _run(captured_spawn["cmd"], captured_spawn)
    assert _FOREIGN_MARK not in result.stderr, result.stderr
    assert "Kanban owner refused" in result.stderr, result.stderr
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
    assert f"{KANBAN_WORKER_EXIT_TRAILER}1" in result.stderr
    assert result.returncode == 1
