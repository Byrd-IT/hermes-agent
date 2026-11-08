"""Leading redirects / env assignments must not hide a command-position rule.

`>/dev/null reboot` runs `reboot` with stdout discarded, exactly like plain `reboot`. The
command-position anchor (`_CMDPOS`) used to treat the redirect as the command word, so the
hardline and dangerous rules that match only at command position saw nothing.
Invariant: a prefix of redirections and `NAME=value` assignments never changes the verdict.
"""

import time

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
    ">f>g ",
    "2>&1>f ",
    ">/dev/null 2>&1>f ",
    "FOO=1>f ",
    "<a>b ",
    "FOO=${X:-a b} ",
    ">${f:-a b} ",
    ">\"a\"b ",
    'FOO=a"b c" ',
    "FOO+=1 ",
    "A[0]=1 ",
]

_STARTS = ["{p}{c}", "echo hi; {p}{c}", "echo hi && {p}{c}", "true | {p}{c}", "$({p}{c})", "(\n{p}{c}\n)", "echo hi & {p}{c}"]


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
    assert _verdicts(template.format(p="{fd}>f ", c=command)) == plain


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


def test_long_prefix_runs_do_not_blow_up():
    """Long runs of prefixes (with a separator, under the parser limits) finish well inside a loose bound
    (catches exponential or badly polynomial backtracking, not a precise complexity class)."""
    for unit in (">f ", "A=1 ", "2>&1 ", "A=b\"c d\"e "):
        command = unit * 800 + "echo hi; " + unit * 800 + "reboot"
        started = time.monotonic()
        assert _verdicts(command)[0]
        assert time.monotonic() - started < 20


@pytest.mark.parametrize("command", [
    "echo \\>& reboot",
    "echo \\<& mkfs.ext4 /dev/sda1",
    "echo \\>& bash -c 'reboot'",
    "echo \\\\\\>& reboot",
])
def test_escaped_redirect_character_does_not_hide_next_command(command):
    assert _verdicts(command)[0]


@pytest.mark.parametrize("command", [
    "env FOO=$(x) reboot",
    "env FOO='a'b reboot",
    'env FOO=a"b" reboot',
    "env 1X=y reboot",
    "env X=<(y) reboot",
    ">\"a\"b reboot",
    '>a"b c" reboot',
    'FOO=a"b c" reboot',
    "FOO+=1 reboot",
    "A[0]=1 reboot",
])
def test_word_shapes_do_not_hide_command(command):
    assert _verdicts(command)[0]


def test_escaped_space_in_operand_fails_closed():
    """`FOO=a\\ reboot ls` runs `ls`; normalization drops the escape, so it is flagged (rare, safe side)."""
    assert _verdicts("FOO=a\\ reboot ls")[0]


@pytest.mark.parametrize("command", [
    "echo >''& bash -c 'reboot'",
    'echo >""& bash -c \'reboot\'',
    "echo \uff1e& bash -c 'reboot'",
    "echo \\>& python3 -c 'import os; os.system(1)'",
    "echo \\>& bash -c 'true'",
])
def test_normalization_fused_fd_duplication_keeps_the_separator(command):
    """Normalization (escape/quote removal, NFKC) can fuse a literal `>` with a real `&` separator."""
    assert _verdicts(command) != (False, False)


@pytest.mark.parametrize("command", ["A=1 halt=0 ls", "halt=0 ls", ">f reboot_count=2 ls", "FOO=1 poweroff=yes ls"])
def test_assignment_named_like_a_command_is_not_the_command(command):
    assert _verdicts(command) == (False, False)


def test_ampersand_before_redirect_stays_a_separator_fail_closed():
    """`echo hi &>f reboot`: bash passes `reboot` to echo, POSIX sh backgrounds `echo hi` and runs it."""
    assert _verdicts("echo hi &>f reboot")[0]


@pytest.mark.parametrize("command", [
    "bash -c 'echo \\>& reboot'",
    "bash -c \"echo >''& reboot\"",
    "bash -c 'echo \uff1e& reboot'",
    "bash -c \"echo >''& rm -rf /\"",
    "sh -c 'echo >\"\"& mkfs /dev/sda'",
    "bash -c \"bash -c 'echo \\>& reboot'\"",
])
def test_fused_fd_duplication_inside_payload_still_reaches_hardline(command):
    assert detect_hardline_command(command)[0]
