"""Same-command shell variable indirection for the detectors. Detection only: nothing is executed.

``X="rm -rf /home"; $X`` runs ``rm -rf /home``, but no pattern sees it: the literal command sits
inside a quoted assignment value (data) and the command word is ``$X``. That exact spelling got past
the hardline floor, and under ``approvals.single_query_mode: approve`` it wiped a home directory.
This module resolves ``NAME=value`` assignments (and ``for NAME in words`` loop bindings) made in the
same command and substitutes them back, so the detectors see the command the shell would run. It
also names every command word that is still an unresolved expansion, which unattended auto-approve
paths use to refuse commands they cannot read.

Resolution is per use, not per name. Each reference takes the value(s) the name holds at that
point in the text, and an assignment's own value is resolved when it is made. So a later
reassignment never erases an earlier destructive use: ``X="rm -rf /home"; $X; X=echo`` still shows
``rm -rf /home``, and ``Y=$X`` copies X's value at that moment. A name can hold several possible
values (loop words, ``+=`` onto a multi-valued name). Each value then gets its own resolved
variant, so every value is seen in command position at least once.

It deliberately over-approximates. A reference with no earlier assignment (a function body defined
first, a loop) resolves to every value the name is given anywhere in the command. A wrong guess
only adds a detection variant, and a variant can only add blocks, never remove one.
"""

import re
from dataclasses import dataclass

# NAME=value / NAME+=value as a whole shell word (the name part is never quoted in real shell).
_ASSIGNMENT_WORD_RE = re.compile(r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<append>\+?)=(?P<value>.*)", re.DOTALL)
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
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
# A for-loop word the resolver cannot enumerate (glob, expansion, substitution): the loop variable
# then stays unresolved rather than being bound to the literal text.
_UNENUMERABLE_WORD_RE = re.compile(r"[$`*?\[]")
_CANDIDATE_ASSIGNMENT_RE = re.compile(r"(?<![\w$/.-])([A-Za-z_][A-Za-z0-9_]*)\+?=|\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s")
_CANDIDATE_REFERENCE_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
# Bounds: the resolver runs on every detection variant, and multi-valued names multiply variants.
_MAX_VALUES_PER_NAME = 16
_MAX_VARIANTS = 8
_MAX_LOOP_WORDS = 64


@dataclass(frozen=True)
class _Binding:
    """One assignment or loop binding. It takes effect at *end* (the offset past its value)."""
    end: int
    name: str
    raw_values: tuple[str, ...]
    append: bool = False


def _dedupe(values, limit: int = _MAX_VALUES_PER_NAME) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))[:limit]


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


def _for_loop_binding(command: str, pos: int) -> _Binding | None:
    """``for NAME in w1 w2 ...`` at *pos* (just past ``for``): NAME bound to each literal word.
    None when the list holds a word no detector can enumerate, or it is not a for-in loop."""
    from tools.approval_detection import _deobfuscate_shell_word_for_detection, _read_shell_word
    _, pos, name = _read_shell_word(command, pos)
    if not _NAME_RE.fullmatch(name):
        return None
    _, pos, keyword = _read_shell_word(command, pos)
    if keyword != "in":
        return None
    words: list[str] = []
    while pos < len(command) and len(words) < _MAX_LOOP_WORDS:
        start, end, word = _read_shell_word(command, pos)
        if start == end or word == "do":
            break
        if _UNENUMERABLE_WORD_RE.search(word):
            return None
        words.append(_deobfuscate_shell_word_for_detection(word))
        pos = end
    return _Binding(pos, name, tuple(words)) if words else None


def _collect_bindings(command: str) -> list[_Binding]:
    """Every assignment / for-loop binding at a command position, in text order. Values are
    deobfuscated the way command words are (quotes and escapes removed, tiny literal substitutions
    folded), so ``X="rm -rf /home"`` binds X to ``rm -rf /home``."""
    from tools.approval_detection import (
        _deobfuscate_shell_word_for_detection, _iter_shell_command_starts, _read_shell_word,
    )
    bindings: list[_Binding] = []
    for pos in _iter_shell_command_starts(command):
        first = True
        declaration = False
        while pos < len(command):
            start, end, word = _read_shell_word(command, pos)
            if start == end:
                break
            if first and word == "for":
                loop = _for_loop_binding(command, end)
                if loop is not None:
                    bindings.append(loop)
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
            raw_value = match.group("value")
            if raw_value == "" and end < len(command) and command[end] == "(":
                array = _array_value(command, end)
                if array is None:
                    break
                value, end = array
            else:
                value = _deobfuscate_shell_word_for_detection(raw_value)
            bindings.append(_Binding(end, match.group("name"), (value,), bool(match.group("append"))))
            pos = end
    bindings.sort(key=lambda binding: binding.end)
    return bindings


