"""A docker-era Bot Desktop marker must not route a local-backend profile into the sandbox path.

Found live: a profile moved from ``terminal.backend: docker`` to ``local`` while its old container kept
running. ``bot-desktop/sandbox.json`` still named that container, ``marker_sandbox_alive`` said yes, and
every ``browser_exec`` died in ``published_env`` -> ``sandbox_host._user_for`` -> ``streams.run_in`` with
"LocalEnvironment cannot exec argv". The marker records a sandbox backend; once the terminal is the
gateway host it is stale: the browser env, the RFB socket, status and stop take the host path, and the
marker is left on disk (not deleted).
"""
from __future__ import annotations

import json
import os
import subprocess
import types

import pytest

from tools.bot_desktop import placement, runtime, sandbox_host
from tools.environments.local import LocalEnvironment

DOCKER_ERA_MARKER = {
    "display": ":20", "dir": "/tmp/hermes-bot-desktop/default", "profile": "default",  # no-tmp: ok — sandbox-side path
    "backend": "DockerEnvironment", "container": "c00e53b4ac9b", "docker": "/usr/bin/docker",
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "bot-desktop").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    sandbox_host._marker().write_text(json.dumps(DOCKER_ERA_MARKER), encoding="utf-8")
    monkeypatch.setattr(runtime, "_STALE_MARKER_WARNED", set(), raising=False)  # raising=False: RED on the base
    sandbox_host._ALIVE_CACHE.clear()

    # The old container is "still running": if anything consulted it, the guard did not hold.
    probes: list[list[str]] = []

    def _inspect(argv, **kw):
        probes.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "true\n", "")
    monkeypatch.setattr(sandbox_host.subprocess, "run", _inspect)
    return types.SimpleNamespace(path=home, probes=probes)


@pytest.fixture
def local_env(tmp_path, monkeypatch):
    """A REAL LocalEnvironment as the profile's active terminal env (what the registry holds after the move)."""
    env = LocalEnvironment(cwd=str(tmp_path), timeout=15)
    monkeypatch.setattr(runtime, "_sandbox_env", lambda *, create: env)
    yield env
    try:
        env.cleanup()
    except Exception:
        pass


def _no_host_screen(monkeypatch):
    monkeypatch.setattr(runtime, "_launcher_pid", lambda: None)


def test_local_env_really_cannot_exec_argv(local_env):
    """The failure the guard prevents, exercised for real: the sandbox probe against a LocalEnvironment."""
    with pytest.raises(RuntimeError, match="LocalEnvironment cannot exec argv"):
        sandbox_host.published_env(local_env, "default")


@pytest.mark.parametrize("configured_backend", ["local", "docker"])
def test_docker_era_marker_on_local_terminal_does_not_raise(monkeypatch, home, local_env, configured_backend):
    """``local`` = terminal.backend moved; ``docker`` = config still says docker but the running terminal env is
    a LocalEnvironment. Either way nothing may exec into it."""
    monkeypatch.setattr(placement, "_terminal_backend", lambda: configured_backend)
    _no_host_screen(monkeypatch)

    assert runtime.published_env() == {}
    assert runtime.is_running() is False
    assert runtime.rfb_socket_path() is None
    assert runtime.sandbox_screen_running() is False
    assert runtime._owned_sandbox_env() is None
    assert home.probes == [], "the stale marker's container was probed"
    assert sandbox_host._marker().exists(), "a stale marker is ignored, never deleted"


def test_stale_marker_falls_back_to_the_host_screen(monkeypatch, home, local_env):
    """With a host screen up, the env and the RFB socket are the HOST ones, not None/sandbox."""
    monkeypatch.setattr(placement, "_terminal_backend", lambda: "local")
    sd = runtime.state_dir()
    (sd / "env").write_text("DISPLAY=:31\nXAUTHORITY=/h/xauth\n", encoding="utf-8")
    (sd / "rfb.sock").write_text("", encoding="utf-8")
    monkeypatch.setattr(runtime, "_launcher_pid", lambda: os.getpid())

    assert runtime.published_env() == {"DISPLAY": ":31", "XAUTHORITY": "/h/xauth"}
    assert runtime.rfb_socket_path() == sd / "rfb.sock"


def test_status_and_stop_take_the_host_path(monkeypatch, home, local_env):
    monkeypatch.setattr(placement, "_terminal_backend", lambda: "local")
    _no_host_screen(monkeypatch)
    sandbox_calls: list[str] = []
    monkeypatch.setattr(sandbox_host, "stop", lambda env, profile: sandbox_calls.append("stop") or True)
    monkeypatch.setattr(sandbox_host, "missing_binaries", lambda env: sandbox_calls.append("missing") or [])
    host_stops: list[object] = []
    monkeypatch.setattr(runtime, "_stop_locked", lambda sd: host_stops.append(sd) or False)

    st = runtime.status()
    assert st.placement == "gateway" and st.running is False

    assert runtime.stop() is False
    assert host_stops == [runtime.state_dir()]
    assert sandbox_calls == [] and home.probes == []
    assert sandbox_host._marker().exists()


def test_stale_marker_warns_once(monkeypatch, home, local_env, caplog):
    monkeypatch.setattr(placement, "_terminal_backend", lambda: "local")
    _no_host_screen(monkeypatch)
    # Building the LocalEnvironment already routed through published_env (and warned): start from a clean slate.
    monkeypatch.setattr(runtime, "_STALE_MARKER_WARNED", set(), raising=False)
    with caplog.at_level("WARNING", logger="tools.bot_desktop.runtime"):
        runtime.published_env()
        runtime.published_env()
        runtime.rfb_socket_path()
    hits = [r for r in caplog.records if "stale sandbox marker" in r.getMessage()]
    assert len(hits) == 1 and "c00e53b4ac9b" in hits[0].getMessage()


def test_sandbox_terminal_still_honours_its_marker(monkeypatch, home):
    """Control: on a docker terminal the same marker still routes to the sandbox (the guard is local-only)."""
    class _FakeDocker:
        _docker_exe = "docker"
        _container_id = "c00e53b4ac9b"

    monkeypatch.setattr(placement, "_terminal_backend", lambda: "docker")
    monkeypatch.setattr(runtime, "_sandbox_env", lambda *, create: _FakeDocker())
    monkeypatch.setattr(sandbox_host, "published_env", lambda env, profile: {"DISPLAY": ":20"})
    assert runtime.published_env() == {"DISPLAY": ":20"}
    assert runtime.rfb_socket_path() is None
    assert runtime.sandbox_screen_running() is True


def test_a_failing_backend_probe_keeps_the_marker_in_force(monkeypatch, home):
    """A malformed TERMINAL_* value makes the backend probe raise: that must neither crash the browser's env
    builder nor turn a live sandbox marker stale."""
    def _bad():
        raise ValueError("TERMINAL_DOCKER_VOLUMES must be valid JSON")
    monkeypatch.setattr(placement, "_terminal_backend", _bad)
    assert runtime._terminal_is_local() is False
    assert runtime._owner_marker() == DOCKER_ERA_MARKER
