"""Same-command shell variable indirection for the detectors. Detection only: nothing is executed.

``X="rm -rf /home"; $X`` runs ``rm -rf /home``, but no pattern sees it: the literal command sits
inside a quoted assignment value (data) and the command word is ``$X``. That exact spelling got past
the hardline floor, and under ``approvals.single_query_mode: approve`` it wiped a home directory.
This module resolves simple ``NAME=value`` assignments made anywhere in the same command and
substitutes them back, so the detectors see the command the shell would run. It also names a command
word that is still an unresolved variable after substitution. Unattended auto-approve paths use that
to refuse commands they cannot read.

Deliberately over-approximating. Every assignment in the command applies to every later or earlier
reference, whatever the scope or order, and arrays join all their elements. A false match only adds
a detection variant, and a detection variant can only add blocks, never remove one.
"""

import re

# NAME=value / NAME+=value as a whole shell word (the name part is never quoted in real shell).
_ASSIGNMENT_WORD_RE = re.compile(r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<append>\+?)=(?P<value>.*)", re.DOTALL)
# Declaration builtins whose operands are assignments (`export X="rm -rf /"`).
_DECLARATION_BUILTINS = frozenset({"export", "declare", "typeset", "local", "readonly"})
# $NAME, ${NAME}, ${NAME[...]}, ${NAME:-...} / ${NAME-...} (resolved to NAME's value when assigned).
_REFERENCE_RE = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]}]*\])?(?::?[-=?+][^}]*)?\}|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)
# A command word that is one variable reference or one command substitution, optionally double-quoted.
_OPAQUE_LEADER_RE = re.compile(
    r'"?(?:\$(?:\{[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]}]*\])?(?::?[-=?+][^}]*)?\}|[A-Za-z_][A-Za-z0-9_]*)'
    r'|\$\(.*\)|`.*`)"?',
    re.DOTALL,
)
# Bound the work: resolution runs on every detection variant.
_MAX_RESOLVE_ROUNDS = 3
_CANDIDATE_ASSIGNMENT_RE = re.compile(r"(?<![\w$/.-])([A-Za-z_][A-Za-z0-9_]*)\+?=")
_CANDIDATE_REFERENCE_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")


def _array_value(command: str, open_paren: int) -> tuple[str, int] | None:
    """``NAME=(a b c)``: the joined element text and the offset past ``)``, or None if unclosed."""
    from tools.approval_detection import _read_shell_word, _skip_shell_whitespace, _deobfuscate_shell_word_for_detection
    elements, pos = [], open_paren + 1
    while True:
        pos = _skip_shell_whitespace(command, pos)
        if pos >= len(command):
            return None
        if command[pos] == ")":
            return (" ".join(elements), pos + 1)
        start, end, word = _read_shell_word(command, pos)
        if start == end:
            return None
        elements.append(_deobfuscate_shell_word_for_detection(word))
        pos = end


def collect_shell_assignments(command: str) -> dict[str, str]:
    """Map NAME -> literal value for the simple assignments at command positions in *command*.

    Values are deobfuscated the way command words are (quotes and escapes removed, tiny literal
    substitutions folded), so ``X="rm -rf /home"`` maps X to ``rm -rf /home``.
    """
    from tools.approval_detection import (
        _deobfuscate_shell_word_for_detection, _iter_shell_command_starts, _read_shell_word,
    )
    assignments: dict[str, str] = {}
    for pos in _iter_shell_command_starts(command):
        first = True
        declaration = False
        while pos < len(command):
            start, end, word = _read_shell_word(command, pos)
            if start == end:
                break
            if first and word in _DECLARATION_BUILTINS:
                declaration, first, pos = True, False, end
                continue
            first = False
            if declaration and word.startswith("-"):
                pos = end
                continue
            match = _ASSIGNMENT_WORD_RE.fullmatch(word)
            if not match:
                break
            name, raw_value = match.group("name"), match.group("value")
            if raw_value == "" and end < len(command) and command[end] == "(":
                array = _array_value(command, end)
                if array is None:
                    break
                value, end = array
            else:
                value = _deobfuscate_shell_word_for_detection(raw_value)
            if match.group("append"):
                value = assignments.get(name, "") + value
            assignments[name] = value
            pos = end
    return assignments


def substitute_shell_assignments(command: str, assignments: dict[str, str]) -> str:
    """Replace references to assigned names outside single quotes with their values."""
    if not assignments:
        return command
    from tools.approval_detection import _scan_shell
    out: list[str] = []
    skip_to = 0
    for kind, i, j, quote in _scan_shell(command):
        if i < skip_to:
            continue
        if kind == "char" and quote != "'" and command[i] == "$":
            match = _REFERENCE_RE.match(command, i)
            name = (match.group("braced") or match.group("bare")) if match else None
            if match and name in assignments:
                out.append(assignments[name])
                skip_to = match.end()
                continue
        out.append(command[i:j])
    return "".join(out)


def resolve_shell_assignments(command: str) -> str | None:
    """*command* with same-command variable references substituted, or None when nothing changed.

    Runs a few rounds so a chain (``A=rm; B="$A -rf /"; $B``) resolves.
    """
    if "$" not in command or "=" not in command:
        return None
    # Cheap pre-filter: some NAME= must also be referenced as $NAME / ${NAME before paying for the
    # quote-aware parse (most commands with `=` are flags like --opt=value or key=value data).
    names = set(_CANDIDATE_ASSIGNMENT_RE.findall(command))
    if not names or not any(ref in names for ref in _CANDIDATE_REFERENCE_RE.findall(command)):
        return None
    resolved = command
    for _ in range(_MAX_RESOLVE_ROUNDS):
        substituted = substitute_shell_assignments(resolved, collect_shell_assignments(resolved))
        if substituted == resolved:
            break
        resolved = substituted
    return resolved if resolved != command else None


def opaque_command_leaders(command: str) -> tuple[str, list[tuple[int, int, str]]]:
    """The resolved command plus every command word in it that is still opaque after same-command
    assignments are substituted: a bare variable reference (``$CMD args``) or a command
    substitution (``$(cat f) args``). Such a word takes its program from the environment or another
    command's output at run time (a loop variable, a sourced file), so no detector can say what it
    runs."""
    from tools.approval_detection import _iter_shell_command_word_spans
    resolved = resolve_shell_assignments(command) or command
    return resolved, [span for span in _iter_shell_command_word_spans(resolved) if _OPAQUE_LEADER_RE.fullmatch(span[2])]
