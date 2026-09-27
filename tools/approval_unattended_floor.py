"""What an unattended approve-mode context may still refuse. Pure classification, no approval state.

``approvals.single_query_mode: approve`` (and ``cron_mode`` / ``unattended_mode``) auto-approve every
dangerous-pattern and Tirith finding, because nobody is present to answer a prompt. Below the
hardline floor, that made both detectors advisory in exactly the runs with nobody watching. A kanban
worker's test script then sent ``X="rm -rf /home"; $X`` through the real terminal tool, and the
command ran. The hardline floor now resolves that spelling, but approve mode still passes some
commands, and this module refuses them:

* The program is unreadable and the arguments look destructive. A command word that is still a
  bare ``$VAR`` or ``$(...)`` after same-command assignments are substituted takes its program
  from the environment at run time. It is refused when its arguments carry a short recursive
  flag group (``-rf``, ``-R``), ``--recursive``, a raw-device ``of=/dev/...``, or would be
  hardline under ``rm`` (``$X -rf ~/.local``, ``$X /home``). ``$PY -c ...``, ``$f`` alone and
  ``for p in ...; do $p --version`` stay allowed. A bare ``$X`` with no arguments is not refused:
  approve mode already runs arbitrary code (``curl | sh`` is only a dangerous-pattern hit), and in
  real history that shape is almost always a detector misread of heredoc or loop text.
* Two independent scanners say "destroys data". Tirith returns ``block`` with a blast-radius
  finding (``blast_*``: system-path write, deletes outside the repo, find -delete, rsync --delete,
  empty-variable glob) AND the dangerous-pattern class is destructive (rm/dd/mkfs/find -delete).
  Either one alone stays advisory, and so does a Tirith block for any other reason
  (``analysis_incomplete`` fires on most loops and ``$(...)``). A replay of historical terminal
  calls found a Tirith block plus a destructive pattern on hundreds of routine commands, such as
  ``rm -f /tmp/x.py`` next to a ``for`` loop.

A refusal is not a hardline block. yolo and ``approvals.mode: off`` still bypass it, and an
interactive user can still approve the same command. Only the unattended auto-approve is withdrawn.
"""

import re

from tools.approval_detection_assignments import opaque_command_leaders

# Dangerous-pattern descriptions that destroy data (not merely "risky"): the classes where an
# unattended auto-approve has no recovery path short of a backup restore.
DESTRUCTIVE_DESCRIPTIONS = frozenset({
    "delete in root path", "recursive delete", "recursive delete (long flag)",
    "recursive delete (flags after operands)", "xargs with rm", "find -exec/-execdir rm", "find -delete",
    "format filesystem", "disk copy", "write to block device",
})
# Tirith's blast-radius rule family (tirith 0.4.x rule ids are ``blast_<what>``).
_TIRITH_BLAST_RULE_PREFIX = "blast_"
# Destructive-looking argv for an unknown program: a short-option group holding r/R (`-rf`, `-R`,
# `-fr`), --recursive, or a dd-style raw-device output. `--version` / `-c` / `-m` do not match.
_DESTRUCTIVE_ARGS_RE = re.compile(r'(?:^|\s)(?:-[A-Za-z]*[rR][A-Za-z]*|--recursive)(?=\s|$)|\bof=/dev/')


def _opaque_leader_refusal(command: str) -> str | None:
    from tools.approval_detection import _shell_command_segment, detect_hardline_command
    resolved, spans = opaque_command_leaders(command)
    for _, end, word in spans:
        arguments = _shell_command_segment(resolved, end)
        if arguments and (_DESTRUCTIVE_ARGS_RE.search(arguments) or detect_hardline_command(f"rm {arguments}")[0]):
            return (f"command word {word} is an unresolved shell expansion and its arguments "
                    f"({arguments[:80]}) look destructive, so what it runs cannot be inspected")
    return None


def tirith_blast_rules(tirith_result: dict | None) -> list[str]:
    """The blast-radius rule ids in a Tirith ``block`` verdict (empty for allow/warn)."""
    if not tirith_result or tirith_result.get("action") != "block":
        return []
    return [str(f.get("rule_id")) for f in tirith_result.get("findings") or []
            if str(f.get("rule_id", "")).startswith(_TIRITH_BLAST_RULE_PREFIX)]


def unattended_approve_refusal(command: str, *, dangerous_description: str | None,
                               tirith_result: dict | None) -> str | None:
    """Why an unattended approve-mode context must NOT auto-approve *command*, or None if it may."""
    blast = tirith_blast_rules(tirith_result)
    if blast and dangerous_description in DESTRUCTIVE_DESCRIPTIONS:
        return (f"Tirith blocked it ({', '.join(blast)}) and it matches the destructive pattern "
                f"'{dangerous_description}'")
    return _opaque_leader_refusal(command)
