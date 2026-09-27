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

A binding is PROOF of a value only when it dominates the use (``approval_detection_shell_scope``):
it always runs, in the same shell, before the use, and is not a prefix-only temporary
(``X=echo true``). Any other binding (conditional, subshell, pipeline, function or loop body, a
heredoc line) only ADDS a possible value. A name no dominating binding fixes also keeps the
environment's value, the explicit ``UNKNOWN``. ``read``/``unset``/``mapfile``/``getopts``/``printf -v``
make their names UNKNOWN; ``eval``/``source``/``.`` make every name UNKNOWN. When a bound is hit,
the dropped values become UNKNOWN too. Nothing is ever dropped silently.

For detection variants this over-approximates: a wrong guess only adds a variant, and a variant can
only add blocks. For the opaque-leader classifier, UNKNOWN among a command word's values means the
command cannot be read.
"""

import functools
import re
from dataclasses import dataclass

from tools.approval_detection_shell_scope import ShellScope

# The environment's (or any unreadable) value of a name. Substituted back as the reference text.
UNKNOWN = "\x00<unknown>"
# NAME=value / NAME+=value as a whole shell word (the name part is never quoted in real shell).
_ASSIGNMENT_WORD_RE = re.compile(r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<append>\+?)=(?P<value>.*)", re.DOTALL)
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Declaration builtins whose operands are assignments (`export X="rm -rf /"`).
_DECLARATION_BUILTINS = frozenset({"export", "declare", "typeset", "local", "readonly"})
# Builtins that give their NAME operands a value the command text does not show.
_NAME_READERS = frozenset({"read", "unset", "mapfile", "readarray", "getopts"})
# Builtins that can set any name at all.
_ANY_NAME_SETTERS = frozenset({"eval", "source", "."})
# $NAME, ${NAME}, ${NAME[...]}, ${NAME:-word} / ${NAME-word} / ${NAME:+word} / ${NAME:=word}.
_REFERENCE_RE = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]}]*\])?(?:(?P<op>:?[-=?+])(?P<word>[^}]*))?\}"
    r"|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)
# A command word that is one variable reference or one command substitution, optionally double-quoted.
_OPAQUE_LEADER_RE = re.compile(
    r'"?(?:\$(?:\{[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]}]*\])?(?::?[-=?+][^}]*)?\}|[A-Za-z_][A-Za-z0-9_]*)'
    r'|\$\(.*\)|`.*`)"?',
    re.DOTALL,
)
# A for-loop word the resolver cannot enumerate (glob, expansion, substitution).
_UNENUMERABLE_WORD_RE = re.compile(r"[$`*?\[]")
_CANDIDATE_ASSIGNMENT_RE = re.compile(r"(?<![\w$/.-])([A-Za-z_][A-Za-z0-9_]*)\+?=|\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s")
_CANDIDATE_REFERENCE_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
# Bounds. Whole-command variants are capped (they cost a full detection pass each); a reference with
# more values than that gets one short per-value variant of its own simple command instead, so every
# value is still examined. Past _MAX_VALUES_PER_NAME the remainder becomes UNKNOWN (fail closed).
_MAX_VALUES_PER_NAME = 64
_MAX_VARIANTS = 8
_MAX_LOOP_WORDS = 64
_MAX_PAYLOAD_DEPTH = 3


@dataclass(frozen=True)
class _Binding:
    """One assignment, loop binding or name-clobbering builtin. Its command starts at *start*; it
    takes effect at *end*. *name* None means every name (``eval``)."""
    start: int
    end: int
    name: str | None
    raw_values: tuple[str, ...]
    append: bool = False
    persistent: bool = True


def _cap(values) -> tuple[str, ...]:
    unique = tuple(dict.fromkeys(values))
    if len(unique) <= _MAX_VALUES_PER_NAME:
        return unique
    return unique[:_MAX_VALUES_PER_NAME - 1] + (UNKNOWN,)


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


def _for_loop_binding(command: str, for_start: int, pos: int) -> _Binding | None:
    """``for NAME in w1 w2 ...`` (*pos* just past ``for``): NAME bound to each literal word. A word
    no detector can enumerate, or more than _MAX_LOOP_WORDS words, adds UNKNOWN."""
    from tools.approval_detection import _deobfuscate_shell_word_for_detection, _read_shell_word
    _, pos, name = _read_shell_word(command, pos)
    if not _NAME_RE.fullmatch(name):
        return None
    _, after, keyword = _read_shell_word(command, pos)
    if keyword != "in":
        # `for NAME; do` / `for NAME do` iterate the positional parameters: unknown.
        return _Binding(for_start, pos, name, (UNKNOWN,))
    pos = after
    words: list[str] = []
    while pos < len(command):
        start, end, word = _read_shell_word(command, pos)
        if start == end or word == "do":
            break
        if _UNENUMERABLE_WORD_RE.search(word) or len(words) >= _MAX_LOOP_WORDS:
            words.append(UNKNOWN)
        else:
            words.append(_deobfuscate_shell_word_for_detection(word))
        pos = end
    return _Binding(for_start, pos, name, tuple(dict.fromkeys(words)) or (UNKNOWN,))


def _reader_bindings(command: str, builtin: str, start: int, pos: int) -> list[_Binding]:
    """NAME operands of read/unset/mapfile/readarray/getopts and ``printf -v NAME``: UNKNOWN."""
    from tools.approval_detection import _deobfuscate_shell_word_for_detection, _read_shell_word
    found, previous = [], None
    while pos < len(command):
        word_start, word_end, word = _read_shell_word(command, pos)
        if word_start == word_end or "\n" in command[pos:word_start]:
            break
        plain = _deobfuscate_shell_word_for_detection(word)
        if _NAME_RE.fullmatch(plain) and (builtin != "printf" or previous == "-v"):
            found.append(_Binding(start, word_end, plain, (UNKNOWN,)))
        previous, pos = plain, word_end
    return found


def _collect_bindings(command: str) -> list[_Binding]:
    """Every assignment / loop binding / name clobber at a command position, in text order. Values
    are deobfuscated the way command words are (quotes and escapes removed, tiny literal
    substitutions folded), so ``X="rm -rf /home"`` binds X to ``rm -rf /home``."""
    from tools.approval_detection import (
        _deobfuscate_shell_word_for_detection, _iter_shell_command_starts, _read_shell_word,
    )
    bindings: list[_Binding] = []
    for pos in _iter_shell_command_starts(command):
        first, declaration = True, False
        pending: list[_Binding] = []
        while pos < len(command):
            start, end, word = _read_shell_word(command, pos)
            if start == end or (not first and "\n" in command[pos:start]):
                break   # an unquoted newline ends the simple command
            if first and word == "for":
                loop = _for_loop_binding(command, start, end)
                if loop is not None:
                    bindings.append(loop)
                break
            if first and word in _ANY_NAME_SETTERS:
                bindings.append(_Binding(start, end, None, (UNKNOWN,)))
                break
            if first and (word in _NAME_READERS or word == "printf"):
                bindings.extend(_reader_bindings(command, word, start, end))
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
                # `X=v cmd ...`: the assignments only reach cmd's environment, not the shell.
                if not declaration:
                    pending = [_Binding(b.start, b.end, b.name, b.raw_values, b.append, False) for b in pending]
                break
            raw_value = match.group("value")
            if raw_value == "" and end < len(command) and command[end] == "(":
                array = _array_value(command, end)
                if array is None:
                    break
                value, end = array
            else:
                value = _deobfuscate_shell_word_for_detection(raw_value)
            pending.append(_Binding(start, end, match.group("name"), (value,), bool(match.group("append"))))
            pos = end
        bindings.extend(pending)
    bindings.sort(key=lambda binding: binding.end)
    return bindings


def _reference_name(match) -> str:
    return match.group("braced") or match.group("bare")


def _reference_values(match, values: tuple[str, ...]) -> tuple[str, ...]:
    """What the reference *match* may expand to, given its name's possible *values*. UNKNOWN is
    kept (callers substitute the reference text for it); ``${X:-word}``-style words are added."""
    from tools.approval_detection import _deobfuscate_shell_word_for_detection
    if match.group("op") and match.group("op").lstrip(":") in "-=+":
        return _cap(values + (_deobfuscate_shell_word_for_detection(match.group("word")),))
    return values


def _expand_references(text: str, values_of) -> tuple[tuple[str, ...], list[str]]:
    """*text* with each ``$NAME`` replaced (see module doc). Returns (whole-text variants, one per
    value index up to _MAX_VARIANTS) and extra per-value variants of the simple command holding a
    reference whose values did not all fit. *values_of(name, offset)* returns the possible values
    or None to leave the reference as is."""
    from tools.approval_detection import _scan_shell
    pieces: list[str | tuple[str, ...]] = []
    overflow: list[tuple[int, int, tuple[str, ...]]] = []
    width = 1
    skip_to = 0
    for kind, i, j, quote in _scan_shell(text):
        if i < skip_to:
            continue
        if kind == "char" and quote != "'" and text[i] == "$":
            match = _REFERENCE_RE.match(text, i)
            values = values_of(_reference_name(match), i) if match else None
            if match and values:
                values = tuple(text[i:match.end()] if v == UNKNOWN else v
                               for v in _reference_values(match, values))
                pieces.append(values)
                width = max(width, len(values))
                if len(values) > _MAX_VARIANTS:
                    overflow.append((i, match.end(), values))
                skip_to = match.end()
                continue
        pieces.append(text[i:j])
    whole = tuple(dict.fromkeys("".join(p if isinstance(p, str) else p[k % len(p)] for p in pieces)
                                for k in range(min(width, _MAX_VARIANTS))))
    return whole, _overflow_variants(text, overflow)


def _overflow_variants(text: str, overflow) -> list[str]:
    if not overflow:
        return []
    from tools.approval_detection import _iter_shell_command_starts, _shell_command_segment
    starts = sorted(_iter_shell_command_starts(text))
    extra: list[str] = []
    for i, j, values in overflow:
        begin = max((s for s in starts if s <= i), default=0)
        tail = _shell_command_segment(text, j) if j < len(text) else ""
        extra.extend(text[begin:i] + value + (" " + tail if tail else "") for value in values[_MAX_VARIANTS:])
    return list(dict.fromkeys(extra))


class _Resolution:
    """Per-use values of every name in one command."""

    def __init__(self, command: str):
        self.bindings = _collect_bindings(command)
        self.scope = ShellScope(command)
        self._memo: dict[int, tuple[str, ...]] = {}
        self._busy: set[int] = set()

    def binding_values(self, k: int) -> tuple[str, ...]:
        if k in self._memo:
            return self._memo[k]
        if k in self._busy:
            return (UNKNOWN,)
        self._busy.add(k)
        b = self.bindings[k]
        values: list[str] = []
        for raw in b.raw_values:
            if raw == UNKNOWN:
                values.append(UNKNOWN)
                continue
            whole, extra = _expand_references(raw, lambda name, _offset: self.values_at(name, b.start))
            values.extend(whole)
            values.extend(extra)
        if b.append:
            old = self.values_at(b.name, b.start)
            values = [UNKNOWN if UNKNOWN in (o, n) else o + n for o in old for n in values]
        self._busy.discard(k)
        self._memo[k] = _cap(values)
        return self._memo[k]

    def values_at(self, name: str, offset: int, *, fallback: bool = False) -> tuple[str, ...]:
        """Possible values of *name* at *offset*; UNKNOWN is among them unless a binding fixes it.
        With *fallback*, a name no earlier binding touches also takes every value it is ever given."""
        values: list[str] = [UNKNOWN]
        bound_before = False
        for k, b in enumerate(self.bindings):
            if b.name not in (name, None):
                continue
            if b.end > offset:
                # A later binding still reaches this use through a loop or a function call.
                if self.scope.in_loop_with(b.start, offset):
                    values.extend(self.binding_values(k))
                continue
            bound_before = True
            if b.persistent and self.scope.dominates(b.start, offset):
                values = list(self.binding_values(k))
            else:
                values.extend(self.binding_values(k))
        if fallback and not bound_before:
            for k, b in enumerate(self.bindings):
                if b.name == name:
                    values.extend(self.binding_values(k))
        return _cap(values)


@functools.lru_cache(maxsize=32)
def _resolution(command: str) -> _Resolution:
    return _Resolution(command)


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
    resolution = _resolution(command)
    if not resolution.bindings:
        return []
    whole, extra = _expand_references(
        command, lambda name, offset: resolution.values_at(name, offset, fallback=True))
    return [v for v in dict.fromkeys((*whole, *extra)) if v != command]


def resolve_shell_assignments(command: str) -> str | None:
    """The first resolved form of *command*, or None when nothing resolves."""
    variants = resolve_shell_assignment_variants(command)
    return variants[0] if variants else None


def _leader_is_opaque(value: str) -> bool:
    if value == UNKNOWN or not value.strip():
        return True
    return bool(_OPAQUE_LEADER_RE.fullmatch(value.split(None, 1)[0]))


def opaque_command_leaders(command: str, _depth: int = 0) -> list[tuple[str, int, int, str]]:
    """Every command word whose program this command does not fix, as ``(script, start, end, word)``
    where *script* is the text the offsets index (the command, or a shell payload inside it). An
    opaque word is a variable reference that may still hold its environment (or another unreadable)
    value at that point, a variable whose possible value starts with one, or a command substitution
    (``$(cat f) args``).

    The ORIGINAL command is scanned, not a resolved form, because splicing a deobfuscated value back
    in unquotes it and invents command positions (``B=$(printf '%s' '<?php $c=1'); echo $B``). Heredoc
    bodies fed to a non-shell are data and skipped. Shell payloads (``bash -c '...'``, the body of
    ``bash <<EOF``) are scanned as scripts of their own that start with an unknown environment."""
    from tools.approval_detection import _execution_flag_findings, _iter_shell_command_word_spans
    resolution = _resolution(command) if _has_candidate(command) else None
    scope = resolution.scope if resolution else ShellScope(command) if "<<" in command else None
    heredocs = scope.heredocs if scope else []
    found: list[tuple[str, int, int, str]] = []
    for start, end, word in _iter_shell_command_word_spans(command):
        if not _OPAQUE_LEADER_RE.fullmatch(word) or any(h.start <= start < h.end for h in heredocs):
            continue
        bare = word.strip('"')
        reference = _REFERENCE_RE.match(bare)
        if reference and reference.end() == len(bare):
            values = (_reference_values(reference, resolution.values_at(_reference_name(reference), start))
                      if resolution else (UNKNOWN,))
            if not any(_leader_is_opaque(value) for value in values):
                continue
        found.append((command, start, end, word))
    found = list(dict.fromkeys(found))
    if _depth < _MAX_PAYLOAD_DEPTH:
        payloads = [command[h.start:h.end] for h in heredocs if h.executed]
        payloads += [payload for _, payload in _execution_flag_findings(command) if payload]
        for payload in dict.fromkeys(payloads):
            if payload != command:
                found.extend(opaque_command_leaders(payload, _depth + 1))
    return list(dict.fromkeys(found))
