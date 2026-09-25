"""Tirith pre-exec scanning. PM owns its optional pinned binary; exit codes
remain the verdict authority and operational failures obey fail_open."""

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from contextvars import copy_context
from pathlib import Path

from hermes_constants import hermes_home_key

logger = logging.getLogger(__name__)
_REPO = "sheeki03/tirith"
# Only the release workflow may attest checksums, not arbitrary repo workflows.
_COSIGN_IDENTITY_REGEXP = f"^https://github.com/{_REPO}/\\.github/workflows/release\\.yml@refs/tags/v"
_COSIGN_ISSUER = "https://token.actions.githubusercontent.com"

# --- Config helpers ---
def _env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    return default if val is None else val.lower() in {"1", "true", "yes"}


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ[key])
    except (KeyError, ValueError):
        return default


def _load_security_config() -> dict:
    """Security settings from config.yaml, with env var overrides."""
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly().get("security", {}) or {}
    except Exception:
        cfg = {}
    return {
        "tirith_enabled": _env_bool("TIRITH_ENABLED", cfg.get("tirith_enabled", True)),
        "tirith_path": os.getenv("TIRITH_BIN", cfg.get("tirith_path", "tirith")),
        "tirith_timeout": _env_int("TIRITH_TIMEOUT", cfg.get("tirith_timeout", 5)),
        "tirith_fail_open": _env_bool("TIRITH_FAIL_OPEN", cfg.get("tirith_fail_open", True))}


# Circuit breaker: after _CRASH_LIMIT consecutive spawn/execution failures tirith is disabled so a broken
# binary can't turn every tool call into a fail-open retry loop (#41400). The breaker HALF-OPENS after
# _CIRCUIT_RETRY_S: one caller re-probes tirith for real, and any completed scan (exit 0/1/2 — allow/block/warn
# all prove the binary is healthy) closes it, while a failed probe re-arms the timer. Without the TTL this was
# a one-way latch: once open, the reset branch below was unreachable for the rest of the process.
# Thread safety: crash counting stays lock-free — a racing double-increment only opens the breaker one call
# early, which is harmless, and matches the mcp_tool.py error counters rather than the locked _warn_once
# pattern. _breaker_lock guards ONLY the half-open claim (TTL check + timestamp re-arm, nanoseconds); it is
# never held across the subprocess probe, so it cannot reintroduce the #41400 hang. Claiming re-arms
# _circuit_open_at first, so concurrent callers see a fresh TTL and stay fail-open: one probe per TTL window.
_CRASH_LIMIT = 3
_CIRCUIT_RETRY_S = 300  # half-open probe interval (seconds)
_crash_count: int = 0
_circuit_open: bool = False
_circuit_open_at: float = 0.0
_breaker_lock = threading.Lock()

# Warn-once: spawn/path warnings sit in the hot path and would otherwise repeat once per
# terminal command while tirith is unavailable (e.g. install thread still running).
_warned_messages: set[str] = set()
_warned_lock = threading.Lock()

def _record_tirith_crash() -> None:
    global _crash_count, _circuit_open, _circuit_open_at
    _crash_count += 1
    if _crash_count >= _CRASH_LIMIT:
        _circuit_open, _circuit_open_at = True, time.monotonic()
        logger.warning("tirith circuit breaker opened after %d consecutive failures; "
                       "disabling for %ds", _crash_count, _CIRCUIT_RETRY_S)


def _warn_once(key: str, message: str, *args) -> None:
    """``logger.warning`` at most once per ``key`` for the process lifetime."""
    with _warned_lock:
        if key in _warned_messages:
            return
        _warned_messages.add(key)
    logger.warning(message, *args)


