"""Named-user POSIX ACL grants survive apply_secure_dir_policy (Byrd-IT carry).

Design: when a directory carries a named-user ACL entry and the default 0700 chmod would
only narrow the ACL mask, the chmod is skipped so the operator's grant keeps working.
Directories without a named-user ACL are chmod'ed as before.
"""

import os
import shutil
import stat
import struct
import subprocess

import pytest

import hermes_constants as hc

pytestmark = pytest.mark.platforms("linux")


def _acl_xattr(*entries):
    """Build a Linux system.posix_acl_access xattr value from (tag, perm, id) tuples."""
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *e) for e in entries)


def test_mask_parser_reports_mask_only_with_named_user(monkeypatch, tmp_path):
    base = [(0x01, 7, 0xFFFFFFFF), (0x04, 0, 0xFFFFFFFF), (0x20, 0, 0xFFFFFFFF)]
    with_user = _acl_xattr(base[0], (0x02, 5, 1234), base[1], (0x10, 5, 0xFFFFFFFF), base[2])
    no_user = _acl_xattr(base[0], base[1], (0x10, 5, 0xFFFFFFFF), base[2])
    for raw, expected in ((with_user, 5), (no_user, None), (b"\x01\x00", None)):
        monkeypatch.setattr(os, "getxattr", lambda *_a, _raw=raw, **_k: _raw, raising=False)
        assert hc._named_user_acl_mask(tmp_path) == expected


def test_mask_parser_returns_none_when_xattr_unavailable(monkeypatch, tmp_path):
    def _enodata(*_a, **_k):
        raise OSError("no acl")

    monkeypatch.setattr(os, "getxattr", _enodata, raising=False)
    assert hc._named_user_acl_mask(tmp_path) is None


@pytest.mark.skipif(not shutil.which("setfacl"), reason="setfacl not installed")
def test_named_user_acl_survives_secure_dir_policy(monkeypatch, tmp_path):
    monkeypatch.setattr(hc, "get_managed_system", lambda *_a, **_k: None)
    monkeypatch.setattr(hc, "_container_or_chmod_skipped", lambda: False)
    monkeypatch.delenv("HERMES_HOME_MODE", raising=False)
    acl_dir, plain_dir = tmp_path / "acl", tmp_path / "plain"
    acl_dir.mkdir()
    plain_dir.mkdir()
    for d in (acl_dir, plain_dir):
        os.chmod(d, 0o755)
    user = str(os.getuid())
    probe = subprocess.run(["setfacl", "-m", f"u:{user}:r-x", str(acl_dir)], capture_output=True)
    if probe.returncode != 0 or hc._named_user_acl_mask(acl_dir) is None:
        pytest.skip("filesystem does not support POSIX ACLs")

    hc.apply_secure_dir_policy(acl_dir)
    hc.apply_secure_dir_policy(plain_dir)

    assert hc._named_user_acl_mask(acl_dir) == 0o5  # mask (and so the named-user grant) intact
    assert stat.S_IMODE(os.stat(plain_dir).st_mode) == 0o700  # no ACL: normal chmod
