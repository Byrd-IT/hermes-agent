"""The variant id is a process-keyed HMAC, not an unkeyed hash of session values.

The id is embedded in the server key. Same epoch, values, and base config still
select one connection; a different value or a changed base config still selects
another. An observer who knows those inputs cannot rebuild the id.
"""
import hashlib
import hmac
import json
import os

import pytest

from agent.session_client_env import client_env_scope
from tools import mcp_tool_session_env as se
from tools.mcp_tool_common import _core

_CONFIG = {
    "command": "unused",
    "args": ["--pin", "${RUN_PIN}"],
    "env": {"PIN": "${RUN_PIN}"},
    "session_env": ["RUN_PIN"],
}


def _install(base, config=None):
    _core._lazy_server_configs[base] = dict(config or _CONFIG)
    return _core._lazy_server_configs[base]


def _cleanup(*bases):
    for base in bases:
        _core._lazy_server_configs.pop(base, None)
    se.reset()


def _identity(base, values):
    return json.dumps([values, se._plain(se._base_config(base))], sort_keys=True, default=str)


def _material(base, values):
    return f"{se._base_epochs[base]}:{_identity(base, values)}".encode()


def _process_secret():
    secret = getattr(se, "_DIGEST_SECRET", None)
    assert isinstance(secret, (bytes, bytearray)) and len(secret) >= 16, (
        "variant digest has no process secret"
    )
    return secret


def _keyed(material, secret=None):
    return hmac.new(secret if secret is not None else _process_secret(), material, "sha256").hexdigest()[:16]


def _id_of(key):
    assert key is not None
    scope, digest = key[0].split(se.MARK, 1)
    assert digest and se.MARK not in digest
    return scope, digest


def test_variant_id_is_hmac_under_the_process_secret_not_an_unkeyed_hash():
    """Knowing the epoch and the session values must not be enough to rebuild the id."""
    base = ("profile-scope", "digest-srv")
    values = {"RUN_PIN": "slug-a"}
    _install(base)
    try:
        with client_env_scope(values):
            key = se.variant_key(base)
        assert se.is_variant_key(key)
        scope, digest = _id_of(key)
        material = _material(base, values)
        unkeyed = hashlib.sha256(material).hexdigest()[:16]
        assert digest != unkeyed
        assert scope == "profile-scope"
        assert key[1] == "digest-srv"
        assert digest == _keyed(material)
        assert "slug-a" not in key[0]
        assert len(digest) == 16
    finally:
        _cleanup(base)


def test_same_inputs_reuse_the_variant_id_until_the_process_secret_rotates(monkeypatch):
    """One secret reuses the connection; rotating it must mint a different id."""
    base = "digest-rotate"
    values = {"RUN_PIN": "slug-a"}
    _install(base)
    try:
        with client_env_scope(values):
            first = se.variant_key(base)
            again = se.variant_key(base)
            rotated = os.urandom(32)
            monkeypatch.setattr(se, "_DIGEST_SECRET", rotated, raising=False)
            second = se.variant_key(base)
        assert first == again
        assert second != first
        assert _id_of(second)[1] == _keyed(_material(base, values), rotated)
        assert _id_of(first)[1] != _id_of(second)[1]
    finally:
        _cleanup(base)


def test_value_or_config_change_still_selects_a_fresh_keyed_id():
    """A new value or an edited base config still gets its own id, still keyed."""
    base = "digest-fresh"
    config = _install(base)
    try:
        with client_env_scope({"RUN_PIN": "slug-a"}):
            original = se.variant_key(base)
        assert _id_of(original)[1] == _keyed(_material(base, {"RUN_PIN": "slug-a"}))
        with client_env_scope({"RUN_PIN": "slug-b"}):
            changed_value = se.variant_key(base)
        assert original != changed_value
        assert _id_of(changed_value)[1] == _keyed(_material(base, {"RUN_PIN": "slug-b"}))
        config["args"] = ["--changed"]
        with client_env_scope({"RUN_PIN": "slug-a"}):
            changed_config = se.variant_key(base)
        assert changed_config not in (original, changed_value)
        assert _id_of(changed_config)[1] == _keyed(_material(base, {"RUN_PIN": "slug-a"}))
    finally:
        _cleanup(base)


@pytest.mark.parametrize("values", [{"RUN_PIN": ""}, {}, {"OTHER": "x"}])
def test_absent_declared_values_still_do_not_mint_a_variant(values):
    """Keying the digest must not start a variant when the client sent nothing declared."""
    base = "digest-absent"
    _install(base)
    try:
        with client_env_scope(values):
            assert se.variant_key(base) is None
    finally:
        _cleanup(base)
