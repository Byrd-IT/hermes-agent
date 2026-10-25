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