def _verify_cosign(checksums_path: str, sig_path: str, cert_path: str) -> bool | None:
    """Cosign provenance of checksums.txt: True verified, False rejected, None if cosign absent/failed."""
    if not (cosign := shutil.which("cosign")):
        logger.info("cosign not found on PATH")
        return None
    try:
        result = subprocess.run(
            [cosign, "verify-blob", "--certificate", cert_path, "--signature", sig_path,
             "--certificate-identity-regexp", _COSIGN_IDENTITY_REGEXP,
             "--certificate-oidc-issuer", _COSIGN_ISSUER, checksums_path],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("cosign execution failed: %s", exc)
        return None
    if result.returncode:
        logger.warning("cosign verification failed (exit %d): %s", result.returncode, result.stderr.strip())
        return False
    logger.info("cosign provenance verification passed")
    return True


def verify_release_provenance(directory: Path, log) -> tuple[bool, str]:
    """Verify PM-acquired checksum provenance; this function never downloads.

    Missing/broken cosign is optional; an explicit rejection is fatal.
    """
    if not shutil.which("cosign"):
        logger.info("cosign not on PATH — installing tirith with SHA-256 verification only")
        return False, ""
    checksums = directory / "checksums.txt"
    signature, certificate = directory / "checksums.txt.sig", directory / "checksums.txt.pem"
    if not signature.is_file() or not certificate.is_file():
        logger.info("cosign artifacts unavailable, proceeding with SHA-256 only")
        return False, ""
    verified = _verify_cosign(str(checksums), str(signature), str(certificate))
    if verified is False:
        log("tirith install aborted: cosign provenance verification failed")
        return False, "cosign_verification_failed"
    return verified is True, ""


# One non-blocking startup attempt per routed home. Durable selection, failure
# recovery, download locks and publication belong to PM, not disk markers here.
_install_lock = threading.Lock()
_install_threads: dict[str, threading.Thread] = {}
_install_attempted: set[str] = set()


def _claim_install_attempt() -> bool:
    """Share the one-attempt budget between cold scans and startup threads."""
    with _install_lock:
        home = hermes_home_key()
        if home in _install_attempted:
            return False
        _install_attempted.add(home)
        return True


def is_platform_supported() -> bool:
    """Whether PM has a managed Tirith build for this host."""
    import pm

    try:
        return pm.get_package("tirith").missing_reason(pm.current_target()) is None
    except RuntimeError:
        return False


def _local_tirith(configured_path: str) -> str | None:
    expanded = os.path.expanduser(configured_path)
    if configured_path != "tirith":
        return (expanded if os.path.isfile(expanded) and os.access(expanded, os.X_OK)
                else shutil.which(expanded))
    if external := shutil.which("tirith"):
        return external
    import pm

    selected = pm.installed_package("tirith")
    return str(selected.binary) if selected and selected.binary else None


def _resolve_tirith_path(configured_path: str) -> str:
    """Resolve for a scan; do not wait on a startup download already in flight."""
    if found := _local_tirith(configured_path):
        return found
    if configured_path == "tirith":
        import pm

        if not pm.lazy_installs_allowed() or not _claim_install_attempt():
            return os.path.expanduser(configured_path)
        try:
            pm.ensure("tirith")
            selected = pm.installed_package("tirith")
            if selected and selected.binary:
                return str(selected.binary)
        except Exception as exc:
            _warn_once("tirith_install", "tirith install unavailable: %s", exc)
    return os.path.expanduser(configured_path)


def _background_install(*, log_failures: bool) -> None:
    import pm

    try:
        pm.ensure("tirith")
    except Exception as exc:
        log = logger.warning if log_failures else logger.debug
        log("tirith install failed: %s", exc)


def ensure_installed(*, log_failures: bool = True, explicit: bool = False):
    """Opt-in startup is non-blocking. Explicit setup waits and reports errors.

    Explicit executable configuration remains authoritative, including a miss.
    Lazy refusal never starts a thread; already installed tools remain usable.
    """
    import pm

    cfg = _load_security_config()
    if not cfg["tirith_enabled"]:
        return None
    configured = cfg["tirith_path"]
    if configured != "tirith":
        return _local_tirith(configured)
    if explicit:
        pm.ensure("tirith", explicit=True)
        selected = pm.installed_package("tirith")
        return str(selected.binary) if selected and selected.binary else None
    if found := _local_tirith(configured):
        return found
    if not is_platform_supported() or not pm.lazy_installs_allowed():
        return None
    if _claim_install_attempt():
        context = copy_context()
        thread = threading.Thread(
            target=context.run, args=(_background_install,),
            kwargs={"log_failures": log_failures}, daemon=True,
        )
        _install_threads[hermes_home_key()] = thread
        thread.start()
    return None


def missing_is_expected() -> bool:
    """Whether an unresolved default tirith is by design rather than a fault.

    The first launch after a PM install starts the download in the background,
    and a lazy-install policy refusal is the operator's choice; neither is
    actionable. A missing explicit ``tirith_path`` always is.
    """
    import pm

    configured = _load_security_config()["tirith_path"]
    if configured != "tirith":
        return False
    thread = _install_threads.get(hermes_home_key())
    if thread is not None and thread.is_alive():
        return True
    return _local_tirith(configured) is not None or not pm.lazy_installs_allowed()


# --- Main API ---
_MAX_FINDINGS = 50
_MAX_SUMMARY_LEN = 500
_EXIT_ACTIONS = {0: "allow", 1: "block", 2: "warn"}
# Summary when tirith's JSON is unparseable and only the exit code is known.
_NO_DETAILS_SUMMARY = {
    "block": "security issue detected (details unavailable)",
    "warn": "security warning detected (details unavailable)"}
_VARIATION_SELECTOR_16 = "\ufe0f"
# Code points that carry the Unicode ``Emoji`` property and take VS16 for emoji presentation: the
# Miscellaneous Symbols / Dingbats blocks, the SMP emoji planes, and the BMP singletons outside them
# (©️ ®️ ‼️ ⁉️ ™️ ℹ️ arrows, ⌚ ⌨️ ⏏️ media keys, Ⓜ️ ▪️ ▶️ ◀️ ◻️ ⤴️ ⬅️ ⬛ ⭐ ⭕ 〰️ 〽️ ㊗️ ㊙️).
# Digits, ``#`` and ``*`` also carry the property (keycap bases) but are deliberately absent: VS16
# after a letter or digit is exactly the steganography signal the rule exists for.
_EMOJI_PRESENTATION_BASE_RANGES = (
    (0x00A9, 0x00A9), (0x00AE, 0x00AE), (0x203C, 0x203C), (0x2049, 0x2049), (0x2122, 0x2122),
    (0x2139, 0x2139), (0x2194, 0x2199), (0x21A9, 0x21AA), (0x231A, 0x231B), (0x2328, 0x2328),
    (0x23CF, 0x23CF), (0x23E9, 0x23F3), (0x23F8, 0x23FA), (0x24C2, 0x24C2), (0x25AA, 0x25AB),
    (0x25B6, 0x25B6), (0x25C0, 0x25C0), (0x25FB, 0x25FE), (0x2600, 0x27BF), (0x2934, 0x2935),
    (0x2B05, 0x2B07), (0x2B1B, 0x2B1C), (0x2B50, 0x2B50), (0x2B55, 0x2B55), (0x3030, 0x3030),
    (0x303D, 0x303D), (0x3297, 0x3297), (0x3299, 0x3299), (0x1F000, 0x1FAFF))


def _verdict(action: str, summary: str = "", findings: list | None = None) -> dict:
    return {"action": action, "findings": [] if findings is None else findings, "summary": summary}


def _fail(fail_open: bool, open_summary: str, closed_summary: str) -> dict:
    return _verdict("allow", open_summary) if fail_open else _verdict("block", closed_summary)


def _crash(fail_open: bool, open_summary: str, closed_summary: str) -> dict:
    """An operational failure: count it toward the circuit breaker, then fail open/closed."""
    _record_tirith_crash()
    return _fail(fail_open, open_summary, closed_summary)


def check_command_security(command: str) -> dict:
    """Run the tirith scan on a command -> ``{"action": allow|warn|block, "findings", "summary"}``.
    Exit code determines the action; JSON enriches. Spawn failures/timeouts respect fail_open."""
    global _crash_count, _circuit_open, _circuit_open_at
    cfg = _load_security_config()
    if not cfg["tirith_enabled"]:
        return _verdict("allow")
    # Circuit breaker: if tirith has crashed _CRASH_LIMIT times in a row, stop trying and fail open (issue
    # #41400). After _CIRCUIT_RETRY_S the breaker half-opens: exactly one caller claims the probe slot —
    # claiming re-arms _circuit_open_at under _breaker_lock, so concurrent callers see a fresh TTL and stay
    # fail-open — and falls through to a real scan below.
    if _circuit_open:
        with _breaker_lock:
            if _circuit_open and time.monotonic() - _circuit_open_at < _CIRCUIT_RETRY_S:
                return _verdict("allow", "tirith disabled (circuit breaker)")
            if _circuit_open:  # TTL expired: claim the single-flight probe slot for this window
                _circuit_open_at = time.monotonic()
                logger.info("tirith circuit breaker half-open: probing after %ds", _CIRCUIT_RETRY_S)
    # No binary for this platform, ever: skip the resolver so we never spawn.
    if cfg["tirith_path"] == "tirith" and not is_platform_supported():
        return _verdict("allow")
    tirith_path = _resolve_tirith_path(cfg["tirith_path"])
    timeout, fail_open = cfg["tirith_timeout"], cfg["tirith_fail_open"]
    if tirith_path is None:
        _warn_once("tirith_path_none", "tirith path resolved to None; scanning disabled")
        return _fail(fail_open, "tirith path unavailable", "tirith path unavailable (fail-closed)")
    # First scan (also the witness for the cache-warm rescan below). One spawn, fully
    # accounted: spawn failure/timeout/unknown-exit are classified here (crash + breaker
    # accounting, fail_open respected); _tirith_check stays for the rescan paths below.
    try:
        result = subprocess.run(
            [tirith_path, "check", "--json", "--non-interactive", "--shell", "posix", "--", command],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout,
            stdin=subprocess.DEVNULL)
    except OSError as exc:
        # FileNotFoundError / PermissionError / exec format error: dedupe by (class, errno)
        # so each failure mode surfaces once, not per command.
        _warn_once(f"tirith_spawn_failed:{type(exc).__name__}:{getattr(exc, 'errno', '')}",
                   "tirith spawn failed: %s", exc)
        return _crash(fail_open, f"tirith unavailable: {exc}", f"tirith spawn failed (fail-closed): {exc}")
    except subprocess.TimeoutExpired:
        _warn_once(f"tirith_timeout:{timeout}", "tirith timed out after %ds", timeout)
        return _crash(fail_open, f"tirith timed out ({timeout}s)", "tirith timed out (fail-closed)")
    if (action := _EXIT_ACTIONS.get(result.returncode)) is None:
        # Unknown exit code (includes signal-killed, e.g. -11): respect fail_open.
        logger.warning("tirith returned unexpected exit code %d", result.returncode)
        return _crash(fail_open, f"tirith exit code {result.returncode} (fail-open)",
                      f"tirith exit code {result.returncode} (fail-closed)")
    # Any completed scan (allow/block/warn) proves the binary is healthy: clear the streak and close the
    # breaker. This is the half-open probe's recovery path, and it also fixes the streak never resetting on
    # block/warn verdicts.
    _crash_count = 0
    if _circuit_open:
        _circuit_open, _circuit_open_at = False, 0.0
        logger.info("tirith circuit breaker closed after successful scan")
    # JSON enriches findings/summary; a parse failure never changes the verdict.
    findings, summary = [], ""
    try:
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        findings = data.get("findings", [])[:_MAX_FINDINGS]
        summary = (data.get("summary", "") or "")[:_MAX_SUMMARY_LEN]
    except (json.JSONDecodeError, AttributeError):
        logger.debug("tirith JSON parse failed, using exit code only")
        summary = _NO_DETAILS_SUMMARY.get(action, "")
    # .app is a legitimate gTLD: a warn consisting solely of lookalike_tld findings for .app is a
    # known false positive and is downgraded to allow. Any other finding keeps the warn.
    if action == "warn" and findings and all(_is_app_tld_finding(f) for f in findings):
        return _verdict("allow")
    # VS16 follows ordinary emoji-capable code points in standard emoji-presentation sequences.
    # Preserve warnings for every other selector, including VS16 after text, because those can
    # carry the steganographic payload that Tirith is intended to detect.
    if action == "warn" and findings and all(_is_emoji_variation_selector_finding(f) for f in findings) \
            and _has_only_emoji_presentation_selectors(command):
        return _verdict("allow")
    # Redirection tokens and package-manager flag operands ("2>&1", the value of
    # --index-strategy) that tirith mistook for package names produce analysis_incomplete
    # warns on ordinary commands the same way: the names 404 on every registry. Drop only
    # findings grounded in tokens of the scanned command (warn-only; a real package or any
    # other rule keeps the verdict).
    if action == "warn" and findings:
        action, findings = _suppress_phantom_package_findings(command, action, findings)
        if action == "allow":
            return _verdict("allow")
    # tirith 0.4.2 hard-blocks while/until bracket-test compounds with two
    # analysis_incomplete HIGH findings even when every leaf command is
    # read-only -- a false positive that kills the command outright in
    # single-query mode. `[ args ]` is exactly `test args` (POSIX), so rewrite
    # the bracket spans, and downgrade ONLY when the ORIGINAL command's leaf
    # set is provably read-only AND the rewritten copy re-scans as a clean
    # allow. The leaf gate is what keeps destructive watcher loops blocked:
    # tirith 0.4.2 cannot see rm/sudo inside while bodies even after the
    # rewrite, so a bare rescan-clean gate would fail open. Everything that
    # does not pass both gates keeps the original block (fail-closed).
    if action == "block" and _is_loop_analysis_fp_block(findings):
        leaves = _extract_leaf_commands(command)
        rewritten = _rewrite_bracket_tests(command)
        if (rewritten is not None and rewritten != command
                and _all_leaves_readonly(leaves)):
            rescan = _tirith_check(tirith_path, timeout, rewritten)
            if rescan is not None:
                r_action, r_findings, r_summary = rescan
                if r_action == "allow":
                    _crash_count = 0
                    return _verdict("allow", "bracket-test loop downgraded after "
                                             "read-only-leaf rescan")
    # tirith <= 0.4.2 runs every package's threat-intel lookups under one small per-run wall-clock
    # budget, so `npm install a b` warns "deadline exhausted" for all packages even when upstreams
    # are healthy — the budget is spent before later packages finish their first lookup. Successful
    # responses are cached on disk (failures are not), so solo per-package scans (one package per
    # run = one budget each) warm the cache and a single re-scan then completes. Warnings for
    # genuinely unreachable upstreams survive the re-scan and stand, so this never fails open.
    if action == "warn" and (real := _incomplete_real_packages(findings, command)):
        if (rescan := _rescan_after_cache_warm(command, tirith_path, timeout, real)) is not None:
            rescan_action, rescan_findings, rescan_summary = rescan
            if rescan_action == "warn" and rescan_findings:
                rescan_action, rescan_findings = _suppress_app_tld_false_positives(
                    rescan_action, rescan_findings)
                rescan_action, rescan_findings = _suppress_phantom_package_findings(
                    command, rescan_action, rescan_findings)
            if rescan_action == "allow":
                _crash_count = 0
                return _verdict("allow")
            return _verdict(rescan_action, rescan_summary, rescan_findings)
    return _verdict(action, summary, findings)


_INCOMPLETE_PKG = re.compile(r"threat-intelligence check for package '([^']*)'")
_REDIRECT_OP = re.compile(r"""
    (?P<fd>\d*|\{[A-Za-z_][A-Za-z0-9_]*\})   # optional fd number or {varname}
    (?:>>|>&|<&|<>|>\||>|<)                  # the redirection operator itself
    """, re.VERBOSE)
# Warm commands are constructed from names extracted out of tirith findings before they are ever
# spawned, and tirith echoes package names from the install command it scanned — so this charset
# (npm/pypi name rules, '=' for version specs) bounds what can reach the shell. Anything else is
# not a package name.
_PKGNAME_OK = re.compile(r"^[@a-zA-Z0-9][@/=.:_a-zA-Z0-9-]*$")
_WARM_SCAN_LIMIT = 12  # bound worst-case added latency (~1s per cold solo scan)
_WARM_CMDS = {"npm": "npm install {pkg}", "pnpm": "pnpm install {pkg}",
              "yarn": "yarn add {pkg}", "pip": "pip install {pkg}"}


_LONG_VALUE_FLAGS = frozenset({
    # pip / uv
    "--index-strategy", "--index-url", "--extra-index-url", "--find-links", "--index",
    "--default-index", "--extra-index", "--keyring-provider", "--config-setting",
    "--config-settings", "--constraint", "--requirement", "--editable", "--target", "--prefix",
    "--platform", "--python-version", "--implementation", "--abi", "--python", "--only-binary",
    "--no-binary", "--prefer-binary", "--trusted-host", "--timeout", "--retries",
    "--resume-retries", "--build", "--cache-dir", "--build-constraint", "--build-constraints",
    "--config-file", "--exclude-newer", "--resolution", "--annotation-style", "--fork-strategy",
    "--link-mode", "--no-build-isolation-package", "--strategy",
    # npm / yarn / cargo / gem
    "--registry", "--destination", "--source", "--tag", "--cwd", "--output", "--format",
})
_SHORT_VALUE_FLAGS = frozenset({"-c", "-e", "-f", "-i", "-r", "-t", "-b", "-s"})


def _redirect_artifact_tokens(command: str) -> set[str]:
    """Tokens in *command* that exist only because of a shell redirection: the operator tokens
    themselves (``2>&1``, ``>``, ``2>/tmp/e``), each redirection *target* (``out.log``), and the
    numeric fd prefixes a naive splitter leaves behind. These are exactly the strings tirith
    mistakes for packages."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:  # unbalanced quotes -- fall back to whitespace splitting
        tokens = command.split()
    artifacts: set[str] = set()
    expect_target = False
    for tok in tokens:
        if expect_target:
            artifacts.add(tok)
            expect_target = False
        if not (m := _REDIRECT_OP.search(tok)):
            continue
        artifacts.add(tok)
        # "2>&1" also yields the bare fd number when a parser splits on the operator.
        if (prefix := m.group("fd")) and prefix.isdigit():
            artifacts.add(prefix)
        # A bare operator ("> out.log") takes its target from the next token.
        expect_target = tok.endswith(m.group(0)) and not tok[m.end():]
    return artifacts


def _flag_operand_tokens(command: str) -> set[str]:
    """The flag token and operand of value-taking package-manager flags in *command*
    (e.g. ``--index-strategy unsafe-best-match`` -> both tokens; ``--flag=value`` -> both).
    The VALUE of a flag is not a package, but tirith enriches it as one."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:  # unbalanced quotes -- fall back to whitespace splitting
        tokens = command.split()
    artifacts: set[str] = set()
    expect_value = False
    for tok in tokens:
        if expect_value:
            artifacts.add(tok)
            expect_value = False
            continue
        if tok in _SHORT_VALUE_FLAGS:
            artifacts.add(tok)
            expect_value = True
        elif tok.startswith("--"):
            if "=" in tok:
                artifacts.add(tok)
                artifacts.add(tok.split("=", 1)[1])
            elif tok in _LONG_VALUE_FLAGS:
                artifacts.add(tok)
                expect_value = True
    return artifacts


def _incomplete_real_packages(findings: list, command: str) -> list[str]:
    """Real package names (not command-text artifacts) named by analysis_incomplete findings,
    filtered to a conservative package-name charset before any warm command is built."""
    artifacts = _redirect_artifact_tokens(command) | _flag_operand_tokens(command)
    pkgs: list[str] = []
    for f in findings or []:
        if not isinstance(f, dict) or f.get("rule_id") != "analysis_incomplete":
            continue
        for name in _INCOMPLETE_PKG.findall(str(f.get("description") or "")):
            if (name and name not in artifacts and _PKGNAME_OK.match(name)
                    and name not in pkgs):
                pkgs.append(name)
    return pkgs


def _is_phantom_package_finding(finding: dict, artifacts: set[str]) -> bool:
    """True if *finding* is an incomplete-lookup warning whose named package(s) are all
    command-text artifacts (redirection tokens, flag operands), not real packages."""
    if not isinstance(finding, dict) or finding.get("rule_id") != "analysis_incomplete":
        return False
    names = _INCOMPLETE_PKG.findall(str(finding.get("description") or ""))
    return bool(names) and all(n in artifacts for n in names)


def _suppress_phantom_package_findings(command: str, action: str, findings: list) -> tuple[str, list]:
    """Warn-only phantom-package suppression (t_2550b91f, re-land of the dc9df97cc6 lineage):
    drop analysis_incomplete findings naming a redirection token or package-manager flag
    operand from the scanned command ("2>&1", the value of --index-strategy) — names that
    404 on every registry. Warn-only: a block action is never downgraded, and a real
    package or any other rule keeps the verdict. Returns ``(action, findings)``; ``allow``
    with an empty list when nothing but phantoms remains."""
    if action != "warn" or not findings:
        return action, findings
    artifacts = _redirect_artifact_tokens(command) | _flag_operand_tokens(command)
    kept = [f for f in findings if not _is_phantom_package_finding(f, artifacts)]
    if kept == findings:
        return action, findings
    if not kept:
        return "allow", []
    return action, kept


def _warm_command(pm: str, pkg: str) -> str:
    """Solo-scan command that warms the cache for ``pkg`` on the SAME registry the original
    command targets: npm/pnpm/yarn installs scan npm packages, everything else (pip etc.)
    scans PyPI. Mirroring the manager keeps the warmed cache entries on the right registry."""
    return _WARM_CMDS[pm].format(pkg=pkg)


def _tirith_check(tirith_path: str, timeout: int, command: str) -> tuple[str, list, str] | None:
    """One tirith check -> ``(action, findings, summary)``, or None on operational trouble
    (spawn failure, timeout, unknown exit). The CALLER owns the verdict for operational trouble
    (fail_open + crash accounting in check_command_security); any COMPLETED scan (allow/block/warn)
    proves the binary is healthy, so the crash streak resets and an open breaker closes here --
    the half-open probe's recovery path (#41400)."""
    global _crash_count, _circuit_open, _circuit_open_at
    try:
        result = subprocess.run(
            [tirith_path, "check", "--json", "--non-interactive", "--shell", "posix", "--", command],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout,
            stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if (action := _EXIT_ACTIONS.get(result.returncode)) is None:
        return None
    # Any completed scan (allow/block/warn) proves the binary is healthy: clear the streak and close the
    # breaker. This is the half-open probe's recovery path, and it also fixes the streak never resetting on
    # block/warn verdicts.
    _crash_count = 0
    if _circuit_open:
        _circuit_open, _circuit_open_at = False, 0.0
        logger.info("tirith circuit breaker closed after successful scan")
    # JSON enriches findings/summary; a parse failure never changes the verdict.
    findings, summary = [], ""
    try:
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        findings = data.get("findings", [])[:_MAX_FINDINGS]
        summary = (data.get("summary", "") or "")[:_MAX_SUMMARY_LEN]
    except (json.JSONDecodeError, AttributeError):
        logger.debug("tirith JSON parse failed, using exit code only")
        summary = _NO_DETAILS_SUMMARY.get(action, "")
    return action, findings, summary


def _rescan_after_cache_warm(command: str, tirith_path: str, timeout: int,
                             packages: list[str]) -> tuple[str, list, str] | None:
    """Warm tirith's persistent response cache with one solo scan per package, then re-scan the
    full command once. Returns the re-scan verdict, or None (keep the original verdict) when
    warming could not run: no binary path, operational failure, or an empty package list.
    The solo-scan command mirrors the original package manager so the cache is warmed on the
    registry the original command actually targets."""
    if not tirith_path or not packages:
        return None
    # Manager detection from the ORIGINAL command text (what tirith scanned). If the manager
    # can't be determined, the original warn verdict stands (fail-closed default).
    head = command.strip().split()
    if head and head[0] in ("npm", "pnpm", "yarn", "pip"):
        pm = head[0]
    elif head and head[0] == "uv" and len(head) > 1 and head[1] == "pip":
        pm = "pip"
    else:
        return None
    for pkg in packages[:_WARM_SCAN_LIMIT]:
        if _tirith_check(tirith_path, timeout, _warm_command(pm, pkg)) is None:
            return None  # binary trouble mid-warm: keep the original verdict untouched
    return _tirith_check(tirith_path, timeout, command)


def _suppress_app_tld_false_positives(action: str, findings: list) -> tuple[str, list]:
    """Drop .app lookalike_tld findings from a warn; everything dropped -> allow, a partial
    drop keeps the remaining real findings and the warn stands. Warn-only: a block action is
    never downgraded."""
    if action != "warn" or not findings:
        return action, findings
    kept = [f for f in findings if not _is_app_tld_finding(f)]
    if kept == findings:
        return action, findings
    if not kept:
        return "allow", []
    return action, kept


def _is_app_tld_finding(finding: dict) -> bool:
    """True if this finding is a lookalike_tld warning for the .app TLD only."""
    if not isinstance(finding, dict) or finding.get("rule_id") != "lookalike_tld":
        return False
    return any(
        val is not None and ".app" in str(val).lower()
        for val in (finding.get(k) for k in ("value", "tld", "detail", "description", "message")))


# ---------------------------------------------------------------------------
# analysis_incomplete nested-loop false-positive suppressor (t_0fb18e49)
#
# tirith 0.4.2 hard-BLOCKS `while [ ... ]`/`until [ ... ]` compounds (rule
# analysis_incomplete, titles "Nested executable body could not be resolved" +
# "nested command analysis was incomplete") even when every leaf command is
# read-only -- and in single-query mode there is no user to approve, so the
# command just dies. POSIX defines `[ args ]` as exactly `test args`, so the
# wrapper rewrites word-boundary bracket spans to `test`, requires every leaf
# command of the ORIGINAL text to be provably read-only, and re-scans the
# rewritten copy; the block is downgraded ONLY on a clean allow. The read-only
# leaf gate is NOT optional: tirith 0.4.2 cannot see destructive bodies inside
# while-loops (verified live: `while test ! -f x; do sudo rm -rf /opt/x; done`
# scans ALLOW), so a bare rescan-clean gate would un-block destructive watcher
# loops.
# ---------------------------------------------------------------------------

_FP_LOOP_BLOCK_TITLE = "Nested executable body could not be resolved"
_FP_LOOP_GAP_TITLE = "nested command analysis was incomplete"

# Word-boundary `[`/`[[` that starts a test invocation (never a glob char
# class like /tmp/[abc]*.log, which is preceded by / or a word char).
_FP_BRACKET_TEST_SPAN = re.compile(r"(?<![\w/])\[{1,2}(?=\s)")

# Strict read-only leaf allowlist for the downgrade gate. Deliberately narrow:
# these commands' observable effects are on stdout/stderr only. `find` is
# excluded on purpose (-delete / -exec rm); xargs/sed/awk/sh never listed.
_FP_READONLY_LEAVES = frozenset({
    "ls", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "ack",
    "stat", "file", "tree", "du", "df", "readlink", "realpath", "basename",
    "dirname", "hostname", "whoami", "id", "uname", "arch", "date", "pwd",
    "tty", "true", "false", "test", "[", "[[", "sleep", "seq", "printf",
    "echo", "env", "printenv",
})

# Shell reserved words that only structure a compound command; stripped from
# segment starts before the leaf head is read.
_FP_LOOP_KEYWORDS = frozenset({
    "while", "until", "for", "if", "then", "do", "else", "elif", "fi",
    "done", "case", "esac", "!", "time",
})

# Assignments that redirect executable/library/startup resolution or shell
# parsing when set on a command -> the leaf is not provably read-only.
_FP_DANGEROUS_ASSIGN = frozenset({
    "PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "IFS", "ENV",
    "BASH_ENV", "HOME", "SHELL", "CDPATH", "GLOBIGNORE", "PYTHONPATH",
    "PYTHONHOME",
})

_FP_ASSIGN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=")


def _is_loop_analysis_fp_block(findings: list) -> bool:
    """True iff findings are EXACTLY the two analysis_incomplete HIGH titles
    tirith 0.4.2 emits for while/until bracket-test loops (the t_0fb18e49
    false-positive pair). Any other finding keeps the fail-closed block."""
    if not isinstance(findings, list) or len(findings) != 2:
        return False
    titles = set()
    for f in findings:
        if not isinstance(f, dict) or f.get("rule_id") != "analysis_incomplete" \
                or str(f.get("severity", "")).lower() != "high":
            return False
        titles.add(str(f.get("title", "")))
    return titles == {_FP_LOOP_BLOCK_TITLE, _FP_LOOP_GAP_TITLE}


def _fp_quoted_spans(command: str) -> list[tuple[int, int]]:
    """Character-index ranges of single/double-quoted regions (best-effort:
    backslash escapes honored outside quotes). Bracket spans inside quotes are
    skipped -- rewriting there would change the quoted text."""
    spans, start, quote = [], None, None
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if quote is None:
            if c == "\\":
                i += 2
                continue
            if c in ("'", '"'):
                quote, start = c, i
        elif c == quote:
            spans.append((start, i))
            quote = None
        i += 1
    if quote is not None:
        spans.append((start, n))  # unterminated quote: rest counts as quoted
    return spans


def _rewrite_bracket_tests(command: str) -> str | None:
    """Rewrite word-boundary bracket-test spans ``[ ... ]`` / ``[[ ... ]]`` to
    ``test ...`` (POSIX-identical builtin). Returns the rewritten text, the
    unchanged text when there is nothing to rewrite, or None when rewriting is
    not provably equivalent: any ``$( `` ``${ `` ``$[ `` or backtick anywhere,
    a quote/escape inside a span, an empty test body, an unterminated span, or
    a ``[[`` span without its matching ``]]``."""
    if any(t in command for t in ("$(", "${", "$[", "`")):
        return None
    quoted = _fp_quoted_spans(command)
    out: list[str] = []
    consumed, rewritten = 0, False
    for m in _FP_BRACKET_TEST_SPAN.finditer(command):
        start = m.start()
        if start < consumed or any(a <= start <= b for a, b in quoted):
            continue
        close = command.find("]", start + len(m.group(0)))
        if close == -1:
            return None
        if command.startswith("[[", start):
            if command[close + 1:close + 2] != "]":
                return None  # [[ without its ]] closer: do not guess
        elif command[close + 1:close + 2] == "]":
            return None  # single-[ span ending in ]]: unmodeled nesting
        inner = command[start + len(m.group(0)):close]
        if any(c in inner for c in "'\"\\\n"):
            return None  # quotes/escapes inside the span: no quote parsing
        words = inner.split()
        if not words:
            return None  # empty test: fail closed
        if any(not re.fullmatch(r"[A-Za-z0-9_@%+=:,./!-]+", w) for w in words):
            return None  # non-plain word inside the span (`!` = test negation)
        out.append(command[consumed:start])
        out.append("test" + inner.rstrip())
        consumed = close + (2 if command.startswith("[[", start) else 1)
        rewritten = True
    if not rewritten:
        return command
    out.append(command[consumed:])
    return "".join(out)


def _extract_leaf_commands(command: str) -> list[str] | None:
    """Leaf command heads of *command*, one per segment split on ``;`` ``|``
    ``&`` ``&&`` and newlines. Returns None (fail-closed: unknown leaf set)
    when any segment carries command substitution or quotes (``$``, backtick,
    quote, backslash), grouping constructs, an input redirection or heredoc
    (any ``<``), a non-/dev/null output redirection, a dangerous assignment
    (``PATH=`` etc.), or an assignment/env wrapper with no command. Compound
    scaffolding (while/do/done/...) is stripped from segment starts; bare
    scaffolding segments yield no leaf. Special write flags of otherwise
    read-only commands (date -s) also disqualify."""
    if any(c in command for c in "$`'\"\\(){}<"):
        return None
    leaves: list[str] = []
    for seg in re.split(r"[;|\n]+", command):
        seg = re.sub(r"\d*(?:&>|>>|>|>&|<|<>|>&\d|>&-|<&|<&\d|<&-)\s*/dev/null\b", " ", seg)
        seg = re.sub(r"\d*>\s*&\s*\d+\b", " ", seg)  # fd dups: 2>&1, >&2
        seg = re.sub(r"\d*<>\s*", " ", seg)  # open-for-read-write fd: no file touched
        seg = seg.replace("/dev/null", " ")
        if re.search(r"[>|&]", seg):
            return None  # unmodeled redirect/pipe/control char -> unknown
        tokens = seg.split()
        while tokens and tokens[0] in _FP_LOOP_KEYWORDS:
            tokens = tokens[1:]
        if not tokens:
            continue  # bare scaffolding (done / fi / then ...)
        while tokens and (m := _FP_ASSIGN.match(tokens[0])):
            if m.group(1) in _FP_DANGEROUS_ASSIGN:
                return None
            tokens = tokens[1:]
        while tokens and tokens[0] in ("env", "nice"):
            tokens = tokens[1:]
            while tokens and (m := _FP_ASSIGN.match(tokens[0])):
                if m.group(1) in _FP_DANGEROUS_ASSIGN:
                    return None
                tokens = tokens[1:]
        if not tokens:
            return None  # assignment or env wrapper with no command
        head, args = tokens[0], tokens[1:]
        if head == "date" and any(a in ("-s", "--set") for a in args):
            return None  # date writes the system clock with -s
        leaves.append(head)
    return leaves


def _all_leaves_readonly(leaves: list[str] | None) -> bool:
    """Every leaf head is in the strict read-only allowlist. Extraction already
    rejected write channels (non-/dev/null redirects, heredocs, dangerous
    assignments, date -s); an empty/None leaf set is fail-closed."""
    if not leaves:
        return False
    return all(head in _FP_READONLY_LEAVES for head in leaves)


def _is_emoji_variation_selector_finding(finding: dict) -> bool:
    """True only for the Tirith rule that reports variation selectors."""
    return isinstance(finding, dict) and finding.get("rule_id") == "variation_selector"


def _has_only_emoji_presentation_selectors(command: str) -> bool:
    """Whether every variation selector is VS16 immediately after an emoji-capable base."""
    selectors = ("\ufe00", "\U000e0100")
    saw_selector = False
    for idx, char in enumerate(command):
        if not selectors[0] <= char <= "\ufe0f" and not selectors[1] <= char <= "\U000e01ef":
            continue
        saw_selector = True
        if char != _VARIATION_SELECTOR_16 or idx == 0:
            return False
        base = ord(command[idx - 1])
        if not any(start <= base <= end for start, end in _EMOJI_PRESENTATION_BASE_RANGES):
            return False
    return saw_selector
