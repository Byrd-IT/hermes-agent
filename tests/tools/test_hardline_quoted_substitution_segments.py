"""A double-quoted command substitution is its own quote context for top-level segmentation.

In bash, ``"$(...)"`` starts a fresh quote context: ``echo "$(grep -ciE "PG::|x" f)"`` is one
command whose grep pattern is ``PG::|x``. The top-level segmenter used to toggle quote state on
the inner quotes, so the pattern's ``|`` looked like a top-level pipe, grep was cut into a
fragment with an unterminated quote, and the whole read-only line hit the unconditional
"command parser limit or malformed executable payload" block (ops card t_cc7d7bfd: a Zammad
``docker logs | grep -ciE "PG::|ConnectionBad|Redis::"`` health one-liner).
"""
import pytest

from tools.approval_detection import detect_dangerous_command, detect_hardline_command

_REPORTED = (
    'ls ~/.hermes/secrets/ | grep -i zammad; curl -s -o /dev/null -w "root=%{http_code}\\n" '
    'http://127.0.0.1:8085/; for c in railsserver scheduler websocket; do echo "$c: $(docker logs '
    '--since 2m zammad-zammad-$c-1 2>&1 | grep -ciE "PG::|ConnectionBad|Redis::|refused|closed the '
    'connection")"; done; docker logs --since 1m zammad-zammad-railsserver-1 2>&1 | grep -iE '
    '"PG::|ConnectionBad|Redis::" | tail -3'
)


@pytest.mark.parametrize("command", [
    _REPORTED,
    'echo "$(grep -ciE "a|b" f)"',
    'echo "$(cat f | grep "a|b")"',
    'echo "n=$(docker logs x 2>&1 | grep -c "a;b&c")"',
    'echo "`grep -c "a|b" f`"',
])
def test_quoted_substitution_grep_pattern_is_not_malformed(command):
    assert detect_hardline_command(command) == (False, None)
    assert detect_dangerous_command(command)[0] is False


@pytest.mark.parametrize(("command", "description"), [
    ('echo "$(grep -c "a|b" f; reboot)"', "system shutdown/reboot"),
    ('echo "$(grep -c "a|b" f)"; reboot', "system shutdown/reboot"),
    ('echo "$(rm -rf --no-preserve-root /)"', "recursive delete of root filesystem"),
    ('echo "$(echo "x" && rm -rf ~)"', "recursive delete of home directory"),
    ('echo "$(grep \'unterminated)"', "command parser limit or malformed executable payload"),
])
def test_hardline_commands_inside_or_after_a_quoted_substitution_still_block(command, description):
    assert detect_hardline_command(command) == (True, description)


def test_interpreter_payload_inside_quoted_substitution_still_needs_approval():
    # Fail-closed: the interpreter scan still flags it (description may be the generic one).
    is_dangerous, _, _ = detect_dangerous_command('echo "$(python3 -c "print(1)")"')
    assert is_dangerous
    assert detect_hardline_command('echo "$(python3 -c "print(1)")"') == (False, None)
