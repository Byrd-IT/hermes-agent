"""Pre-execution guards for the terminal tool.

Pure functions that decide whether a command may run at all: workdir
validation, the foreground long-lived/background-operator guidance, the
supervised-gateway lifecycle block, and the Windows self-repo git guard.
Each ``*_block`` helper returns a finished JSON error string, or None when
the command may proceed. Split out of tools/terminal_tool.py; the origin
module re-imports every public helper so ``tools.terminal_tool.<name>``
keeps resolving.
"""

import json
import logging
import re
import shlex
import stat
from pathlib import Path
from typing import Any, Optional

from tools.shell_heredoc import strip_inert_heredoc_bodies

logger = logging.getLogger("tools.terminal_tool")


# Workdir allowlist: Unicode alnum plus path/drive/UNC separators and common
# punctuation; shell metacharacters stay rejected. Unicode is allowed on
# purpose (e.g. CJK vault paths). Defense-in-depth — the cwd is also
# shlex-quoted before reaching the shell.
_WORKDIR_SAFE_ASCII_CHARS = frozenset('/\\:_-.~ +@=,')


def _is_safe_workdir_char(ch: str) -> bool:
    if not ch or ord(ch) < 32 or ord(ch) == 127:  # control chars / NUL
        return False
    return ch.isalnum() or ch in _WORKDIR_SAFE_ASCII_CHARS


def _validate_workdir(workdir: str) -> str | None:
    """Error message if *workdir* has a disallowed character, else None.
    Allowlist rather than deny-list so novel metacharacters can't slip through."""
    for ch in workdir or "":
        if not _is_safe_workdir_char(ch):
            return (
                f"Blocked: workdir contains disallowed character {repr(ch)}. "
                "Use a simple filesystem path without shell metacharacters."
            )
    return None


def _safe_command_preview(command: Any, limit: int = 200) -> str:
    """Return a log-safe preview for possibly-invalid command values."""
    if command is None:
        return "<None>"
    if isinstance(command, str):
        return command[:limit]
    try:
        return repr(command)[:limit]
    except Exception:
        return f"<{type(command).__name__}>"


def _blocked_json(error: str, status: str) -> str:
    """The guard result envelope: exit_code 1 + *error* + *status*."""
    return json.dumps({"output": "", "exit_code": 1, "error": error, "status": status}, ensure_ascii=False)


_SHELL_LEVEL_BACKGROUND_RE = re.compile(
    r"(?:^|[;&|]\s*|&&\s*|\|\|\s*|\$\(\s*)(?:nohup|disown|setsid)\b", re.IGNORECASE | re.MULTILINE
)
_INLINE_BACKGROUND_AMP_RE = re.compile(r"\s&\s")
_TRAILING_BACKGROUND_AMP_RE = re.compile(r"\s&\s*(?:#.*)?$")


def _strip_quotes(command: str) -> str:
    """Blank quoted / backtick content and provably-inert heredoc bodies so
    regex checks can't match keywords (nohup, setsid, '&') inside strings.

    Heredocs are masked FIRST: their delimiter may itself be quoted
    (``<<'EOF'``). ``strip_inert_heredoc_bodies`` is conservative — only a
    quoted, terminated delimiter on a simple opener fed to a known non-shell
    consumer is masked, so a real background operator can't hide in one.
    """
    result = strip_inert_heredoc_bodies(command)
    result = re.sub(r"'[^']*'", "''", result)
    result = re.sub(r'"(?:[^"\\]|\\.)*"', '""', result)
    return re.sub(r"`[^`]*`", "``", result)


# End of one simple command: ; | || && & newline or a subshell/group
# paren, but not the & of a redirect (2>&1, &>file), which belongs to the
# same command.
_SIMPLE_COMMAND_END_RE = re.compile(r"[;|\n()]|(?<![<>])&(?!>)")
# `up` flags that take no value, so they may be clustered (-dV).
_COMPOSE_UP_BOOL_SHORT_FLAGS = "dVwy"


