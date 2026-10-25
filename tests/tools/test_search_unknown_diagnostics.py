"""Unknown rg/grep diagnostics degrade to partial results, not a silent fail.

When a search exits non-zero with usable stdout and stderr lines that match no
known unreadable-path form, the readable results are returned and the
unrecognised lines are surfaced in the warning (bounded, deduplicated,
credential-redacted). Genuine failures stay errors: invalid regex, usage
errors, a missing search tool, and a non-zero exit with no results.
"""

import pytest

from tools.file_operations import ShellFileOperations
from tools.file_operations_search import (
    _bounded_unknown_diagnostics,
    _redact_diagnostic_text,
)


class RecordingEnvironment:
    """Minimal env fake: canned rg stdout/exit, path exists."""

    is_local = False
    cwd = "/repo"

    def __init__(self, *, rg_output="", rg_code=0, has_rg=True, has_find=False, has_grep=False):
        self.commands = []
        self.rg_output = rg_output
        self.rg_code = rg_code
        self.has_rg = has_rg
        self.has_find = has_find
        self.has_grep = has_grep

    def execute(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("test -e "):
            return {"output": "exists\n", "returncode": 0}
        if command.startswith("command -v rg"):
            return {"output": "/usr/bin/rg\n" if self.has_rg else "", "returncode": 0 if self.has_rg else 1}
        if command.startswith("command -v find"):
            return {"output": "yes\n" if self.has_find else "", "returncode": 0 if self.has_find else 1}
        if command.startswith("command -v grep"):
            return {"output": "yes\n" if self.has_grep else "", "returncode": 0 if self.has_grep else 1}
        if "--version" in command:
            return {"output": "ripgrep 15.2.0\n", "returncode": 0}
        if "--files" in command or "--line-number" in command or "grep -rnHE" in command:
            return {"output": self.rg_output, "returncode": self.rg_code}
        return {"output": "", "returncode": 1}


UNKNOWN = "rg: /repo/weird: disk quota exceeded\n"
PARTIAL_FILE = "/repo/partial.py\n"
PARTIAL_MATCH = "/repo/a.py:3:needle\n"


@pytest.mark.parametrize("order", ["discovery", "modified"])
def test_file_search_unknown_diagnostic_returns_partial_results(order):
    env = RecordingEnvironment(rg_output=UNKNOWN + PARTIAL_FILE, rg_code=2)

    result = ShellFileOperations(env).search("*.py", path="/repo", target="files", order=order)

    assert result.error is None
    assert result.files == ["/repo/partial.py"]
    assert "disk quota exceeded" in (result.warning or "")
    assert "partial" in (result.warning or "").lower()


def test_file_search_unknown_lines_are_deduplicated():
    env = RecordingEnvironment(rg_output=UNKNOWN + UNKNOWN + PARTIAL_FILE, rg_code=2)

    result = ShellFileOperations(env).search("*.py", path="/repo", target="files")

    assert result.error is None
    assert (result.warning or "").count("disk quota exceeded") == 1


def test_file_search_mixes_known_unreadable_path_with_unknown_line():
    output = (
        "rg: /srv/locked: Permission denied (os error 13)\n"
        + UNKNOWN
        + PARTIAL_FILE
    )
    env = RecordingEnvironment(rg_output=output, rg_code=2)

    result = ShellFileOperations(env).search("*.py", path="/repo", target="files")

    assert result.error is None
    assert result.files == ["/repo/partial.py"]
    assert "/srv/locked" in (result.warning or "")
    assert "disk quota exceeded" in (result.warning or "")


def test_content_search_unknown_diagnostic_returns_partial_matches():
    env = RecordingEnvironment(rg_output=UNKNOWN + PARTIAL_MATCH, rg_code=2)

    result = ShellFileOperations(env).search("needle", path="/repo", target="content")

    assert result.error is None
    assert [(m.path, m.line_number, m.content) for m in result.matches] == [
        ("/repo/a.py", 3, "needle")]
    assert "disk quota exceeded" in (result.warning or "")


def test_file_search_unknown_diagnostic_with_no_results_still_fails():
    env = RecordingEnvironment(rg_output=UNKNOWN, rg_code=2)

    result = ShellFileOperations(env).search("*.py", path="/repo", target="files")

    assert result.error is not None
    assert result.files == []
    assert "disk quota exceeded" in result.error


def test_content_search_unknown_diagnostic_with_no_results_still_fails():
    env = RecordingEnvironment(rg_output=UNKNOWN, rg_code=2)

    result = ShellFileOperations(env).search("needle", path="/repo", target="content")

    assert result.error is not None
    assert result.matches == []
    assert "disk quota exceeded" in result.error


@pytest.mark.parametrize("diagnostic", [
    "rg: regex parse error:\n    (?:[\n       ^\nerror: unclosed character class\n",
    "rg: unrecognized flag --not-a-flag\n",
    "rg: error parsing flag --sortr: choice 'bogus' is unrecognized\n",
])
def test_file_search_usage_and_regex_errors_still_fail_with_partial_stdout(diagnostic):
    env = RecordingEnvironment(rg_output=diagnostic + PARTIAL_FILE, rg_code=2)

    result = ShellFileOperations(env).search("*.py", path="/repo", target="files")

    assert result.error is not None
    assert result.files == []
    assert result.warning is None


@pytest.mark.parametrize("diagnostic", [
    "rg: regex parse error:\n    (?:[\n       ^\nerror: unclosed character class\n",
    "rg: unrecognized flag --not-a-flag\n",
])
def test_content_search_usage_and_regex_errors_still_fail_with_partial_stdout(diagnostic):
    env = RecordingEnvironment(rg_output=diagnostic + PARTIAL_MATCH, rg_code=2)

    result = ShellFileOperations(env).search("needle", path="/repo", target="content")

    assert result.error is not None
    assert not result.matches
    assert result.warning is None


def test_content_search_grep_unmatched_still_fails():
    env = RecordingEnvironment(
        rg_output="grep: Unmatched ( or \\(\n" + PARTIAL_MATCH,
        rg_code=2,
        has_rg=False,
        has_grep=True,
    )

    result = ShellFileOperations(env).search("(", path="/repo", target="content")

    assert result.error is not None
    assert "Unmatched" in result.error
    assert not result.matches


def test_file_search_without_rg_or_find_still_errors():
    env = RecordingEnvironment(has_rg=False, has_find=False)

    result = ShellFileOperations(env).search("*.py", path="/repo", target="files")

    assert result.error is not None
    assert "ripgrep" in result.error.lower() or "rg" in result.error
    assert result.files == []


def test_content_search_without_rg_or_grep_still_errors():
    env = RecordingEnvironment(has_rg=False, has_grep=False)

    result = ShellFileOperations(env).search("needle", path="/repo", target="content")

    assert result.error is not None
    assert "ripgrep" in result.error.lower() or "grep" in result.error


def test_surfaced_diagnostics_drop_credentials():
    bearer = "sk-synthbearer0123456789ABCDEF"
    password = "SynthPwsecret99ABCDEF"
    token = "ghp_SYNTHtoken0123456789ABCDEF"
    header_key = "SYNTHhdrkey0123456789ABCDEF"
    query_key = "AKSYNTHq0123456789ABCDEF"
    line = (
        "rg: note Authorization: Bearer " + bearer
        + " https://synthuser:" + password + "@probe.example.invalid/api"
        + " X-Api-Key: " + header_key
        + " ?api_key=" + query_key
        + " token " + token
    )
    redacted = _redact_diagnostic_text(line)
    for secret in (bearer, password, token, header_key, query_key):
        assert secret not in redacted
        assert secret[:8] not in redacted
        assert secret[-6:] not in redacted
    assert "[REDACTED]" in redacted
    assert "probe.example.invalid" in redacted


def test_unknown_diagnostic_warning_is_bounded_and_deduplicated():
    lines = [f"rg: /repo/p{i}: something went wrong in a very long diagnostic " + ("x" * 80) for i in range(12)]
    lines.append(lines[0])
    bounded = _bounded_unknown_diagnostics(lines)
    assert len(bounded) < 400
    assert "(+" in bounded and "more" in bounded
    assert bounded.count("something went wrong") <= 5


# A bad pattern inside a nested ignore file is a non-fatal warning. The line
# carries ``error parsing glob`` but is prefixed by ``<path>: line N:``.
# rg 15 exits 0; rg 14 exits 2 and still prints the matches.
IGNORE_FILE_GLOB = (
    "rg: /repo/sub/.gitignore: line 1: error parsing glob 'a{b': "
    "unclosed character class; missing ']'\n"
)


@pytest.mark.parametrize("code", [0, 2])
def test_file_search_ignore_file_glob_warning_keeps_partial_results(code):
    env = RecordingEnvironment(rg_output=IGNORE_FILE_GLOB + PARTIAL_FILE, rg_code=code)

    result = ShellFileOperations(env).search("*.py", path="/repo", target="files")

    assert result.error is None
    assert result.files == ["/repo/partial.py"]
    assert "error parsing glob" in (result.warning or "")


@pytest.mark.parametrize("code", [0, 2])
def test_content_search_ignore_file_glob_warning_keeps_partial_results(code):
    env = RecordingEnvironment(rg_output=IGNORE_FILE_GLOB + PARTIAL_MATCH, rg_code=code)

    result = ShellFileOperations(env).search("needle", path="/repo", target="content")

    assert result.error is None
    assert [(m.path, m.line_number, m.content) for m in result.matches] == [
        ("/repo/a.py", 3, "needle")]
    assert "error parsing glob" in (result.warning or "")


@pytest.mark.parametrize("path_name", ["error parsing", "Unmatched"])
def test_file_search_permission_denied_path_with_fatal_keyword_recovers(path_name):
    output = f"rg: /repo/{path_name}: Permission denied (os error 13)\n" + PARTIAL_FILE
    env = RecordingEnvironment(rg_output=output, rg_code=2)

    result = ShellFileOperations(env).search("*.py", path="/repo", target="files")

    assert result.error is None
    assert result.files == ["/repo/partial.py"]
    assert path_name in (result.warning or "")


@pytest.mark.parametrize("path_name", ["error parsing", "Unmatched"])
def test_content_search_permission_denied_path_with_fatal_keyword_recovers(path_name):
    output = f"rg: /repo/{path_name}: Permission denied (os error 13)\n" + PARTIAL_MATCH
    env = RecordingEnvironment(rg_output=output, rg_code=2)

    result = ShellFileOperations(env).search("needle", path="/repo", target="content")

    assert result.error is None
    assert [(m.path, m.line_number, m.content) for m in result.matches] == [
        ("/repo/a.py", 3, "needle")]
    assert path_name in (result.warning or "")


def test_permission_denied_line_shaped_like_glob_error_is_not_fatal():
    # The path itself is the invocation-level token. The unreadable-path
    # matcher must win, or anchoring the fatal regex is not enough.
    output = "rg: error parsing glob: Permission denied (os error 13)\n" + PARTIAL_FILE
    env = RecordingEnvironment(rg_output=output, rg_code=2)

    result = ShellFileOperations(env).search("*.py", path="/repo", target="files")

    assert result.error is None
    assert result.files == ["/repo/partial.py"]


def test_file_search_multiline_unknown_error_with_no_real_files_is_an_error():
    env = RecordingEnvironment(
        rg_output="rg: some new failure:\n    continuation detail\n",
        rg_code=2,
    )

    result = ShellFileOperations(env).search("*.py", path="/repo", target="files")

    assert result.error is not None
    assert result.files == []
    assert "some new failure" in result.error
    assert "continuation detail" not in result.files


def test_file_search_continuation_line_is_not_counted_as_a_file():
    output = "rg: some new failure:\n    continuation detail\n" + PARTIAL_FILE
    env = RecordingEnvironment(rg_output=output, rg_code=2)

    result = ShellFileOperations(env).search("*.py", path="/repo", target="files")

    assert result.error is None
    assert result.files == ["/repo/partial.py"]
    assert "continuation detail" in (result.warning or "")


@pytest.mark.parametrize("target,pattern,payload", [
    ("files", "*.py", PARTIAL_FILE),
    ("content", "needle", PARTIAL_MATCH),
])
def test_pathless_glob_error_still_fails_with_partial_stdout(target, pattern, payload):
    env = RecordingEnvironment(
        rg_output="rg: error parsing glob '[': unclosed character class\n" + payload,
        rg_code=2,
    )

    result = ShellFileOperations(env).search(pattern, path="/repo", target=target)

    assert result.error is not None
    assert not result.files
    assert not result.matches
    assert result.warning is None


def test_file_search_continuation_redacts_credentials_before_display():
    bearer = "sk-" + ("n" * 12) + "ABCDEF"
    password = "SynthPw" + "secret99" + "ABCDEF"
    token = "ghp_" + ("q" * 20) + "ABCDEF"
    header_key = "SYNTHhdr" + "key0123456789" + "ABCDEF"
    query_key = "AKSYNTH" + "q0123456789" + "ABCDEF"
    blob = (
        "Authorization: Bearer " + bearer
        + " https://synthuser:" + password + "@probe.example.invalid/api"
        + " X-Api-Key: " + header_key
        + " ?api_key=" + query_key
        + " token=" + token
    )
    output = "rg: some new failure:\n    continuation " + blob + "\n" + PARTIAL_FILE
    result = ShellFileOperations(
        RecordingEnvironment(rg_output=output, rg_code=2)
    ).search("*.py", path="/repo", target="files")

    shown = (result.warning or "") + (result.error or "")
    for secret in (bearer, password, token, header_key, query_key):
        assert secret not in shown
        assert secret[:8] not in shown
        assert secret[-6:] not in shown
    assert "[REDACTED]" in shown
    assert result.error is None
    assert result.files == ["/repo/partial.py"]


def test_exit_zero_does_not_fail_closed_on_a_fatal_looking_line():
    env = RecordingEnvironment(
        rg_output="rg: regex parse error:\n" + PARTIAL_FILE,
        rg_code=0,
    )

    result = ShellFileOperations(env).search("*.py", path="/repo", target="files")

    assert result.error is None
    assert result.files == ["/repo/partial.py"]
