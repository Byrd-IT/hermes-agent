"""Leading redirects / env assignments must not hide a command-position rule.

`>/dev/null reboot` runs `reboot` with stdout discarded, exactly like plain `reboot`. The
command-position anchor (`_CMDPOS`) used to treat the redirect as the command word, so the
hardline and dangerous rules that match only at command position saw nothing.
Invariant: a prefix of redirections and `NAME=value` assignments never changes the verdict.
"""

import pytest

from tools.approval import detect_dangerous_command, detect_hardline_command

# Commands matched only at command position (hardline and dangerous tiers).
_COMMANDS = [
    "reboot",
    "shutdown -h now",
    "poweroff",
    "halt",
    "init 0",
    "systemctl poweroff",
    "telinit 6",
    "mkfs.ext4 /dev/sda1",
    "mkfs /dev/sda",
    "dd if=/dev/zero of=/dev/sda",
    "rm -rf /",
    "rm -rf /etc",
]

_PREFIXES = [
    ">/dev/null ",
    "> /dev/null ",
    ">f ",
    ">>f ",
    "&>f ",
    "&>/dev/null ",
    "&>>f ",
    "2>/dev/null ",
    "2>&1 ",
    "1>f ",
    "3>f ",
    ">&2 ",
    ">| f ",
    "<<<x ",
    "<<< x ",
    "< /etc/hostname ",
    "<>f ",
    "2>&- ",
    "{fd}>f ",
    '>"a b" ',
    ">'a b' ",
    ">/dev/null 2>&1 ",
    "FOO=1 ",
    "FOO=1 BAR=2 ",
    'FOO="a b" ',
    "FOO=1 >f ",
    ">f FOO=1 ",
    "sudo >f ",
    "sudo FOO=1 ",
    "env >f ",
]

_STARTS = ["{p}{c}", "echo hi; {p}{c}", "echo hi && {p}{c}", "true | {p}{c}", "$({p}{c})", "(\n{p}{c}\n)"]


def _verdicts(command: str):
    return (bool(detect_hardline_command(command)[0]), bool(detect_dangerous_command(command)[0]))


@pytest.mark.parametrize("command", _COMMANDS)
@pytest.mark.parametrize("prefix", _PREFIXES)
def test_prefix_does_not_change_verdict(prefix, command):
    plain = _verdicts(command)
    assert plain != (False, False), f"fixture {command!r} is not flagged at all"
    assert _verdicts(prefix + command) == plain


@pytest.mark.parametrize("template", _STARTS)
@pytest.mark.parametrize("command", ["reboot", "mkfs.ext4 /dev/sda1"])
def test_prefix_after_separator_or_substitution(template, command):
    plain = _verdicts(command)
    assert _verdicts(template.format(p=">/dev/null ", c=command)) == plain
    assert _verdicts(template.format(p="&>f FOO=1 ", c=command)) == plain


@pytest.mark.parametrize("command", [">/dev/null reboot", "&>f reboot", ">/dev/null mkfs.ext4 /dev/sda1"])
def test_issue_examples_are_flagged(command):
    assert _verdicts(command) != (False, False)


_ALLOWED = [
    "echo reboot",
    ">f echo reboot",
    "&>f echo shutdown now",
    "> reboot",
    ">reboot",
    "echo hi > reboot",
    "ls 2>&1",
    "FOO=reboot ls",
    "FOO=1 ls",
    "FOO=1 >f ls",
    ">/dev/null grep reboot /var/log/syslog",
    "<<<reboot cat",
    "cat <<<mkfs",
    "2>&1 echo mkfs.ext4",
    ">f git commit -m 'reboot the box'",
    "reboot_count=1 ls",
    "ls /tmp/reboot",
    ">/dev/null true",
]


@pytest.mark.parametrize("command", _ALLOWED)
def test_non_dangerous_stays_allowed(command):
    assert _verdicts(command) == (False, False)


def test_long_redirect_runs_stay_linear():
    command = ">f " * 20000 + "ls"
    assert _verdicts(command)[0] is not None  # returns (parser-limit verdicts allowed), does not hang


@pytest.mark.parametrize("prefix", ["2>&1 ", ">&2 ", "2>&- ", "<&0 ", ">/dev/null 2>&1 "])
@pytest.mark.parametrize("command", ["bash -c 'reboot'", "sh -c \"mkfs.ext4 /dev/sda1\"", "python3 -c 'print(1)'"])
def test_fd_duplication_is_not_a_command_separator(prefix, command):
    """`2>&1 bash -c ...`: the `&` of `>&` is part of the redirect, so bash is still the command word."""
    plain = _verdicts(command)
    assert plain != (False, False)
    assert _verdicts(prefix + command) == plain


@pytest.mark.parametrize("command", ["ls 2>&1 && reboot", "ls >&2; reboot", "ls 2>&1 | true; reboot", "ls 2>&1 & reboot"])
def test_separator_after_fd_duplication_still_splits(command):
    assert _verdicts(command)[0]


def test_fd_duplication_does_not_flag_plain_commands():
    assert _verdicts("ls 2>&1") == (False, False)
    assert _verdicts("2>&1 ls -la") == (False, False)
    assert _verdicts("echo a 2>&1 bash -c 'reboot'") == (False, False)
