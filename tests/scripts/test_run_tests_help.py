"""run_tests.sh answers -h/--help without provisioning anything.

Help is a read-only question. Answered after the environment check, it would
re-run the script under run-in-hermes-env, which syncs -- and may download --
the whole test environment before printing a usage line. So the runner answers
it first, from an interpreter that already exists (or from its own Usage block
when there is none), and splits arguments as run_tests_parallel.py does: a
pytest value flag's token, and everything after a literal ``--``, is not help.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash") or "bash"


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _store_at_other_pins(store: Path) -> Path:
    """An account store recording none of this checkout's pins."""
    store.mkdir(parents=True)
    (store / "facts.json").write_text('{"packages": {}}', encoding="utf-8")
    return store


def _snapshot(tree: Path) -> list:
    return sorted((p.relative_to(tree), p.is_file() and p.read_bytes(), p.stat().st_mode) for p in tree.rglob("*"))


def _getent(tmp_path: Path, account: Path) -> None:
    """Answer with ``account`` as the OS account's home; HOME is what the caller says."""
    _executable(tmp_path / "bin" / "getent").write_text(
        f"#!/bin/sh\necho 'account:x:0:0::{account}:/bin/sh'\n", encoding="utf-8")


def _checkout(tmp_path: Path, *, bootstrap: bool = True) -> Path:
    """A repo-shaped checkout: the real runner, activation library, pin check, help
    runner (with the CI modules its --help imports) and pm, beside a stub setup that
    records the store pm resolves for this run and stops, so nothing is installed."""
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    for name in ("run_tests.sh", "run-in-hermes-env", "_activation.sh", "tool_store_pins.py",
                 "run_tests_parallel.py", "run_tests_launch.py"):
        shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
    (root / "scripts" / "ci").symlink_to(ROOT / "scripts" / "ci")
    for name in ("pm", "hermes_constants.py", "hermes_platform"):
        (root / name).symlink_to(ROOT / name)
    if bootstrap:
        _executable(root / ".venv" / "bin" / "python").write_text(
            f"#!/bin/sh\nexec '{sys.executable}' \"$@\"\n", encoding="utf-8")
    (root / "setup-hermes.sh").write_text(
        'cd "$(dirname "$0")"\n'
        f"'{sys.executable}' -I -B -c 'import sys\nsys.path.insert(0, sys.argv[1])\n"
        "from pm.environments import store_root\nprint(store_root(sys.argv[1]), end=\"\")' "
        f"'{root}' > '{root}/provisioned'\nexit 1\n", encoding="utf-8")
    return root


def _run(root: Path, tmp_path: Path, home: Path, args: list, naming: dict | None = None,
         *, path: str | None = None):
    """Run the real runner with ``args`` and return the run plus the store its setup
    step was aimed at (None when setup never ran)."""
    (root / "provisioned").unlink(missing_ok=True)
    run = subprocess.run(
        [BASH, str(root / "scripts" / "run_tests.sh"), *args], cwd=root,
        env={"PATH": path or f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}", "HOME": str(home),
             "PYTHONDONTWRITEBYTECODE": "1", **(naming or {})},
        capture_output=True, text=True, encoding="utf-8", timeout=120, check=False,
    )
    provisioned = root / "provisioned"
    return run, Path(provisioned.read_text(encoding="utf-8-sig")) if provisioned.is_file() else None


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("arg", ["--help", "-h"])
def test_help_prints_the_runners_usage_without_provisioning(tmp_path, arg):
    """A help request must be answered before the environment check: reaching that
    check re-execs the script under run-in-hermes-env, which syncs the test
    environment, so the usage would arrive after a download rather than instead of
    one. Nothing may be provisioned, written, or re-executed."""
    account, home = tmp_path / "account", tmp_path / "fakehome"
    _store_at_other_pins(account / ".hermes" / "tools")  # a store this run would move off
    home.mkdir()
    _getent(tmp_path, account)
    root = _checkout(tmp_path)
    before = (_snapshot(account), _snapshot(home), _snapshot(root))
    run, provisioned = _run(root, tmp_path, home, [arg])
    assert run.returncode == 0, run.stdout + run.stderr
    assert "usage:" in run.stdout and "--file-timeout" in run.stdout, run.stdout
    assert provisioned is None, f"setup reached {provisioned}\n{run.stdout}{run.stderr}"
    assert "environment missing or stale" not in run.stderr, run.stderr
    assert (_snapshot(account), _snapshot(home), _snapshot(root)) == before, \
        "help must not write anything (no store entry, no setup, no __pycache__: -B)"


