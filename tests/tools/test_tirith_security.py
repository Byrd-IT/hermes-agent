"""Tests for the tirith security scanning subprocess wrapper."""

import json
import os
import subprocess
import time
from unittest.mock import MagicMock, patch

import pytest

import tools.tirith_security as _tirith_mod
from tools.tirith_security import check_command_security, ensure_installed


def _reset_state():
    _tirith_mod._install_attempted.clear()
    _tirith_mod._install_threads.clear()
    _tirith_mod._crash_count = 0
    _tirith_mod._circuit_open = False
    _tirith_mod._circuit_open_at = 0.0


@pytest.fixture(autouse=True)
def _reset_tirith_state():
    _reset_state()
    yield
    _reset_state()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_run(returncode=0, stdout="", stderr=""):
    """Build a mock subprocess.CompletedProcess."""
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


def _json_stdout(findings=None, summary=""):
    return json.dumps({"findings": findings or [], "summary": summary})


# ---------------------------------------------------------------------------
# Exit code → action mapping
# ---------------------------------------------------------------------------

class TestExitCodeMapping:
    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_exit_0_allow(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        mock_run.return_value = _mock_run(0, _json_stdout())
        result = check_command_security("echo hello")
        assert result["action"] == "allow"
        assert result["findings"] == []

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_exit_1_block_with_findings(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        findings = [{"rule_id": "homograph_url", "severity": "high"}]
        mock_run.return_value = _mock_run(1, _json_stdout(findings, "homograph detected"))
        result = check_command_security("curl http://gооgle.com")
        assert result["action"] == "block"
        assert len(result["findings"]) == 1
        assert result["summary"] == "homograph detected"

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_exit_2_warn_with_findings(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        findings = [{"rule_id": "shortened_url", "severity": "medium"}]
        mock_run.return_value = _mock_run(2, _json_stdout(findings, "shortened URL"))
        result = check_command_security("curl https://bit.ly/abc")
        assert result["action"] == "warn"
        assert len(result["findings"]) == 1
        assert result["summary"] == "shortened URL"


# ---------------------------------------------------------------------------
# JSON parse failure (exit code still wins)
# ---------------------------------------------------------------------------

class TestJsonParseFailure:
    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_exit_1_invalid_json_still_blocks(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        mock_run.return_value = _mock_run(1, "NOT JSON")
        result = check_command_security("bad command")
        assert result["action"] == "block"
        assert "details unavailable" in result["summary"]

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_exit_0_invalid_json_allows(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        mock_run.return_value = _mock_run(0, "NOT JSON")
        result = check_command_security("safe command")
        assert result["action"] == "allow"


# ---------------------------------------------------------------------------
# Operational failures + fail_open
# ---------------------------------------------------------------------------

class TestOSErrorFailOpen:
    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_file_not_found_fail_open(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        mock_run.side_effect = FileNotFoundError("No such file: tirith")
        result = check_command_security("echo hi")
        assert result["action"] == "allow"
        assert "unavailable" in result["summary"]

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_os_error_fail_closed(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": False}
        mock_run.side_effect = FileNotFoundError("No such file: tirith")
        result = check_command_security("echo hi")
        assert result["action"] == "block"
        assert "fail-closed" in result["summary"]


class TestTimeoutFailOpen:
    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_timeout_fail_closed(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": False}
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="tirith", timeout=5)
        result = check_command_security("slow command")
        assert result["action"] == "block"
        assert "fail-closed" in result["summary"]


class TestUnknownExitCode:
    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_unknown_exit_code_fail_closed(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": False}
        mock_run.return_value = _mock_run(99, "")
        result = check_command_security("cmd")
        assert result["action"] == "block"
        assert "exit code 99" in result["summary"]


# ---------------------------------------------------------------------------
# Circuit breaker: half-open recovery
# ---------------------------------------------------------------------------

def _open_breaker(age_s):
    """Put the breaker in the open state as if it tripped ``age_s`` seconds ago."""
    _tirith_mod._crash_count = _tirith_mod._CRASH_LIMIT
    _tirith_mod._circuit_open = True
    _tirith_mod._circuit_open_at = time.monotonic() - age_s


class TestCircuitBreakerHalfOpen:
    @pytest.mark.parametrize("returncode, action", [(0, "allow"), (1, "block"), (2, "warn")])
    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_completed_probe_after_retry_window_closes_breaker(self, mock_cfg, mock_run, returncode, action):
        """Once the retry window has elapsed, one real scan runs; any verdict (allow/block/warn)
        proves the binary healthy and closes the breaker, so the next command is scanned again."""
        mock_cfg.return_value = _CFG
        _open_breaker(age_s=_tirith_mod._CIRCUIT_RETRY_S + 1)
        mock_run.return_value = _mock_run(returncode, _json_stdout())

        result = check_command_security("echo hi")

        assert result["action"] == action
        assert mock_run.call_count == 1
        assert (_tirith_mod._circuit_open, _tirith_mod._crash_count) == (False, 0)
        # Breaker closed: the following command is scanned rather than short-circuited.
        check_command_security("echo again")
        assert mock_run.call_count == 2

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_open_breaker_probes_once_per_window_and_failed_probe_rearms(self, mock_cfg, mock_run):
        """Inside the window nothing spawns; after it exactly one probe runs, and a probe that
        fails re-arms the window so the next caller is fail-open without spawning again."""
        mock_cfg.return_value = _CFG
        _open_breaker(age_s=1)
        mock_run.side_effect = OSError("binary gone")

        assert check_command_security("echo hi")["summary"] == "tirith disabled (circuit breaker)"
        assert mock_run.call_count == 0

        _open_breaker(age_s=_tirith_mod._CIRCUIT_RETRY_S + 1)
        assert check_command_security("echo hi")["action"] == "allow"  # probe spawned and failed
        assert mock_run.call_count == 1
        assert _tirith_mod._circuit_open is True
        assert check_command_security("echo hi")["summary"] == "tirith disabled (circuit breaker)"
        assert mock_run.call_count == 1  # re-armed: no second probe inside the fresh window


# ---------------------------------------------------------------------------
# Disabled
# ---------------------------------------------------------------------------

class TestDisabled:
    @patch("tools.tirith_security._load_security_config")
    def test_disabled_returns_allow(self, mock_cfg):
        mock_cfg.return_value = {"tirith_enabled": False, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        result = check_command_security("rm -rf /")
        assert result["action"] == "allow"


# ---------------------------------------------------------------------------
# Findings cap + summary cap
# ---------------------------------------------------------------------------

class TestCaps:
    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_findings_and_summary_capped(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        findings = [{"rule_id": f"rule_{i}"} for i in range(100)]
        mock_run.return_value = _mock_run(2, _json_stdout(findings, "x" * 1000))
        result = check_command_security("cmd")
        assert len(result["findings"]) == 50
        assert len(result["summary"]) == 500


# ---------------------------------------------------------------------------
# Programming errors propagate
# ---------------------------------------------------------------------------

class TestProgrammingErrors:
    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_attribute_error_propagates(self, mock_cfg, mock_run):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        mock_run.side_effect = AttributeError("unexpected bug")
        with pytest.raises(AttributeError):
            check_command_security("cmd")


# ---------------------------------------------------------------------------
# ensure_installed
# ---------------------------------------------------------------------------

class TestEnsureInstalled:
    @patch("tools.tirith_security._load_security_config")
    def test_disabled_returns_none(self, mock_cfg):
        mock_cfg.return_value = {"tirith_enabled": False, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        assert ensure_installed() is None

    @patch("tools.tirith_security.shutil.which", return_value="/usr/local/bin/tirith")
    @patch("tools.tirith_security._load_security_config")
    def test_found_on_path_returns_immediately(self, mock_cfg, mock_which):
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        assert ensure_installed() == "/usr/local/bin/tirith"


# ---------------------------------------------------------------------------
# Unsupported platform (Windows etc.) — silent fast-path everywhere
# ---------------------------------------------------------------------------

class TestUnsupportedPlatform:
    """When PM has no tirith build for this OS+arch, the entire subsystem
    must stay silent: no install thread, no spawn attempts, no CLI banner. Pattern-matching
    guards still cover the gap; tirith content scanning is just absent."""

    @pytest.mark.parametrize("target, expected", [
        ("linux-x64", True),
        ("win32-x64", False),
        (RuntimeError("unsupported architecture: riscv64"), False),
    ])
    def test_is_platform_supported(self, target, expected):
        # Table inputs, not a host fake: support is PM's per-target mapping.
        current_target = MagicMock(side_effect=[target])
        with patch("pm.current_target", current_target):
            assert _tirith_mod.is_platform_supported() is expected

    @patch("tools.tirith_security._load_security_config")
    def test_check_command_security_unsupported_allows_silently(self, mock_cfg):
        """Windows: skip the resolver and spawn entirely — return allow with
        an empty summary so callers can't accidentally surface 'tirith
        unavailable' messaging to the user."""
        mock_cfg.return_value = {"tirith_enabled": True, "tirith_path": "tirith",
                                 "tirith_timeout": 5, "tirith_fail_open": True}
        with patch("tools.tirith_security.is_platform_supported", return_value=False), \
             patch("tools.tirith_security.subprocess.run") as mock_run, \
             patch("tools.tirith_security._resolve_tirith_path") as mock_resolve:
            result = check_command_security("rm -rf /")
            assert result == {"action": "allow", "findings": [], "summary": ""}
            mock_run.assert_not_called()
            mock_resolve.assert_not_called()

    def test_explicit_path_still_honored_on_unsupported_platform(self, tmp_path):
        """If a user explicitly configured a tirith_path (e.g. they built it
        themselves under WSL), the unsupported-platform short-circuit must
        NOT override that — explicit config wins."""
        custom = tmp_path / "tirith"
        custom.write_text("#!/bin/sh\nexit 0\n")
        custom.chmod(0o755)
        with patch("tools.tirith_security.is_platform_supported", return_value=False):
            assert _tirith_mod._resolve_tirith_path(str(custom)) == str(custom)


# ---------------------------------------------------------------------------
# PM-provisioned binary: one install attempt per home, explicit paths never download
# ---------------------------------------------------------------------------

_BARE_CFG = {"tirith_enabled": True, "tirith_path": "tirith",
             "tirith_timeout": 5, "tirith_fail_open": True}


@pytest.fixture
def pm_tirith(monkeypatch):
    """Nothing on PATH, nothing installed yet, lazy installs allowed."""
    import pm

    installed = MagicMock(return_value=None)
    ensure = MagicMock()
    monkeypatch.setattr("tools.tirith_security.shutil.which", lambda _name: None)
    monkeypatch.setattr(pm, "installed_package", installed)
    monkeypatch.setattr(pm, "ensure", ensure)
    monkeypatch.setattr(pm, "lazy_installs_allowed", lambda: True)
    return ensure, installed


class TestPmInstall:
    def test_default_path_installs_through_pm(self, pm_tirith):
        """The default bare 'tirith' is provisioned by PM on a cold scan."""
        ensure, installed = pm_tirith
        ensure.side_effect = lambda *_a, **_k: setattr(
            installed, "return_value", MagicMock(binary="/pm/tirith"))

        assert _tirith_mod._resolve_tirith_path("tirith") == "/pm/tirith"
        ensure.assert_called_once_with("tirith")

    def test_failed_install_is_not_retried(self, pm_tirith):
        """After a failed install, subsequent resolves fall back without retrying."""
        ensure, _ = pm_tirith
        ensure.side_effect = RuntimeError("download failed")

        assert _tirith_mod._resolve_tirith_path("tirith") == "tirith"
        assert _tirith_mod._resolve_tirith_path("tirith") == "tirith"
        assert ensure.call_count == 1

    def test_tilde_explicit_path_missing_no_download(self, pm_tirith):
        """An explicit ~/path that doesn't exist must NOT trigger an install."""
        ensure, _ = pm_tirith

        result = _tirith_mod._resolve_tirith_path("~/bin/tirith")

        ensure.assert_not_called()
        assert "~" not in result  # tilde still expanded

    def test_install_proceeds_without_cosign(self, tmp_path):
        """Provenance is optional without cosign: SHA-256 verification alone proceeds."""
        with patch("tools.tirith_security.shutil.which", return_value=None):
            verified, reason = _tirith_mod.verify_release_provenance(tmp_path, MagicMock())
        assert (verified, reason) == (False, "")


# ---------------------------------------------------------------------------
# Background install / non-blocking startup (P2)
# ---------------------------------------------------------------------------

class TestBackgroundInstall:
    def test_ensure_installed_non_blocking(self, pm_tirith):
        """ensure_installed must return immediately when an install is needed."""
        with patch("tools.tirith_security._load_security_config", return_value=_BARE_CFG), \
             patch("tools.tirith_security.is_platform_supported", return_value=True), \
             patch("tools.tirith_security.threading.Thread") as MockThread:
            assert ensure_installed() is None  # not available yet
            MockThread.assert_called_once()
            MockThread.return_value.start.assert_called_once()

    def test_scan_does_not_wait_on_startup_install(self, pm_tirith):
        """A scan during the startup install returns the default instead of installing again."""
        ensure, _ = pm_tirith
        with patch("tools.tirith_security._load_security_config", return_value=_BARE_CFG), \
             patch("tools.tirith_security.is_platform_supported", return_value=True), \
             patch("tools.tirith_security.threading.Thread"):
            ensure_installed()

        assert _tirith_mod._resolve_tirith_path("tirith") == "tirith"
        ensure.assert_not_called()


# ---------------------------------------------------------------------------
# Warn-once dedupe (issue: tirith spawn failed spamming on Windows)
# ---------------------------------------------------------------------------

class TestSpawnWarningDedup:
    """When tirith isn't installed yet (background install in flight, or
    install marked failed), every terminal command spammed an identical
    ``tirith spawn failed: [WinError 2]`` warning to ``errors.log``. The
    dedupe set in ``_warn_once`` collapses repeats by ``(exc class, errno)``
    while still surfacing the first occurrence so users see the failure.
    """

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_repeated_spawn_failure_logs_once(self, mock_cfg, mock_run, caplog):
        mock_cfg.return_value = {
            "tirith_enabled": True, "tirith_path": "tirith",
            "tirith_timeout": 5, "tirith_fail_open": True,
        }
        mock_run.side_effect = FileNotFoundError("[WinError 2]")
        # Fresh dedupe state — clear any keys left by other tests.
        _tirith_mod._warned_messages.clear()

        with caplog.at_level("WARNING", logger="tools.tirith_security"):
            for i in range(15):
                result = check_command_security("echo hi")
                # Behavior must remain the same on every call —
                # fail-open allow, with the exception captured in summary.
                assert result["action"] == "allow"
                if i < _tirith_mod._CRASH_LIMIT:
                    # Before circuit breaker opens, summary has the exception
                    assert "unavailable" in result["summary"]
                else:
                    # After circuit breaker opens, summary is generic
                    assert "circuit breaker" in result["summary"]

        spawn_warnings = [
            rec for rec in caplog.records
            if "tirith spawn failed" in rec.message
        ]
        assert len(spawn_warnings) == 1, (
            f"expected exactly 1 spawn-failed warning across 15 commands, "
            f"got {len(spawn_warnings)}: {[r.message for r in spawn_warnings]}"
        )


# ---------------------------------------------------------------------------
# .app TLD suppression (issue #24461)
# ---------------------------------------------------------------------------

_CFG = {"tirith_enabled": True, "tirith_path": "tirith",
        "tirith_timeout": 5, "tirith_fail_open": True}


class TestAppTldSuppression:
    """warn verdicts whose only finding is lookalike_tld/.app are downgraded to allow."""

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_app_only_warn_downgraded_to_allow(self, mock_cfg, mock_run):
        mock_cfg.return_value = _CFG
        findings = [{"rule_id": "lookalike_tld", "value": ".app",
                     "message": "Domain uses '.app' TLD which can be confused with file extensions"}]
        mock_run.return_value = _mock_run(2, _json_stdout(findings, ".app TLD warning"))
        result = check_command_security("curl https://example.app")
        assert result["action"] == "allow"
        assert result["findings"] == []
        assert result["summary"] == ""

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_mixed_findings_preserve_warn(self, mock_cfg, mock_run):
        """If .app finding is accompanied by another finding, warn is preserved."""
        mock_cfg.return_value = _CFG
        findings = [
            {"rule_id": "lookalike_tld", "value": ".app"},
            {"rule_id": "shortened_url", "severity": "medium"},
        ]
        mock_run.return_value = _mock_run(2, _json_stdout(findings, "mixed"))
        result = check_command_security("curl https://bit.ly/test.app")
        assert result["action"] == "warn"
        assert len(result["findings"]) == 2

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_block_verdict_never_suppressed(self, mock_cfg, mock_run):
        """block exit code is never downgraded, even if finding looks like .app."""
        mock_cfg.return_value = _CFG
        findings = [{"rule_id": "lookalike_tld", "value": ".app"}]
        mock_run.return_value = _mock_run(1, _json_stdout(findings, "block"))
        result = check_command_security("curl https://example.app")
        assert result["action"] == "block"


class TestEmojiVariationSelectorSuppression:
    """VS16 after an emoji-capable base is presentation, not obfuscation: no approval prompt."""

    _VS = [{"rule_id": "variation_selector", "severity": "medium"}]

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_emoji_only_variation_selector_warn_is_downgraded(self, mock_cfg, mock_run):
        mock_cfg.return_value = _CFG
        mock_run.return_value = _mock_run(2, _json_stdout(self._VS, "variation selector"))

        # SMP emoji, Dingbats/Misc Symbols, and BMP singletons outside those blocks (ℹ ▶).
        result = check_command_security('ls "🗞️ Journal/" "✅️ Projects/" "ℹ️ Info/" "▶️ Media/"')

        assert result == {"action": "allow", "findings": [], "summary": ""}

    @pytest.mark.parametrize("command, findings", [
        ("printf 'a️'", _VS),            # VS16 after a letter
        ("printf '0️'", _VS),            # VS16 after a digit (keycap base)
        ("printf 'x󠄀'", _VS),        # a non-VS16 selector
        ('curl https://bit.ly/x --output "🗞️ Journal/file"',  # emoji path + another finding
         _VS + [{"rule_id": "shortened_url", "severity": "medium"}]),
    ])
    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_other_selectors_or_mixed_findings_keep_warn(self, mock_cfg, mock_run, command, findings):
        mock_cfg.return_value = _CFG
        mock_run.return_value = _mock_run(2, _json_stdout(findings, "variation selector"))

        result = check_command_security(command)

        assert result["action"] == "warn"
        assert result["findings"] == findings




# ---------------------------------------------------------------------------
# Cache-warm rescan (tirith <= 0.4.2 shared enrichment-budget workaround)
# ---------------------------------------------------------------------------

class TestCacheWarmRescan:
    """t_8802c7db: multi-package installs warn 'deadline exhausted' on a cold cache because
    tirith's per-run enrichment budget is shared; solo per-package scans warm the persistent
    cache and one re-scan completes. Fail-closed preserved: the original warn stands whenever
    the warm phase cannot run or the re-scan still warns."""

    CFG = {"tirith_enabled": True, "tirith_path": "tirith", "tirith_timeout": 5,
           "tirith_fail_open": True}

    def _incomplete(self, pkg):
        return {"rule_id": "analysis_incomplete", "severity": "medium",
                "description": (f"Tirith could not complete every configured runtime "
                                f"threat-intelligence check for package '{pkg}' "
                                f"(OSV lookup deadline exhausted; ecosyste.ms metadata "
                                f"lookup deadline exhausted)")}

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_cold_multi_package_allow_after_rescan(self, mock_cfg, mock_run):
        mock_cfg.return_value = dict(self.CFG)
        # 1st call: cold multi-package scan -> warn (deadline exhausted). Solo warm scans
        # (#2..#N): allow (cache warmed). Final re-scan: allow (budget now fits).
        mock_run.side_effect = [
            _mock_run(2, _json_stdout([self._incomplete("werift"), self._incomplete("ws")],
                                      "threat intel incomplete")),
            _mock_run(0, _json_stdout()),
            _mock_run(0, _json_stdout()),
            _mock_run(0, _json_stdout()),
        ]
        result = check_command_security("npm install werift ws")
        assert result["action"] == "allow"
        assert mock_run.call_count == 4
        cmds = [c.args[0][-1] for c in mock_run.call_args_list]
        assert cmds[1] == "npm install werift"
        assert cmds[2] == "npm install ws"
        assert cmds[3] == "npm install werift ws"

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_rescan_warn_after_warm_keeps_warn(self, mock_cfg, mock_run):
        # Upstream genuinely dead: warm scans allow but the re-scan STILL warns -> the
        # original warn verdict stands (never fails open).
        mock_cfg.return_value = dict(self.CFG)
        warn = _mock_run(2, _json_stdout([self._incomplete("werift")], "threat intel incomplete"))
        mock_run.side_effect = [
            warn,
            _mock_run(0, _json_stdout()),
            warn,
        ]
        result = check_command_security("npm install werift")
        assert result["action"] == "warn"
        assert len(result["findings"]) == 1

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_warm_phase_operational_failure_keeps_original_warn(self, mock_cfg, mock_run):
        # Binary dies mid-warm (operational trouble in a solo scan): returncode -9 is not a
        # known exit action -> _tirith_check returns None -> original verdict stands untouched.
        mock_cfg.return_value = dict(self.CFG)
        warn = _mock_run(2, _json_stdout([self._incomplete("werift")], "threat intel incomplete"))
        mock_run.side_effect = [warn, _mock_run(-9, "")]
        result = check_command_security("npm install werift")
        assert result["action"] == "warn"

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_unknown_package_manager_no_rescan(self, mock_cfg, mock_run):
        # Manager cannot be determined from the command: no warm phase runs at all.
        mock_cfg.return_value = dict(self.CFG)
        mock_run.return_value = _mock_run(2, _json_stdout([self._incomplete("werift")], "x"))
        result = check_command_security("cargo install werift")
        assert result["action"] == "warn"
        assert mock_run.call_count == 1

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_pip_gets_pip_warm_commands(self, mock_cfg, mock_run):
        mock_cfg.return_value = dict(self.CFG)
        mock_run.side_effect = [
            _mock_run(2, _json_stdout([self._incomplete("requests")], "x")),
            _mock_run(0, _json_stdout()),
            _mock_run(0, _json_stdout()),
        ]
        result = check_command_security("pip install requests")
        assert result["action"] == "allow"
        cmds = [c.args[0][-1] for c in mock_run.call_args_list]
        assert cmds[1] == "pip install requests"

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_yarn_add_gets_yarn_warm_command(self, mock_cfg, mock_run):
        mock_cfg.return_value = dict(self.CFG)
        mock_run.side_effect = [
            _mock_run(2, _json_stdout([self._incomplete("left-pad")], "x")),
            _mock_run(0, _json_stdout()),
            _mock_run(0, _json_stdout()),
        ]
        result = check_command_security("yarn add left-pad")
        assert result["action"] == "allow"
        cmds = [c.args[0][-1] for c in mock_run.call_args_list]
        assert cmds[1] == "yarn add left-pad"

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_warm_scan_limit_caps_solo_scans(self, mock_cfg, mock_run):
        mock_cfg.return_value = dict(self.CFG)
        findings = [self._incomplete(f"pkg{i}") for i in range(20)]
        mock_run.side_effect = (
            [_mock_run(2, _json_stdout(findings, "x"))]
            + [_mock_run(0, _json_stdout()) for _ in range(_tirith_mod._WARM_SCAN_LIMIT)]
            + [_mock_run(0, _json_stdout())])
        result = check_command_security("npm install " + " ".join(f"pkg{i}" for i in range(20)))
        assert result["action"] == "allow"
        # 1 original + 12 warm + 1 rescan = 14 spawns; never 20.
        assert mock_run.call_count == _tirith_mod._WARM_SCAN_LIMIT + 2

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_non_incomplete_findings_skip_rescan(self, mock_cfg, mock_run):
        # Real findings (not analysis_incomplete) must NOT trigger the warm/rescan path.
        mock_cfg.return_value = dict(self.CFG)
        findings = [{"rule_id": "homograph_url", "severity": "high"}]
        mock_run.return_value = _mock_run(2, _json_stdout(findings, "homograph detected"))
        result = check_command_security("npm install werift")
        assert result["action"] == "warn"
        assert mock_run.call_count == 1

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_artifact_tokens_not_warmed(self, mock_cfg, mock_run):
        # A name that is a plain token of the command text (redirection artifact shape) is
        # never used to construct a warm command, and since t_2550b91f the phantom warn
        # itself is suppressed warn-only: `npm install werift > foo.txt` warns solely on
        # the redirect target 'foo.txt', which drops -> allow before any warm phase runs.
        mock_cfg.return_value = dict(self.CFG)
        mock_run.return_value = _mock_run(2, _json_stdout([self._incomplete("foo.txt")], "x"))
        result = check_command_security("npm install werift > foo.txt")
        # The artifact is filtered, no valid packages remain, no warm phase runs.
        assert result["action"] == "allow"
        assert mock_run.call_count == 1

    def test_pkgname_charset_rejects_shell_metachars(self):
        from tools.tirith_security import _PKGNAME_OK
        for bad in ["a;rm", "a&&b", "$(x)", "a|b", "a b", "`x`", "a\nb", "-x", "--flag"]:
            assert not _PKGNAME_OK.match(bad), bad
        for good in ["werift", "@scope/pkg", "pkg.name", "pkg_name", "pkg-name", "pkg==1.0"]:
            assert _PKGNAME_OK.match(good), good

    def test_warm_command_pm_aware(self):
        from tools.tirith_security import _warm_command
        assert _warm_command("npm", "werift") == "npm install werift"
        assert _warm_command("pnpm", "werift") == "pnpm install werift"
        assert _warm_command("yarn", "left-pad") == "yarn add left-pad"
        assert _warm_command("pip", "requests") == "pip install requests"

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_fail_open_config_rescan_block_returned_verbatim(self, mock_cfg, mock_run):
        # fail_open governs OPERATIONAL failures only (spawn/timeout/unknown exit, per
        # TestFailOpenBehavior); verdict-level warns/blocks from tirith are returned
        # verbatim even with fail_open=True (existing test_exit_1_block_with_findings
        # pins this for first scans). The rescan path must behave identically: a block
        # after the rescan is NEVER downgraded.
        mock_cfg.return_value = dict(self.CFG)
        warn = _mock_run(2, _json_stdout([self._incomplete("werift")], "x"))
        block = _mock_run(1, _json_stdout([{"rule_id": "malicious_script", "severity": "high"}],
                                          "malicious"))
        mock_run.side_effect = [warn, _mock_run(0, _json_stdout()), block]
        result = check_command_security("npm install werift")
        assert result["action"] == "block"
        assert len(result["findings"]) == 1

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_fail_closed_config_rescan_block_still_blocks(self, mock_cfg, mock_run):
        # Fail-closed: the rescan's block verdict is returned verbatim (no weakening, no
        # downgrade) — the important direction for single-query mode.
        mock_cfg.return_value = {**self.CFG, "tirith_fail_open": False}
        warn = _mock_run(2, _json_stdout([self._incomplete("werift")], "x"))
        block = _mock_run(1, _json_stdout([{"rule_id": "malicious_script", "severity": "high"}],
                                          "malicious"))
        mock_run.side_effect = [warn, _mock_run(0, _json_stdout()), block]
        result = check_command_security("npm install werift")
        assert result["action"] == "block"
        assert len(result["findings"]) == 1

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_fail_closed_config_warns_survive(self, mock_cfg, mock_run):
        mock_cfg.return_value = {**self.CFG, "tirith_fail_open": False}
        warn = _mock_run(2, _json_stdout([self._incomplete("werift")], "x"))
        mock_run.side_effect = [warn, _mock_run(0, _json_stdout()), warn]
        result = check_command_security("npm install werift")
        assert result["action"] == "warn"

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_first_scan_operational_failure_still_crash_accounted(self, mock_cfg, mock_run):
        # The refactor kept the crash accounting: FileNotFoundError on the FIRST scan counts
        # one crash and respects fail_open (allow — the long-standing fail-open contract).
        mock_cfg.return_value = dict(self.CFG)
        mock_run.side_effect = FileNotFoundError(2, "No such file", "tirith")
        result = check_command_security("echo hi")
        assert result["action"] == "allow"
        assert _tirith_mod._crash_count == 1

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_first_scan_timeout_fail_closed_counts_crash(self, mock_cfg, mock_run):
        mock_cfg.return_value = {**self.CFG, "tirith_fail_open": False}
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="tirith", timeout=5)
        result = check_command_security("echo hi")
        assert result["action"] == "block"
        assert _tirith_mod._crash_count == 1


# ---------------------------------------------------------------------------
# analysis_incomplete nested-loop false-positive suppressor (t_0fb18e49)
#
# tirith 0.4.2 hard-BLOCKS `while [ ... ]`/`until [ ... ]`/`for`/`if` compounds
# (rule analysis_incomplete, "dynamic shell wrapper body" + "nested shell
# execution coverage gap") even when every leaf command is read-only — and in
# single-query mode there is no user to approve, so the command just dies.
# POSIX defines `[ args ]` as exactly `test args`, so the wrapper rewrites the
# bracket spans to `test`, requires every leaf command to be provably
# read-only, and re-scans; the block is downgraded ONLY when the rewritten copy
# re-scans as a clean allow. The read-only gate is NOT optional: tirith 0.4.2
# cannot see destructive bodies inside while-loops (verified live:
# `while test ! -f x; do sudo rm -rf /opt/x; done` scans ALLOW), so a bare
# rescan-clean gate would un-block destructive watcher loops.
# ---------------------------------------------------------------------------

_FP_LOOP_BLOCK = {"rule_id": "analysis_incomplete", "severity": "high",
                  "title": "Nested executable body could not be resolved"}
_FP_LOOP_GAP = {"rule_id": "analysis_incomplete", "severity": "high",
                "title": "nested command analysis was incomplete"}

_LOOP_FP_CFG = {"tirith_enabled": True, "tirith_path": "tirith",
                "tirith_timeout": 5, "tirith_fail_open": True}

# The exact watcher shape from t_0fb18e49 (benign bounded poll-watcher).
_FP_LOOP_CMD = ("while [ ! -f /tmp/tpwd_repro_done ]; do sleep 30; "
                "ls /home/brandonabyrd/projects/supervisor/logs | grep 2026-09-16; done")
_FP_LOOP_REWRITTEN = ("while test ! -f /tmp/tpwd_repro_done; do sleep 30; "
                      "ls /home/brandonabyrd/projects/supervisor/logs | grep 2026-09-16; done")


class TestLoopAnalysisIncompleteSuppressor:
    CFG = _LOOP_FP_CFG

    def _fp_block(self):
        return _mock_run(1, _json_stdout([dict(_FP_LOOP_BLOCK), dict(_FP_LOOP_GAP)],
                                         "nested analysis incomplete"))

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_benign_while_loop_downgraded_to_allow(self, mock_cfg, mock_run):
        mock_cfg.return_value = dict(self.CFG)
        mock_run.side_effect = [_mock_run(1, _json_stdout(
            [dict(_FP_LOOP_BLOCK), dict(_FP_LOOP_GAP)], "nested")), _mock_run(0, _json_stdout())]
        result = check_command_security(_FP_LOOP_CMD)
        assert result["action"] == "allow"
        assert result["findings"] == []
        # First scan = original text, second = the rewritten copy.
        cmds = [c.args[0][-1] for c in mock_run.call_args_list]
        assert cmds == [_FP_LOOP_CMD, _FP_LOOP_REWRITTEN]

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_mixed_findings_not_downgraded(self, mock_cfg, mock_run):
        # Any non-FP finding beside the analysis_incomplete pair -> suppressor must not run.
        mock_cfg.return_value = dict(self.CFG)
        findings = [dict(_FP_LOOP_BLOCK),
                    {"rule_id": "curl_pipe_shell", "severity": "high", "title": "Pipe to interpreter"}]
        mock_run.return_value = _mock_run(1, _json_stdout(findings, "mixed"))
        result = check_command_security(_FP_LOOP_CMD)
        assert result["action"] == "block"
        assert mock_run.call_count == 1

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_destructive_body_blocks_despite_clean_rescan(self, mock_cfg, mock_run):
        # tirith 0.4.2 cannot see `sudo rm -rf` inside while bodies even in test form
        # (verified live: the rewritten copy scans ALLOW) — the read-only leaf gate is
        # what keeps this blocked. No rescan may even run.
        mock_cfg.return_value = dict(self.CFG)
        mock_run.side_effect = [
            _mock_run(1, _json_stdout([dict(_FP_LOOP_BLOCK), dict(_FP_LOOP_GAP)], "nested")),
            _mock_run(0, _json_stdout())]
        cmd = "while [ ! -f /tmp/tpwd_dest_done ]; do sudo rm -rf /opt/tpwd_dest; sleep 1; done"
        result = check_command_security(cmd)
        assert result["action"] == "block"
        assert mock_run.call_count == 1

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_rm_body_blocks(self, mock_cfg, mock_run):
        mock_cfg.return_value = dict(self.CFG)
        mock_run.side_effect = [
            _mock_run(1, _json_stdout([dict(_FP_LOOP_BLOCK), dict(_FP_LOOP_GAP)], "nested")),
            _mock_run(0, _json_stdout())]
        cmd = "while [ ! -f /tmp/tpwd_dest_done ]; do rm -rf /tmp/tpwd_dest_gone; sleep 1; done"
        result = check_command_security(cmd)
        assert result["action"] == "block"
        assert mock_run.call_count == 1

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_command_substitution_body_blocks_without_rescan(self, mock_cfg, mock_run):
        # tirith 0.4.2 does NOT analyze $(...) bodies (verified live: a $(touch ...)
        # loop scans ALLOW), so any command substitution disqualifies the downgrade.
        mock_cfg.return_value = dict(self.CFG)
        mock_run.side_effect = [
            _mock_run(1, _json_stdout([dict(_FP_LOOP_BLOCK), dict(_FP_LOOP_GAP)], "nested")),
            _mock_run(0, _json_stdout())]
        cmd = "while [ ! -f /tmp/x ]; do echo \"$(touch /tmp/tirith_pwn)\"; sleep 1; done"
        result = check_command_security(cmd)
        assert result["action"] == "block"
        assert mock_run.call_count == 1

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_variable_exec_body_blocks(self, mock_cfg, mock_run):
        mock_cfg.return_value = dict(self.CFG)
        mock_run.side_effect = [
            _mock_run(1, _json_stdout([dict(_FP_LOOP_BLOCK), dict(_FP_LOOP_GAP)], "nested")),
            _mock_run(0, _json_stdout())]
        cmd = "while [ ! -f /tmp/x ]; do \"$p\" --version; sleep 1; done"
        result = check_command_security(cmd)
        assert result["action"] == "block"
        assert mock_run.call_count == 1

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_rescan_real_block_keeps_original_block(self, mock_cfg, mock_run):
        # Readonly-leaf command, but the rewritten copy trips a REAL rule on rescan:
        # only a clean ALLOW downgrades — the original block is returned verbatim.
        mock_cfg.return_value = dict(self.CFG)
        real = [{"rule_id": "curl_pipe_shell", "severity": "high", "title": "Pipe to interpreter"}]
        mock_run.side_effect = [
            _mock_run(1, _json_stdout([dict(_FP_LOOP_BLOCK), dict(_FP_LOOP_GAP)], "nested")),
            _mock_run(1, _json_stdout(real, "pipe"))]
        cmd = "while [ ! -f /tmp/x ]; do sleep 1; ls /tmp | grep z; done"
        result = check_command_security(cmd)
        assert result["action"] == "block"
        assert [f["rule_id"] for f in result["findings"]] == [
            "analysis_incomplete", "analysis_incomplete"]
        assert mock_run.call_count == 2

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_rescan_still_fp_pair_keeps_block(self, mock_cfg, mock_run):
        # Rescan of the rewritten copy still returns the FP pair (e.g. a body tirith
        # itself cannot cover): fail-closed, block stands.
        mock_cfg.return_value = dict(self.CFG)
        mock_run.side_effect = [
            _mock_run(1, _json_stdout([dict(_FP_LOOP_BLOCK), dict(_FP_LOOP_GAP)], "nested")),
            _mock_run(1, _json_stdout([dict(_FP_LOOP_BLOCK)], "nested again"))]
        cmd = "while [ ! -f /tmp/x ]; do ls /tmp | grep z; sleep 1; done"
        result = check_command_security(cmd)
        assert result["action"] == "block"

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_rescan_operational_failure_keeps_block(self, mock_cfg, mock_run):
        mock_cfg.return_value = dict(self.CFG)
        mock_run.side_effect = [
            _mock_run(1, _json_stdout([dict(_FP_LOOP_BLOCK), dict(_FP_LOOP_GAP)], "nested")),
            _mock_run(-9, "")]
        result = check_command_security(_FP_LOOP_CMD)
        assert result["action"] == "block"
        assert mock_run.call_count == 2

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_quoted_span_not_downgraded(self, mock_cfg, mock_run):
        # Quotes inside a bracket span are skipped (conservative: no quote parsing) —
        # nothing rewritten -> no rescan -> block stands.
        mock_cfg.return_value = dict(self.CFG)
        mock_run.side_effect = [
            _mock_run(1, _json_stdout([dict(_FP_LOOP_BLOCK), dict(_FP_LOOP_GAP)], "nested")),
            _mock_run(0, _json_stdout())]
        cmd = "while [ \"$i\" -lt 3 ]; do sleep 1; done"
        result = check_command_security(cmd)
        assert result["action"] == "block"
        assert mock_run.call_count == 1

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_non_devnull_redirect_blocks_downgrade(self, mock_cfg, mock_run):
        # A leaf that redirects output to a real file is NOT read-only.
        mock_cfg.return_value = dict(self.CFG)
        mock_run.side_effect = [
            _mock_run(1, _json_stdout([dict(_FP_LOOP_BLOCK), dict(_FP_LOOP_GAP)], "nested")),
            _mock_run(0, _json_stdout())]
        cmd = "while [ ! -f /tmp/x ]; do ls /tmp > /tmp/out.log; sleep 1; done"
        result = check_command_security(cmd)
        assert result["action"] == "block"
        assert mock_run.call_count == 1

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_devnull_redirect_still_downgrades(self, mock_cfg, mock_run):
        mock_cfg.return_value = dict(self.CFG)
        mock_run.side_effect = [
            _mock_run(1, _json_stdout([dict(_FP_LOOP_BLOCK), dict(_FP_LOOP_GAP)], "nested")),
            _mock_run(0, _json_stdout())]
        cmd = "while [ ! -f /tmp/x ]; do ls /tmp >/dev/null 2>&1; sleep 1; done"
        result = check_command_security(cmd)
        assert result["action"] == "allow"

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_until_loop_downgrades(self, mock_cfg, mock_run):
        mock_cfg.return_value = dict(self.CFG)
        mock_run.side_effect = [
            _mock_run(1, _json_stdout([dict(_FP_LOOP_BLOCK), dict(_FP_LOOP_GAP)], "nested")),
            _mock_run(0, _json_stdout())]
        cmd = "until [ -f /tmp/x ]; do sleep 30; ls /tmp | grep z; done"
        result = check_command_security(cmd)
        assert result["action"] == "allow"
        cmds = [c.args[0][-1] for c in mock_run.call_args_list]
        assert cmds[1] == "until test -f /tmp/x; do sleep 30; ls /tmp | grep z; done"

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_path_assignment_body_blocks(self, mock_cfg, mock_run):
        # PATH= before a command redirects executable resolution -> not provably read-only.
        mock_cfg.return_value = dict(self.CFG)
        mock_run.side_effect = [
            _mock_run(1, _json_stdout([dict(_FP_LOOP_BLOCK), dict(_FP_LOOP_GAP)], "nested")),
            _mock_run(0, _json_stdout())]
        cmd = "while [ ! -f /tmp/x ]; do PATH=/tmp/evil date; sleep 1; done"
        result = check_command_security(cmd)
        assert result["action"] == "block"
        assert mock_run.call_count == 1

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_date_setflag_blocks_downgrade(self, mock_cfg, mock_run):
        # date is read-only EXCEPT with -s/--set (writes the system clock).
        mock_cfg.return_value = dict(self.CFG)
        mock_run.side_effect = [
            _mock_run(1, _json_stdout([dict(_FP_LOOP_BLOCK), dict(_FP_LOOP_GAP)], "nested")),
            _mock_run(0, _json_stdout())]
        cmd = "while [ ! -f /tmp/x ]; do date -s 2026-01-01; sleep 1; done"
        result = check_command_security(cmd)
        assert result["action"] == "block"
        assert mock_run.call_count == 1

    def test_rewrite_helper_shapes(self):
        rw = _tirith_mod._rewrite_bracket_tests
        assert rw(_FP_LOOP_CMD) == _FP_LOOP_REWRITTEN
        assert rw("until [ -f /tmp/x ]; do sleep 1; done") == "until test -f /tmp/x; do sleep 1; done"
        assert rw("for f in a b; do [ -f \"$f\" ]; done") is None  # $ anywhere -> abort
        assert rw("if [ -f a ]; then echo hi; fi") == "if test -f a; then echo hi; fi"
        assert rw("ls /tmp/[abc]*.log | grep z") is not None  # glob: span not on a word boundary
        # a [ preceded by / is a glob char, not a test: no span -> unchanged text
        assert rw("ls /tmp/[abc]*.log | grep z") == "ls /tmp/[abc]*.log | grep z"
        assert rw("echo \"[ -f x ]\"") is not None and rw("echo \"[ -f x ]\"") == "echo \"[ -f x ]\""
        assert rw("while [ ! -f x ]; do echo $(date); done") is None
        assert rw("while [ ]; do sleep 1; done") is None  # empty test -> skip -> unchanged

    def test_leaf_gate_shapes(self):
        leaves = _tirith_mod._extract_leaf_commands
        assert leaves(_FP_LOOP_REWRITTEN) == ["test", "sleep", "ls", "grep"]
        assert leaves("while test ! -f /tmp/x; do sudo rm -rf /opt/x; sleep 1; done") is None \
            or "sudo" in leaves("while test ! -f /tmp/x; do sudo rm -rf /opt/x; sleep 1; done")
        assert leaves("while test ! -f /tmp/x; do rm -rf /tmp/x; sleep 1; done") == ["test", "rm", "sleep"]
        assert leaves("while test ! -f /tmp/x; do echo \"$(touch /tmp/p)\"; done") is None
        assert leaves("while test ! -f /tmp/x; do FOO=1 date; done") == ["test", "date"]
        assert leaves("while test ! -f /tmp/x; do ls /tmp >/dev/null 2>&1; done") == ["test", "ls"]
        assert leaves("while test ! -f /tmp/x; do ls /tmp > /tmp/out; done") is None
        assert _tirith_mod._all_leaves_readonly(["sleep", "ls", "grep"]) is True
        assert _tirith_mod._all_leaves_readonly(["rm"]) is False
        assert _tirith_mod._all_leaves_readonly([]) is False

    def test_fp_detector_shapes(self):
        det = _tirith_mod._is_loop_analysis_fp_block
        assert det([dict(_FP_LOOP_BLOCK), dict(_FP_LOOP_GAP)]) is True
        assert det([dict(_FP_LOOP_BLOCK),
                    {"rule_id": "curl_pipe_shell", "severity": "high"}]) is False
        assert det([]) is False
        assert det([{"rule_id": "analysis_incomplete", "severity": "high",
                     "title": "some other title"}]) is False


_REAL_TIRITH = "/home/brandonabyrd/.hermes/profiles/ops-infra/bin/tirith"


@pytest.mark.skipif(not os.path.exists(_REAL_TIRITH), reason="live tirith binary not present")
class TestLoopSuppressorLiveBinary:
    """End-to-end against the real tirith 0.4.2 binary (no mocks).

    The suite-wide hermetic conftest (tests/conftest.py) force-sets
    TIRITH_ENABLED=false for every test so unit tests never spawn the real
    binary or hit the network. That env var wins over the on-disk config in
    _load_security_config(), so without overriding it here every call in
    this class short-circuits to an unconditional "allow" at the top of
    check_command_security *before* any scan runs (summary stays empty) --
    negatives silently "pass" open instead of exercising the real block
    path. monkeypatch.setenv restores the true value automatically at
    teardown, so this cannot leak into other test files/classes.
    """

    @pytest.fixture(autouse=True)
    def _enable_real_tirith(self, monkeypatch):
        monkeypatch.setenv("TIRITH_ENABLED", "true")

    def _check(self, command):
        _tirith_mod._resolved_path = _REAL_TIRITH
        _tirith_mod._crash_count = 0
        _tirith_mod._circuit_open = False
        return check_command_security(command)

    def test_benign_watcher_loop_allows(self):
        assert self._check(_FP_LOOP_CMD)["action"] == "allow"

    def test_destructive_watcher_still_blocks(self):
        cmd = "while [ ! -f /tmp/tpwd_live_done ]; do sudo rm -rf /opt/tpwd_live_gone; sleep 1; done"
        assert self._check(cmd)["action"] == "block"

    def test_rm_body_still_blocks(self):
        cmd = "while [ ! -f /tmp/tpwd_live_done2 ]; do rm -rf /tmp/tpwd_live_gone; sleep 1; done"
        assert self._check(cmd)["action"] == "block"

    def test_command_substitution_body_still_blocks(self):
        cmd = "while [ ! -f /tmp/tpwd_live_done3 ]; do echo \"$(touch /tmp/tpwd_live_pwn)\"; done"
        assert self._check(cmd)["action"] == "block"

    def test_var_exec_still_blocks(self):
        cmd = "while [ ! -f /tmp/tpwd_live_done4 ]; do \"$p\" --version; sleep 1; done"
        assert self._check(cmd)["action"] == "block"

    def test_plain_readonly_loop_still_allows(self):
        assert self._check("echo hello")["action"] == "allow"
