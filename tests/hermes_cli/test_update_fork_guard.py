"""Plain ``hermes update`` must refuse on a release-folder / carried-commit install.

Live incident (2026-11-07): a plain update moved a detached release-folder checkout to plain
upstream and dropped every carried commit. These run ``main.cmd_update`` against REAL git repos.
"""

import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import main as hermes_main
from hermes_cli import update_fork_guard
from hermes_cli.update_fork_guard import REFUSAL_MESSAGE


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def _commit(repo, name):
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", name)


@pytest.fixture()
def upstream(tmp_path):
    up = tmp_path / "upstream.git-src"
    up.mkdir()
    _git(up, "init", "-q", "-b", "main")
    _commit(up, "u1")
    return up


def _clone(upstream, dest, *, remote="upstream"):
    _git(dest.parent, "clone", "-q", "-o", remote, str(upstream), str(dest))
    return dest


@pytest.fixture()
def harness(monkeypatch, tmp_path):
    monkeypatch.delenv(update_fork_guard.ALLOW_ENV, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    calls = []
    monkeypatch.setattr("hermes_cli.update_owning_install.retarget_to_owning_install", lambda root: None)
    monkeypatch.setattr(hermes_main, "_update_preflight_handled", lambda args: False)
    monkeypatch.setattr(hermes_main, "_install_hangup_protection", lambda **kw: calls.append("hangup") or None)
    monkeypatch.setattr("hermes_cli.update_cmd._cmd_update_impl", lambda *a, **k: calls.append("impl"))
    monkeypatch.setattr(hermes_main, "_finalize_update_output", lambda state: None, raising=False)

    def run(root):
        monkeypatch.setattr(hermes_main, "PROJECT_ROOT", root)
        return hermes_main.cmd_update(SimpleNamespace(gateway=False))

    run.calls = calls
    return run


def _state(repo):
    return _git(repo, "rev-parse", "HEAD"), _git(repo, "reflog", "-n", "50"), _git(repo, "status", "--porcelain")


def test_detached_release_folder_with_carries_refuses(harness, upstream, tmp_path, capsys):
    release = _clone(upstream, _mk(tmp_path / "hermes-agent-releases") / "aaa-bbb")
    _commit(release, "carry1")
    _git(release, "checkout", "-q", "--detach")
    before = _state(release)
    with pytest.raises(SystemExit) as exc:
        harness(release)
    out = capsys.readouterr().out
    assert exc.value.code not in (0, None)
    assert REFUSAL_MESSAGE in out and "HERMES_ALLOW_PLAIN_UPDATE=1" in out
    assert harness.calls == []  # no lock, no snapshot, no fetch-and-switch
    assert _state(release) == before


def test_symlinked_live_path_into_releases_refuses(harness, upstream, tmp_path, capsys):
    release = _clone(upstream, _mk(tmp_path / "hermes-agent-releases") / "ccc-ddd")  # no carries at all
    live = tmp_path / "hermes-agent"
    live.symlink_to(release)
    with pytest.raises(SystemExit) as exc:
        harness(live)
    assert exc.value.code not in (0, None)
    assert REFUSAL_MESSAGE in capsys.readouterr().out
    assert harness.calls == []


def test_plain_branch_checkout_with_carried_commit_refuses(harness, upstream, tmp_path, capsys):
    clone = _clone(upstream, tmp_path / "clone")
    _commit(clone, "carry1")
    with pytest.raises(SystemExit):
        harness(clone)
    assert REFUSAL_MESSAGE in capsys.readouterr().out
    assert harness.calls == []


def test_plain_clone_without_carries_is_unchanged(harness, upstream, tmp_path, capsys):
    clone = _clone(upstream, tmp_path / "clone")
    harness(clone)
    assert REFUSAL_MESSAGE not in capsys.readouterr().out
    assert "impl" in harness.calls


def test_clone_without_upstream_remote_is_unchanged(harness, upstream, tmp_path, capsys):
    clone = _clone(upstream, tmp_path / "clone", remote="origin")
    _commit(clone, "local")
    harness(clone)
    assert "impl" in harness.calls


@pytest.mark.parametrize("value", ["1", "true", "YES"])
def test_override_env_allows_update(harness, monkeypatch, upstream, tmp_path, value):
    release = _clone(upstream, _mk(tmp_path / "hermes-agent-releases") / "eee-fff")
    monkeypatch.setenv(update_fork_guard.ALLOW_ENV, value)
    harness(release)
    assert "impl" in harness.calls


def test_override_env_zero_does_not_allow(harness, monkeypatch, upstream, tmp_path):
    release = _clone(upstream, _mk(tmp_path / "hermes-agent-releases") / "ggg-hhh")
    monkeypatch.setenv(update_fork_guard.ALLOW_ENV, "0")
    with pytest.raises(SystemExit):
        harness(release)


def test_refusal_text_leaks_no_remote_credentials(monkeypatch, upstream, tmp_path):
    monkeypatch.delenv(update_fork_guard.ALLOW_ENV, raising=False)
    release = _clone(upstream, _mk(tmp_path / "hermes-agent-releases") / "iii-jjj")
    _git(release, "remote", "set-url", "upstream", "https://user:s3cr3tT0ken@example.com/x.git")
    msg = update_fork_guard.plain_update_refusal(release)
    assert msg and "s3cr3tT0ken" not in msg and "user:" not in msg


def _mk(path):
    path.mkdir(exist_ok=True)
    return path
