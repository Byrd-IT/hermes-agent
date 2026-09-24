"""The suite must never relink the operator's real ``~/.local/bin/hermes``.

HOME is not redirected under pytest, so ``hermes doctor --fix``'s command-link
repair (``doctor_platform._check_command_installation``: ``Path.unlink`` then
``Path.symlink_to`` on ``Path.home()/.local/bin/hermes``) used to repoint the
real launcher at whichever checkout's venv ran the tests. The conftest guard
refuses link/unlink/rename primitives aimed at the real directory.

The probes use random names, so without the guard they are harmless but red
(a stray probe link, which is removed; FileNotFoundError instead of
PermissionError). They never touch the real launcher itself.
"""

import os
import subprocess
import uuid
from pathlib import Path

import pytest


def _probe() -> Path:
    return Path(os.path.expanduser("~/.local/bin")) / f"hermes-guard-probe-{uuid.uuid4().hex}"


def test_symlink_into_real_local_bin_is_refused():
    link = _probe()
    try:
        with pytest.raises(PermissionError, match="real"):
            link.symlink_to("/nonexistent")
    finally:
        if os.path.lexists(link):  # guard missing: clean up without the patched os.unlink
            subprocess.run(["rm", "-f", "--", str(link)], check=False)


def test_unlink_in_real_local_bin_is_refused():
    with pytest.raises(PermissionError, match="real"):
        _probe().unlink()


def test_tmp_home_links_still_work(tmp_path):
    link = tmp_path / ".local" / "bin" / "hermes"
    link.parent.mkdir(parents=True)
    link.symlink_to(tmp_path / "venv-hermes")
    link.unlink()
    assert not os.path.lexists(link)
