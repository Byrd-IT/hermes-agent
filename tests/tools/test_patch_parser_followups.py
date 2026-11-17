"""V4A ambiguity check counts whole-line matches, as the cursor seek accepts them (Byrd-IT carry)."""

from types import SimpleNamespace

from tools.patch_parser import _seek_hunk, apply_v4a_operations, parse_v4a_patch


class _Files:
    def __init__(self, files):
        self.files = dict(files)

    def read_file_raw(self, path):
        if path in self.files:
            return SimpleNamespace(content=self.files[path], error=None)
        return SimpleNamespace(content="", error="file not found", not_found=True)

    def write_file(self, path, content, pre_content=None):
        self.files[path] = content
        return SimpleNamespace(error=None)


_PATCH = (
    "*** Begin Patch\n*** Update File: a.py\n@@\n-start\n+START\n@@\n-x\n+X\n*** End Patch\n"
)


def _apply(original):
    ops, err = parse_v4a_patch(_PATCH)
    assert err is None
    files = _Files({"a.py": original})
    return apply_v4a_operations(ops, files), files


def test_substring_inside_another_line_is_not_a_second_block():
    """``x`` also occurs inside 'tail x tail'; only one WHOLE line matches, so hunk 2 is unambiguous."""
    result, files = _apply("start\nmark\nx\ntail x tail\n")
    assert result.success is True, result.error
    assert files.files["a.py"] == "START\nmark\nX\ntail x tail\n"


def test_two_whole_line_copies_after_the_cursor_still_fail_closed():
    result, files = _apply("start\nx\nmid\nx\n")
    assert result.success is False
    assert "ambiguous after the previous hunk" in (result.error or "")
    assert files.files["a.py"] == "start\nx\nmid\nx\n"


def test_whitespace_variant_copies_count_as_ambiguous():
    """The seek accepts a stripped match, so two indented copies are two candidate blocks."""
    result, _files = _apply("start\n  x\nmid\n    x\n")
    assert result.success is False


def test_seek_falls_through_exact_rstrip_strip_in_order():
    lines = ["a", "b  ", "  c", "d"]
    content = "\n".join(lines)
    assert _seek_hunk(content, ["b"], 0) == (2, 5)          # rstrip match
    assert _seek_hunk(content, ["c"], 0) == (6, 9)          # strip match
    assert _seek_hunk(content, ["a", "b  "], 0) == (0, 5)   # exact multi-line
    assert _seek_hunk(content, ["zzz"], 0) is None


def test_a_later_hunk_starting_with_a_blank_context_line_is_not_double_counted():
    """The cursor sits at the end of the previous hunk's line; that line's empty tail is not a
    candidate for the seek, so it must not make a unique blank-led block look ambiguous."""
    patch = (
        "*** Begin Patch\n*** Update File: a.py\n@@\n-start\n+START\n@@\n \n-x\n+X\n*** End Patch\n"
    )
    ops, err = parse_v4a_patch(patch)
    assert err is None
    files = _Files({"a.py": "start\nx\n\nx\n"})
    result = apply_v4a_operations(ops, files)
    assert result.success is True, result.error
    assert files.files["a.py"] == "START\nx\n\nX\n"
