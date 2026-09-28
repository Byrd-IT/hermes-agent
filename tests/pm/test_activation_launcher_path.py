"""Bare ``hermes`` in an activated child shell must resolve to the published launcher.

The committed venv's ``bin/hermes`` is uv's console script; its editable finder maps
packages to the generation's build snapshot, which goes stale after a source update
that leaves uv.lock unchanged. activate_dependencies() prepends the venv bin to PATH
for child processes, so without the launcher ahead of it every agent-shell ``hermes``
call ran pre-update code and reported false PM drift ("install out of sync").
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX launcher layout")

REPO = Path(__file__).resolve().parents[2]


def _site_of(venv: Path) -> Path:
    return venv / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"


def _executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)


def _install(tmp_path, monkeypatch, *, launcher: bool) -> tuple[Path, Path]:
    from pm import environments as runtime_paths

    root = tmp_path / "repo"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    state = runtime_paths.install_state_dir(root)
    venv = state / "environments" / "gen" / "venv"
    _site_of(venv).mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = test\n")
    _executable(venv / "bin" / "hermes")  # the stale uv console script
    if launcher:
        _executable(root / ".hermes" / "bin" / "hermes")
    (state / "facts.json").write_text(json.dumps({"schema": 1, "packages": {
        "venv": {"environment": str(venv)}}}))
    return root, venv


def _activate(root: Path, path: str) -> list[str]:
    code = ("import os, sys; from pathlib import Path; "
            "from pm.environments import activate_dependencies; "
            "activate_dependencies(Path(sys.argv[1])); print(os.environ['PATH'])")
    env = dict(os.environ, PATH=path)
    process = subprocess.run([sys.executable, "-c", code, str(root)], cwd=REPO, env=env,
                             text=True, capture_output=True, timeout=60)
    assert process.returncode == 0, process.stderr
    return process.stdout.strip().split(os.pathsep)


def test_published_launcher_outranks_generation_console_script(tmp_path, monkeypatch):
    root, venv = _install(tmp_path, monkeypatch, launcher=True)
    launcher_dir = str((root / ".hermes" / "bin").resolve())
    entries = _activate(root, "/usr/bin:/bin")
    assert entries[:2] == [launcher_dir, str(venv / "bin")]
    assert entries[2:] == ["/usr/bin", "/bin"]
    # What a child shell's bare `hermes` resolves to.
    assert shutil.which("hermes", path=os.pathsep.join(entries)) == os.path.join(launcher_dir, "hermes")


def test_nested_activation_keeps_launcher_first_without_growing_path(tmp_path, monkeypatch):
    """A worker spawned by an activated gateway re-activates with the parent's PATH."""
    root, venv = _install(tmp_path, monkeypatch, launcher=True)
    first = _activate(root, "/usr/bin:/bin")
    # Simulate an inherited PATH where the stale venv bin had been put in front.
    inherited = [str(venv / "bin"), *first]
    second = _activate(root, os.pathsep.join(inherited))
    assert second == first


def test_without_published_launcher_venv_bin_still_leads(tmp_path, monkeypatch):
    """Sealed/developer installs with no .hermes/bin keep the historical contract."""
    root, venv = _install(tmp_path, monkeypatch, launcher=False)
    entries = _activate(root, "/usr/bin:/bin")
    assert entries == [str(venv / "bin"), "/usr/bin", "/bin"]


def test_published_launcher_dir_requires_the_hermes_launcher(tmp_path):
    from pm.environments import published_launcher_dir

    root = tmp_path / "repo"
    (root / ".hermes" / "bin").mkdir(parents=True)
    assert published_launcher_dir(root) is None
    _executable(root / ".hermes" / "bin" / "hermes")
    assert published_launcher_dir(root) == (root / ".hermes" / "bin").resolve()