def _compose_up_is_detached(args: list[str]) -> bool:
    """True when `compose up` *args* return once the containers start.

    ``-d``/``--detach`` detach, and ``--wait`` implies detached mode
    (docs.docker.com/reference/cli/docker/compose/up). ``-w``/``--watch``
    keeps the command running to sync files, so it stays long-lived.
    """
    detached = False
    for arg in args:
        if arg in ("--watch", "-w") or arg.startswith("--watch="):
            return False
        if arg in ("--detach", "--wait") or arg.lower() in ("--detach=true", "--wait=true"):
            detached = True
        elif re.fullmatch(f"-[{_COMPOSE_UP_BOOL_SHORT_FLAGS}]+", arg):
            if "w" in arg:
                return False
            detached = detached or "d" in arg
    return detached


# Compose global options that take a separate value (`-f a.yml up`).
_COMPOSE_GLOBAL_VALUE_OPTS = frozenset((
    "-f", "--file", "-p", "--project-name", "--project-directory", "--env-file",
    "--profile", "--ansi", "--progress", "--parallel",
))


def _compose_starts_attached_up(args: list[str]) -> bool:
    """True when compose *args* (after `docker compose`/`docker-compose`)
    run an attached `up`; global options before the subcommand are skipped."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _COMPOSE_GLOBAL_VALUE_OPTS else 1
    return i < len(args) and args[i] == "up" and not _compose_up_is_detached(args[i + 1:])


# The long-lived check looks only at the COMMAND POSITION of each simple
# command, so `grep uvicorn ...`, `ls .../uvicorn/...` or `ps | grep serve`
# (read-only, argument-only mentions) are never mistaken for a server start.
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_REDIRECT_RE = re.compile(r"\d*(?:>>?|<<?<?|>&|<&|&>>?)")
# Reserved words / prefixes that precede the real command word.
_COMMAND_PREFIX_WORDS = frozenset((
    "!", "{", "}", "if", "then", "elif", "else", "while", "until", "do",
    "time", "exec", "command", "builtin", "caffeinate",
))
# Wrappers that run their argv tail as the command; value is the set of
# short/long options that consume a following value.
_COMMAND_WRAPPERS = {
    "sudo": frozenset(("-u", "-g", "-h", "-p", "-C", "-U", "-r", "-t", "-D", "-R", "-T",
                       "--user", "--group", "--host", "--prompt", "--chdir", "--role", "--type")),
    "doas": frozenset(("-u", "-C")),
    "env": frozenset(("-u", "-C", "-S", "--unset", "--chdir", "--split-string")),
    "nice": frozenset(("-n", "--adjustment")),
    "ionice": frozenset(("-c", "-n", "-p", "--class", "--classdata")),
    "stdbuf": frozenset(("-i", "-o", "-e")),
    "timeout": frozenset(("-s", "-k", "--signal", "--kill-after")),
}
# `<launcher> <sub> <cmd...>` style runners that exec a named program.
_RUNNER_SUBCOMMANDS = {
    "poetry": frozenset(("run",)), "uv": frozenset(("run",)), "pipenv": frozenset(("run",)),
    "pdm": frozenset(("run",)), "hatch": frozenset(("run",)), "rye": frozenset(("run",)),
    "npm": frozenset(("exec", "x")), "pnpm": frozenset(("exec", "dlx")),
    "yarn": frozenset(("exec", "dlx")), "bun": frozenset(("x", "exec")),
}
_NPX_LIKE = frozenset(("npx", "bunx", "pnpx"))
_SCRIPT_RUNNERS = frozenset(("npm", "pnpm", "yarn", "bun"))
_LONG_LIVED_SCRIPT_RE = re.compile(r"(?:dev|start|serve|watch)\b", re.IGNORECASE)
_ALWAYS_LONG_LIVED_BINARIES = frozenset(("nodemon", "uvicorn", "gunicorn", "hypercorn", "daphne"))
_LONG_LIVED_PYTHON_MODULES = frozenset(("http.server", "uvicorn", "gunicorn", "hypercorn", "daphne"))
_PYTHON_RE = re.compile(r"python(?:\d+(?:\.\d+)?)?")
_VITE_BOUNDED_SUBCOMMANDS = frozenset(("build", "optimize"))


def _strip_command_prefix(words: list[str]) -> tuple[list[str], bool]:
    """Drop assignments, redirections, reserved words and wrapper commands
    (with their options) until the real command word is first.

    Also returns whether a ``timeout`` wrapper bounds the command: a
    foreground ``timeout 30 tail -f log`` cannot hang, so it is never
    long-lived."""
    i = 0
    bounded = False
    while i < len(words):
        word = words[i]
        if word in _COMMAND_PREFIX_WORDS or _ASSIGNMENT_RE.match(word):
            i += 1
            continue
        redirect = _REDIRECT_RE.match(word)
        if redirect:
            # `>file` carries its target; a bare `>` takes the next word.
            i += 1 if redirect.end() < len(word) else 2
            continue
        name = word.rsplit("/", 1)[-1]
        if name in _COMMAND_WRAPPERS:
            value_opts = _COMMAND_WRAPPERS[name]
            i += 1
            while i < len(words) and (words[i].startswith("-") or (name == "env" and _ASSIGNMENT_RE.match(words[i]))):
                i += 2 if words[i] in value_opts else 1
            if name == "timeout" and i < len(words):
                i += 1  # the DURATION operand
                bounded = True
            continue
        break
    return words[i:], bounded


def _argv_is_long_lived(words: list[str], depth: int = 0) -> bool:
    """True when the command word of *words* starts a server/watch process."""
    words, bounded = _strip_command_prefix(words)
    if not words or bounded or depth > 4:
        return False
    name = words[0].rsplit("/", 1)[-1].lower()
    args = words[1:]
    if name in _ALWAYS_LONG_LIVED_BINARIES:
        return True
    if name == "vite":
        return not (args and args[0].lower() in _VITE_BOUNDED_SUBCOMMANDS)
    if name == "next":
        return bool(args) and args[0].lower() == "dev"
    if name == "tail":
        return any(a in ("-f", "-F", "--follow") or a.startswith("--follow=")
                   or re.fullmatch(r"-[A-Za-z0-9]*[fF][A-Za-z0-9]*", a) is not None
                   for a in args)
    if name == "docker-compose":
        return _compose_starts_attached_up(args)
    if name == "docker":
        return bool(args) and args[0] == "compose" and _compose_starts_attached_up(args[1:])
    if _PYTHON_RE.fullmatch(name):
        for idx, arg in enumerate(args):
            if arg == "-m":
                return idx + 1 < len(args) and args[idx + 1] in _LONG_LIVED_PYTHON_MODULES
            if not arg.startswith("-"):
                return False  # a script path: `python server.py` is not judged here
        return False
    if name in _NPX_LIKE:
        tail = list(args)
        while tail and tail[0].startswith("-"):
            tail.pop(0)
        return _argv_is_long_lived(tail, depth + 1)
    if name in _RUNNER_SUBCOMMANDS and args and args[0] in _RUNNER_SUBCOMMANDS[name]:
        tail = args[1:]
        while tail and tail[0].startswith("-"):
            tail = tail[1:]
        return _argv_is_long_lived(tail, depth + 1)
    if name in _SCRIPT_RUNNERS and args:
        script = args[1] if args[0] == "run" and len(args) > 1 else args[0]
        if _LONG_LIVED_SCRIPT_RE.match(script):
            return True
        # `yarn vite`, `pnpm nodemon`, `bun uvicorn`: a bin run directly.
        return name != "npm" and _argv_is_long_lived(args, depth + 1)
    return False


def _starts_long_lived_process(unquoted: str) -> bool:
    """True when any simple command in *unquoted* starts a server/watch process.

    Only the command word of each ``;``/``|``/``&&``/``||``/``&``/newline/
    subshell segment is judged; a keyword in an argument (grep pattern, path,
    ps filter) never counts.
    """
    return any(_argv_is_long_lived(segment.split())
               for segment in _SIMPLE_COMMAND_END_RE.split(unquoted))


_LONG_LIVED_FOREGROUND_PATTERNS = (_starts_long_lived_process,)

# Ordered (predicate on the unquoted command, guidance) — first hit wins.
_FOREGROUND_GUIDANCE = (
    (
        _SHELL_LEVEL_BACKGROUND_RE.search,
        "Foreground command uses shell-level background wrappers (nohup/disown/setsid). "
        "Re-send WITHOUT the wrapper as terminal(command=\"<cmd>\", background=true, "
        "notify_on_complete=true) so Hermes tracks the process, then run readiness "
        "checks and tests in separate commands.",
    ),
    (
        lambda s: _INLINE_BACKGROUND_AMP_RE.search(s) or _TRAILING_BACKGROUND_AMP_RE.search(s),
        "Foreground command uses '&' backgrounding. Re-send WITHOUT the '&' as "
        "terminal(command=\"<cmd>\", background=true) — add notify_on_complete=true "
        "for bounded jobs — then run health checks and tests in follow-up terminal calls.",
    ),
    (
        lambda s: any(hit(s) for hit in _LONG_LIVED_FOREGROUND_PATTERNS),
        "This foreground command appears to start a long-lived server/watch process. "
        "Run it with background=true, verify readiness (health endpoint/log signal), "
        "then execute tests in a separate command.",
    ),
)


def _looks_like_help_or_version_command(command: str) -> bool:
    """Return True for informational invocations that should never be blocked."""
    normalized = " ".join(command.lower().split())
    return (
        " --help" in normalized
        or normalized.endswith(" -h")
        or " --version" in normalized
        or normalized.endswith(" -v")
    )


def _foreground_background_guidance(command: str) -> str | None:
    """Guidance text when a foreground command looks long-lived or uses shell
    backgrounding (it should be a managed background session), else None."""
    if _looks_like_help_or_version_command(command):
        return None
    unquoted = _strip_quotes(command)
    return next((msg for hit, msg in _FOREGROUND_GUIDANCE if hit(unquoted)), None)


def _read_script_for_guard(env: Any, guard_cwd: str, script_path: str, max_bytes: int) -> Optional[str]:
    """Best-effort script read: host filesystem first, then a bounded
    ``env.execute('head -c ... < path')`` for remote backends. Binary content
    (NUL byte) is not a script: feeding it to the guard tokenizes machine code
    into bogus paths and crashes the scanner, so it yields None."""
    if env is None:
        return None
    try:
        local_path = Path(script_path).expanduser()
        if not local_path.is_absolute():
            local_path = Path(guard_cwd) / local_path
        if local_path.is_file():
            metadata = local_path.stat()
            if stat.S_ISREG(metadata.st_mode) and metadata.st_size <= max_bytes:
                data = local_path.read_bytes()
                if len(data) <= max_bytes:
                    return None if b"\x00" in data else data.decode("utf-8", errors="replace")
    except Exception:
        pass
    # Remote backend: bound the read at the source with `head -c` so an
    # oversized binary never crosses the wire (an unbounded `cat` once
    # pinned the gateway's tool thread for 30+ min on a shlex scan). One
    # byte over budget is enough for lifecycle_guard to fail closed. The
    # `< path` redirect keeps leading-dash paths out of argv.
    try:
        result = env.execute(f"head -c {max_bytes + 1} < {shlex.quote(script_path)}")
        if result.get("returncode", -1) == 0:
            output = result.get("output", "")
            return None if output and "\x00" in output else output
    except Exception:
        pass
    return None


def gateway_lifecycle_block(
    *,
    command: str,
    env: Any,
    env_type: str,
    cwd: str,
    workdir: Optional[str],
    session_key: str,
) -> Optional[str]:
    """Refuse gateway lifecycle commands issued from inside the supervised gateway.

    ``systemctl``/``launchctl``/``hermes gateway restart|stop|uninstall``
    targeting hermes-gateway would SIGTERM the gateway — and this very
    subprocess — before completing, so the service may never come back.
    Applies unconditionally (``force=True`` cannot bypass it). Gated on the
    SUPERVISED-gateway probe, not the raw ``_HERMES_GATEWAY`` marker: that
    marker leaks into every process that merely imports gateway.run (hermes
    serve, CLI, web server), which must still be able to restart the gateway;
    an unsupervised foreground ``hermes gateway run`` has no KeepAlive to turn
    a self-restart into a respawn loop, so it passes too.
    Returns the JSON error string when blocked, else None.
    """
    from tools.process_registry import _is_supervised_gateway_process
    from tools.terminal_tool import _resolve_command_cwd, get_session_cwd

    if not _is_supervised_gateway_process():
        return None
    from cron.lifecycle_guard import (
        _MAX_REFERENCED_SCRIPT_BYTES,
        HOST_INTERPRETER_KILL_REJECTION,
        contains_host_interpreter_kill,
        contains_launchctl_submit_command,
        lifecycle_scan_root_within_budget,
        scan_gateway_lifecycle,
    )
    # Keep the specific launchctl diagnostic when this optional pre-scan fits the
    # budget. The full fail-closed guard below still runs when it does not, so
    # oversized roots never reach shlex here.
    if lifecycle_scan_root_within_budget(command) and contains_launchctl_submit_command(command):
        return _blocked_json(
            "Blocked: launchctl submit/bootstrap is restricted inside a supervised "
            "gateway regardless of the job label, to prevent indirect gateway "
            "restart loops. This guard does not inspect the job's KeepAlive settings "
            "or determine whether it is independent of Hermes. Perform authorized "
            "LaunchAgent maintenance from a separate shell outside the gateway, "
            "not by switching launchctl verbs to bypass this rejection.",
            "error",
        )
    guard_cwd_base = get_session_cwd(session_key)
    if guard_cwd_base is None:
        guard_cwd_base = getattr(env, "cwd", None) or cwd
    guard_cwd = _resolve_command_cwd(
        workdir=workdir, default_cwd=guard_cwd_base, session_key=session_key, env_type=env_type,
        mounted_host=getattr(env, "host_cwd", None),
        env=env,
    )
    unsafe, refusal = scan_gateway_lifecycle(
        command,
        cwd=guard_cwd,
        read_remote_script=lambda p: _read_script_for_guard(env, guard_cwd, p, _MAX_REFERENCED_SCRIPT_BYTES),
    )
    if unsafe and refusal:
        # Not a lifecycle command: a script the command EXECUTES could not be scanned (budget,
        # size, device, live SQLite, cloud placeholder). Say so, or the model rewords and retries
        # the same command in a loop (#113944).
        return _blocked_json(
            f"Blocked: the lifecycle guard could not scan this command or referenced script: {refusal}. "
            "Nothing in the command is known to contain a gateway lifecycle command, but a "
            "script the command executes must be scannable (a regular text file under 1 MiB) "
            "before it can run inside the gateway process.",
            "error",
        )
    if unsafe:
        # Name the ownership-scoped route for image-name kills: the intent is almost always "stop
        # MY background job", and re-rolling the same over-broad spelling is what takes the gateway down.
        if lifecycle_scan_root_within_budget(command) and contains_host_interpreter_kill(command):
            return _blocked_json(HOST_INTERPRETER_KILL_REJECTION, "error")
        return _blocked_json(
            "Blocked: command or referenced script cannot restart, stop, or "
            "uninstall the gateway from inside the gateway process. The gateway would "
            "kill this command before it could complete (SIGTERM propagates "
            "to child processes). Run `hermes gateway restart` from a "
            "separate shell outside the running gateway.",
            "error",
        )
    return None


def self_repo_block(
    *,
    command: str,
    cwd: str,
    workdir: Optional[str],
    session_key: str,
) -> Optional[str]:
    """Windows-only guard against git-mutating the checkout backing this interpreter.

    NTFS locks loaded module files, so rewriting the live checkout can corrupt
    the running process; POSIX keeps old inodes alive for open handles, so the
    guard is off there (``guard_active``). Local backend only — remote
    backends cannot reach that checkout. Returns the JSON error string when
    blocked, else None.
    """
    from tools.self_repo_guard import detect_self_repo_git_mutation, guard_active
    from tools.terminal_tool import _resolve_command_cwd

    if not guard_active():
        return None
    guard_cwd = _resolve_command_cwd(workdir=workdir, default_cwd=cwd, session_key=session_key)
    hit, msg = detect_self_repo_git_mutation(command, guard_cwd)
    if not hit:
        return None
    logger.warning("Blocked self-repo git mutation (command: %s)", _safe_command_preview(command))
    return _blocked_json(msg, "blocked")