def _expand_references(text: str, values_of) -> tuple[str, ...]:
    """*text* with each ``$NAME`` replaced, one result per value index (see module doc).
    *values_of(name, offset)* returns the possible values or None to leave the reference as is."""
    from tools.approval_detection import _scan_shell
    pieces: list[str | tuple[str, ...]] = []
    width = 1
    skip_to = 0
    for kind, i, j, quote in _scan_shell(text):
        if i < skip_to:
            continue
        if kind == "char" and quote != "'" and text[i] == "$":
            match = _REFERENCE_RE.match(text, i)
            values = values_of(match.group("braced") or match.group("bare"), i) if match else None
            if match and values:
                pieces.append(values)
                width = max(width, len(values))
                skip_to = match.end()
                continue
        pieces.append(text[i:j])
    width = min(width, _MAX_VARIANTS)
    return _dedupe(("".join(p if isinstance(p, str) else p[k % len(p)] for p in pieces) for k in range(width)),
                   _MAX_VARIANTS)


def _resolver(command: str, *, fallback: bool):
    """A ``values_of(name, offset)`` for *command*: the values *name* holds at *offset*. With
    *fallback*, a name not yet bound there resolves to every value it is ever given."""
    env_at: list[tuple[int, dict[str, tuple[str, ...]]]] = []
    env: dict[str, tuple[str, ...]] = {}
    ever: dict[str, tuple[str, ...]] = {}
    for binding in _collect_bindings(command):
        current = dict(env)

        def at_binding(name, _offset, _env=current):
            return _env.get(name)
        resolved = _dedupe(v for raw in binding.raw_values for v in _expand_references(raw, at_binding))
        if binding.append:
            resolved = _dedupe(old + new for old in env.get(binding.name, ("",)) for new in resolved)
        env = {**env, binding.name: resolved}
        ever[binding.name] = _dedupe(ever.get(binding.name, ()) + resolved)
        env_at.append((binding.end, env))

    def values_of(name: str, offset: int):
        bound = None
        for end, snapshot in env_at:
            if end > offset:
                break
            bound = snapshot
        values = bound.get(name) if bound else None
        return values if values is not None or not fallback else ever.get(name)
    return values_of


def _has_candidate(command: str) -> bool:
    # Cheap pre-filter: some bound NAME must also be referenced as $NAME / ${NAME before paying for
    # the quote-aware parse (most commands with `=` are flags like --opt=value or key=value data).
    if "$" not in command:
        return False
    names = {a or b for a, b in _CANDIDATE_ASSIGNMENT_RE.findall(command)}
    return bool(names) and any(ref in names for ref in _CANDIDATE_REFERENCE_RE.findall(command))


def resolve_shell_assignment_variants(command: str) -> list[str]:
    """Every resolved form of *command* that differs from it (empty when nothing resolves)."""
    if not _has_candidate(command):
        return []
    return [v for v in _expand_references(command, _resolver(command, fallback=True)) if v != command]


def resolve_shell_assignments(command: str) -> str | None:
    """The first resolved form of *command*, or None when nothing resolves."""
    variants = resolve_shell_assignment_variants(command)
    return variants[0] if variants else None


def opaque_command_leaders(command: str) -> list[tuple[str, int, int, str]]:
    """Every command word that stays opaque after same-command bindings, as
    ``(command, start, end, word)``. An opaque word is a variable reference with no binding earlier
    in the command (``$CMD args``), a variable whose bound value itself starts with one, or a
    command substitution (``$(cat f) args``). It takes its program from the environment or another
    command's output at run time, so no detector can say what it runs.

    Only bindings made BEFORE the use count (no fallback): the environment decides the rest. The
    ORIGINAL command is scanned, not a resolved form, because splicing a deobfuscated value back in
    unquotes it and invents command positions (``B=$(printf '%s' '<?php $c=1'); echo $B``).
    Heredoc bodies are stdin data, so words inside them are skipped."""
    from tools.approval_detection import _iter_shell_command_word_spans, _quoted_heredoc_body_spans
    heredocs = _quoted_heredoc_body_spans(command, quoted_only=False) if "<<" in command else []
    values_of = _resolver(command, fallback=False) if _has_candidate(command) else (lambda _name, _offset: None)
    found: list[tuple[str, int, int, str]] = []
    for start, end, word in _iter_shell_command_word_spans(command):
        if not _OPAQUE_LEADER_RE.fullmatch(word) or any(lo <= start < hi for lo, hi in heredocs):
            continue
        reference = _REFERENCE_RE.search(word)
        if reference and word.strip('"').startswith("$") and not word.strip('"').startswith(("$(", "$`")):
            values = values_of(reference.group("braced") or reference.group("bare"), start)
            if values is not None and not any(
                    _OPAQUE_LEADER_RE.fullmatch(value.split(None, 1)[0] if value.strip() else value)
                    for value in values):
                continue
        found.append((command, start, end, word))
    return found
