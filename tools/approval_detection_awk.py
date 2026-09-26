"""Does an inline awk program hand text to /bin/sh? (detection only; nothing is executed).

``system()``, ``print | "cmd"``, ``"cmd" | getline`` and gawk's ``|&`` coprocess all run a shell
command. Those spellings are only execution syntax in awk CODE: inside a string (``print "a|b"``),
a regex literal (``/system\\(/``, ``print /a|b/``) or a ``#`` comment they are data, and treating
them as execution made read-only one-liners prompt.
"""

import re

AWK_NAMES = frozenset({"awk", "gawk", "mawk", "nawk"})
AWK_EXEC_DESCRIPTION = "awk program runs a shell command (system()/pipe)"
_AWK_OPTIONS_WITH_ARG = {"-F", "--field-separator", "-v", "--assign", "-f", "--file", "-e", "--source",
                         "-i", "--include", "-l", "--load", "-W"}
_AWK_COMMAND_EXEC_RE = re.compile(r'\bsystem\s*\(|\|&|\|\s*getline\b|\bprintf?\b[^;}\n]*(?<!\|)\|(?![|&])')
# After one of these keywords a `/` opens a regex (`print /re/`); after any other operand it divides.
_REGEX_AFTER_KEYWORDS = frozenset({"print", "printf", "return", "in", "case", "getline", "delete"})
_OPERAND_END = re.compile(r"[A-Za-z0-9_$.)\]]")
_TRAILING_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")


def awk_program_texts(args: list[str]) -> list[str]:
    """Return the inline awk program text(s) in *args* (``-f`` files are not inspectable)."""
    texts, index, from_file = [], 0, False
    while index < len(args):
        token = args[index]
        if token == "--":
            index += 1
            break
        if token == "-" or not token.startswith("-"):
            break
        option, equals, value = token.partition("=")
        attached = token[2:] if not token.startswith("--") and len(token) > 2 else None
        takes_arg = option in _AWK_OPTIONS_WITH_ARG or token[:2] in _AWK_OPTIONS_WITH_ARG
        if takes_arg and not equals and attached is None:
            value = args[index + 1] if index + 1 < len(args) else ""
            index += 2
        else:
            value = value if equals else (attached or "")
            index += 1
        name = option if option.startswith("--") else token[:2]
        if name in ("-e", "--source"):
            texts.append(value)
        from_file = from_file or name in ("-f", "--file")
    if not texts and not from_file and index < len(args):
        texts.append(args[index])
    return texts


def _regex_may_start(code: list[str]) -> bool:
    """Whether a ``/`` read now opens a regex literal, judged from the code emitted so far."""
    tail = "".join(code[-32:]).rstrip(" \t")
    if not tail or not _OPERAND_END.match(tail[-1]):
        return True
    word = _TRAILING_WORD.search(tail)
    return bool(word) and word.group() in _REGEX_AFTER_KEYWORDS


def _scan_delimited(program: str, i: int, closer: str, brackets: bool) -> int:
    """Index just past the literal opened at *i* (``"..."`` or ``/.../``); a regex ``[...]`` class may
    hold an unescaped ``/``. An unterminated literal runs to the end of its line, as awk rejects it."""
    j, n, in_class = i + 1, len(program), False
    while j < n and program[j] != "\n":
        ch = program[j]
        if ch == "\\":
            j += 2
            continue
        if brackets and ch == "[" and not in_class:
            # A `]` right after `[` or `[^` is a literal member, not the class closer.
            in_class, j = True, j + 1
            j += program.startswith("^", j)
            j += program.startswith("]", j)
            continue
        if in_class:
            in_class = ch != "]"
        elif ch == closer:
            return j + 1
        j += 1
    return min(j, n)


def awk_code_only(program: str) -> str:
    """*program* with string literals reduced to ``""``, regex literals to ``//`` and comments
    dropped, so only awk code remains."""
    code: list[str] = []
    i, n = 0, len(program)
    while i < n:
        ch = program[i]
        if ch == '"':
            i = _scan_delimited(program, i, '"', brackets=False)
            code.append('""')
        elif ch == "/" and _regex_may_start(code):
            i = _scan_delimited(program, i, "/", brackets=True)
            code.append("//")
        elif ch == "#":
            end = program.find("\n", i)
            i = n if end < 0 else end
        elif ch == "\\" and i + 1 < n:
            code.append(program[i:i + 2])
            i += 2
        else:
            code.append(ch)
            i += 1
    return "".join(code)


def awk_program_runs_shell(args: list[str]) -> bool:
    return any(_AWK_COMMAND_EXEC_RE.search(awk_code_only(text)) for text in awk_program_texts(args))
