"""`hermes update` must not drop local commits without leaving a named way back.

Byrd-IT deployment contract: when the checkout sits on the update's target
branch and carries local commits, the update refuses (e82bf770cc) instead of
upstream's reset hard to ``origin/<branch>``. Only when no local commit lies
outside ``origin/<branch>`` (a true upstream force-push) does the reset run, and
then the old HEAD is still parked under ``refs/hermes-update-backups/`` first.
The installer update paths are covered in
``tests/scripts/install/test_install_diverged_rescue_ref.py``.
"""

from __future__ import annotations

import subprocess

import pytest

from hermes_cli import update_cmd


GIT = ["git"]


def _git(repo, *args, check=True):
    return subprocess.run(
        GIT + list(args), cwd=repo, capture_output=True, text=True, check=check)


def _commit(repo, name, text):
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture()
def diverged_checkout(tmp_path):
    """A checkout on ``main`` carrying a local commit its ``origin/main`` does not have."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _commit(upstream, "shared.txt", "shared\n")
    _commit(upstream, "upstream-only.txt", "upstream\n")

    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(upstream), str(checkout))
    _git(checkout, "reset", "-q", "--hard", "HEAD~1")          # back to the shared commit
    local_sha = _commit(checkout, "local-fix.txt", "local\n")  # diverges from origin/main
    return checkout, local_sha


def _rescue_refs(checkout):
    out = _git(checkout, "for-each-ref", "--format=%(refname) %(objectname)",
               "refs/hermes-update-backups/").stdout
    return dict(line.split() for line in out.splitlines() if line.strip())


def test_hermes_update_refuses_to_reset_local_commits(
        diverged_checkout, monkeypatch, capsys):
    """Byrd-IT guard (e82bf770cc): the real apply path stops instead of resetting local commits.

    Upstream resets after parking a rescue ref; this deployment carries fleet fixes on ``main``
    and must never drop them unattended, so the update refuses and leaves HEAD and refs alone.
    """
    checkout, local_sha = diverged_checkout
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", checkout)

    with pytest.raises(SystemExit) as exc:
        update_cmd._pull_updates(
            GIT, "main", None, prompt_for_restore=False, gw_input_fn=None,
            discard_local_changes=False, keep_stash=False)

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Refusing to reset main: 1 local commit(s)" in out
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == local_sha
    assert _rescue_refs(checkout) == {}