@pytest.mark.platforms("posix")
def test_help_among_test_paths_and_bare_flags_is_still_help(tmp_path):
    """The scan looks at every argument, not just the first: a help flag after a
    test path and a bare pytest flag is still a help request."""
    account, home = tmp_path / "account", tmp_path / "fakehome"
    _store_at_other_pins(account / ".hermes" / "tools")
    home.mkdir()
    _getent(tmp_path, account)
    root = _checkout(tmp_path)
    run, provisioned = _run(root, tmp_path, home, ["tests/nothing.py", "-q", "--help"])
    assert run.returncode == 0, run.stdout + run.stderr
    assert "usage:" in run.stdout and "--file-timeout" in run.stdout, run.stdout
    assert provisioned is None, f"setup reached {provisioned}\n{run.stdout}{run.stderr}"
    assert "environment missing or stale" not in run.stderr, run.stderr


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("args", [["-k", "-h"], ["--", "-h"]])
def test_dash_h_as_a_flag_value_or_after_the_separator_is_not_help(tmp_path, args):
    """`-k -h` selects tests matching "-h" and everything after a literal `--` is
    pytest's, so neither is this runner's help: the run must go on to the
    environment check instead of printing usage and exiting."""
    account, home = tmp_path / "account", tmp_path / "fakehome"
    _store_at_other_pins(account / ".hermes" / "tools")
    home.mkdir()
    _getent(tmp_path, account)
    root = _checkout(tmp_path)
    run, provisioned = _run(root, tmp_path, home, args)
    assert "usage:" not in run.stdout, run.stdout
    assert provisioned is not None, f"the run stopped before setup\n{run.stdout}{run.stderr}"
    assert "environment missing or stale" in run.stderr, run.stdout + run.stderr


@pytest.mark.platforms("posix")
def test_help_with_no_interpreter_falls_back_to_the_scripts_own_usage(tmp_path):
    """Without HERMES_PYTHON, a bootstrap interpreter or a python3 on PATH there is
    nothing to print the runner's usage, so the script prints the Usage block it
    carries itself — and still exits 0 without provisioning."""
    account, home = tmp_path / "account", tmp_path / "fakehome"
    _store_at_other_pins(account / ".hermes" / "tools")
    home.mkdir()
    _getent(tmp_path, account)
    root = _checkout(tmp_path, bootstrap=False)
    slim = tmp_path / "slim"
    _executable(slim / "getent").write_text(
        f"#!/bin/sh\necho 'account:x:0:0::{account}:/bin/sh'\n", encoding="utf-8")
    for name in ("dirname", "sed", "cut", "id", "cat", "readlink"):
        (slim / name).symlink_to(shutil.which(name))
    run, provisioned = _run(root, tmp_path, home, ["--help"], path=str(slim))
    assert run.returncode == 0, run.stdout + run.stderr
    assert "Usage:" in run.stdout and "scripts/run_tests.sh -j 4" in run.stdout, run.stdout
    assert "usage:" not in run.stdout, "no interpreter may print the runner's own usage here"
    assert provisioned is None, f"setup reached {provisioned}\n{run.stdout}{run.stderr}"
    assert "environment missing or stale" not in run.stderr, run.stderr
