"""Carry diagnostics never echo arbitrary repository or child-process data."""
import logging
import base64
from urllib.parse import quote
import subprocess

import pytest

from hermes_cli import update_carry as carry


@pytest.mark.parametrize("value", [
    "Authorization: Bearer synthetic-bearer-ABC123",
    "https://synthetic-user:synthetic-pass@example.invalid/path",
    "synthetic-random-token-ABC123",
    "X-Api-Key: synthetic-api-key-ABC123",
    "https://example.invalid/?api_key=synthetic-query-ABC123",
])
def test_diagnostics_omit_untrusted_values(value, tmp_path, monkeypatch, capsys, caplog):
    item = {"kind": "commit", "identity": value, "sha": value, "title": value,
            "files": [value], "action": "conflict", "reason": "unknown-left-out",
            "flags": [value]}
    plan = {"items": [item], "upstream": value, "upstream_sha": value, "tip": value}
    outputs = [carry.format_report(plan, quiet=False)]
    monkeypatch.setattr(carry, "plan_repo", lambda *a, **kw: plan)
    monkeypatch.setattr(carry, "verify_accounted", lambda *a, **kw: [])
    assert carry.main(["--repo", str(tmp_path), "--json"]) == 0
    outputs.append(capsys.readouterr().out)
    with pytest.raises(carry.CarryError) as err:
        carry._ok(subprocess.CompletedProcess([], 1, "", value), "git commit")
    outputs.append(str(err.value))
    def fail(*a, **kw):
        raise RuntimeError(value)
    monkeypatch.setattr(carry, "_hook_plan", fail)
    with caplog.at_level(logging.ERROR):
        carry.reapply_after_diverged_reset(["git"], tmp_path, pre_pull_sha="HEAD", upstream_ref="HEAD")
        carry.reapply_parked_stash(["git"], tmp_path, "stash@{0}")
    outputs.extend([caplog.text, capsys.readouterr().out])
    monkeypatch.setattr(carry, "plan_repo", fail)
    assert carry.main(["--repo", str(tmp_path)]) == 1
    outputs.append(capsys.readouterr().err)
    with pytest.raises(SystemExit) as invalid:
        carry.main(["--unknown", value])
    assert invalid.value.code == 2
    outputs.append(capsys.readouterr().err)
    for output in outputs:
        assert quote(value, safe="") not in output
        assert base64.b64encode(value.encode()).decode().rstrip("=") not in output
        assert value not in output
        assert "ABC123" not in output
        assert "synthetic-user" not in output
        assert "synthetic-pass" not in output
