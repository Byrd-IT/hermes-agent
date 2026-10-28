"""V4A cursor-aware hunk seeking and fail-closed ambiguity (Byrd-IT fork carry).

Ported from fork commits fd0fc15155, 0a3792a5ea and 04701b8be4.
"""

from types import SimpleNamespace

from tools.patch_parser import apply_v4a_operations, parse_v4a_patch


class _DictFileOps:
    """In-memory file_ops backing store supporting update/move/delete/add."""

    def __init__(self, files):
        self.files = dict(files)

    def read_file_raw(self, path):
        if path in self.files:
            return SimpleNamespace(content=self.files[path], error=None)
        return SimpleNamespace(content="", error="file not found", not_found=True)

    def write_file(self, path, content, pre_content=None):
        self.files[path] = content
        return SimpleNamespace(error=None)

    def move_file(self, src, dst):
        self.files[dst] = self.files.pop(src)
        return SimpleNamespace(error=None)

    def delete_file(self, path):
        self.files.pop(path, None)
        return SimpleNamespace(error=None)


class TestCursorSeekBoilerplate:
    def test_validation_and_apply_seek_boilerplate_hunks_from_context_hints(self):
        """Later boilerplate hunks seek from inert anchors, then fall back when
        the intended target is before the monotonic cursor."""
        patch = """\
*** Begin Patch
*** Update File: adapter_test.py
@@ class TestSessions @@
 class TestSessions:
@@
-    self.assertEqual(resp.status_code, 200)
+    self.assertEqual(resp.status_code, 202)
@@ class TestAuth @@
 class TestAuth:
@@
-    self.assertEqual(resp.status_code, 200)
+    self.assertEqual(resp.status_code, 201)
*** End Patch"""
        ops, err = parse_v4a_patch(patch)
        assert err is None

        original = (
            "class TestPartials:\n"
            "    self.assertEqual(resp.status_code, 200)\n\n"
            "class TestSessions:\n"
            "    pass\n\n"
            "class TestBounds:\n"
            "    self.assertEqual(resp.status_code, 200)\n\n"
            "class TestAuth:\n"
            "    pass\n"
        )
        file_ops = _DictFileOps({"adapter_test.py": original})

        result = apply_v4a_operations(ops, file_ops)

        assert result.success is True, result.error
        assert file_ops.files["adapter_test.py"] == original.replace(
            "class TestPartials:\n    self.assertEqual(resp.status_code, 200)",
            "class TestPartials:\n    self.assertEqual(resp.status_code, 201)",
        ).replace(
            "class TestBounds:\n    self.assertEqual(resp.status_code, 200)",
            "class TestBounds:\n    self.assertEqual(resp.status_code, 202)",
        )

    def test_v4a_ambiguity_error_recommends_hunk_context_not_replace_all(self):
        patch = """\
*** Begin Patch
*** Update File: duplicate.py
@@
-    self.assertEqual(resp.status_code, 200)
+    self.assertEqual(resp.status_code, 201)
*** End Patch"""
        ops, err = parse_v4a_patch(patch)
        assert err is None
        file_ops = _DictFileOps({
            "duplicate.py": (
                "    self.assertEqual(resp.status_code, 200)\n"
                "    self.assertEqual(resp.status_code, 200)\n"
            )
        })

        result = apply_v4a_operations(ops, file_ops)

        assert result.success is False
        assert "replace_all" not in result.error
        assert "unique context lines" in (result.error or "")


class TestSafeCursorHunkSelection:
    """Regression coverage for dense repeated V4A search text."""

    def test_first_broad_hunk_seeks_from_top_of_file(self):
        patch = (
            "*** Begin Patch\n"
            "*** Update File: providers.py\n"
            "@@\n"
            "-    def available() -> bool:\n"
            "-        return True\n"
            "+    def available() -> bool:\n"
            "+        return has_openai_key()\n"
            "@@ Edge marker @@\n"
            "-edge_flag = False\n"
            "+edge_flag = True\n"
            "*** End Patch\n"
        )
        ops, err = parse_v4a_patch(patch)
        assert err is None
        original = (
            "class OpenAI:\n"
            "    def available() -> bool:\n"
            "        return True\n\n"
            "class Edge:\n"
            "    def available() -> bool:\n"
            "        return True\n"
            "edge_flag = False\n"
        )
        fo = _DictFileOps({"providers.py": original})

        result = apply_v4a_operations(ops, fo)

        assert result.success is True, result.error
        assert fo.files["providers.py"] == original.replace(
            "        return True", "        return has_openai_key()", 1
        ).replace("edge_flag = False", "edge_flag = True")

    def test_dense_hint_ambiguity_recommends_unique_context_not_another_hint(self):
        from tools.patch_parser import _v4a_match_error

        error = _v4a_match_error(
            "Found 5 matches. Provide more context to make it unique, or use replace_all=True.",
            hint_window_ambiguous=True,
        )

        assert "include unique context lines" in (error or "").lower()
        assert "add a unique @@ hint @@" not in (error or "").lower()

    def test_duplicate_hint_window_rejects_atomically_after_preceding_hunk(self):
        """A duplicate fallback hint must not select its first window silently."""
        patch = (
            "*** Begin Patch\n"
            "*** Update File: providers.py\n"
            "@@ prologue @@\n"
            "-prologue = old\n"
            "+prologue = new\n"
            "@@ duplicated hint @@\n"
            "-value =  old\n"
            "+value = new\n"
            "*** End Patch\n"
        )
        ops, err = parse_v4a_patch(patch)
        assert err is None
        original = (
            "prologue = old\n"
            "duplicated hint\n"
            "value = old\n"
            + ("padding\n" * 400)
            + "duplicated hint\n"
            "value = old\n"
        )
        fo = _DictFileOps({"providers.py": original})

        result = apply_v4a_operations(ops, fo)

        assert result.success is False
        assert fo.files["providers.py"] == original
        assert "context hint 'duplicated hint' is ambiguous" in (result.error or "")

    def test_later_broad_hunk_with_remaining_duplicates_rejects_atomically(self):
        patch = (
            "*** Begin Patch\n"
            "*** Update File: providers.py\n"
            "@@\n"
            " class OpenAI:\n"
            "-    def available() -> bool:\n"
            "+    def available() -> bool:\n"
            "+        return has_openai_key()\n"
            "@@\n"
            "-    def available() -> bool:\n"
            "+    def available() -> bool:\n"
            "+        return has_minimax_key()\n"
            "*** End Patch\n"
        )
        ops, err = parse_v4a_patch(patch)
        assert err is None
        original = (
            "class OpenAI:\n    def available() -> bool:\n        return True\n\n"
            "class Edge:\n    def available() -> bool:\n        return True\n\n"
            "class MiniMax:\n    def available() -> bool:\n        return True\n"
        )
        fo = _DictFileOps({"providers.py": original})

        result = apply_v4a_operations(ops, fo)

        assert result.success is False
        assert fo.files["providers.py"] == original
        assert "hunk 2" in (result.error or "").lower()

    def test_repeated_later_hunks_apply_successive_duplicate_blocks(self):
        """An explicit sequence of identical later hunks means successive sites.

        A single broad later hunk stays fail-closed; two or more identical hunks
        explicitly encode the cursor-ordered sequence required by the V4A repro.
        """
        patch = (
            "*** Begin Patch\n"
            "*** Update File: providers.py\n"
            "@@ anchor @@\n"
            "-anchor = old\n"
            "+anchor = new\n"
            "@@\n"
            "-value = old\n"
            "+value = first\n"
            "@@\n"
            "-value = old\n"
            "+value = second\n"
            "*** End Patch\n"
        )
        ops, err = parse_v4a_patch(patch)
        assert err is None
        original = "anchor = old\nvalue = old\nvalue = old\nvalue = old\n"
        fo = _DictFileOps({"providers.py": original})

        result = apply_v4a_operations(ops, fo)

        assert result.success is True, result.error
        assert fo.files["providers.py"] == (
            "anchor = new\nvalue = first\nvalue = second\nvalue = old\n"
        )

    def test_already_applied_later_hunk_does_not_hide_remaining_source_match(self, monkeypatch):
        from tools import fuzzy_match

        patch = (
            "*** Begin Patch\n"
            "*** Update File: providers.py\n"
            "@@ first @@\n"
            "-first = old\n"
            "+first = new\n"
            "@@ second @@\n"
            "-value = old\n"
            "+value = new\n"
            "*** End Patch\n"
        )
        ops, err = parse_v4a_patch(patch)
        assert err is None
        original = "first = old\nvalue = new\nvalue = old\n"
        fo = _DictFileOps({"providers.py": original})

        real_replace = fuzzy_match.fuzzy_find_and_replace

        def return_no_change_when_replacement_is_already_present(
                content, search_pattern, replacement, replace_all=False, **kwargs):
            if search_pattern == "value = old":
                return content, 0, "exact", None
            return real_replace(content, search_pattern, replacement, replace_all=replace_all, **kwargs)

        monkeypatch.setattr(
            fuzzy_match, "fuzzy_find_and_replace",
            return_no_change_when_replacement_is_already_present,
        )

        result = apply_v4a_operations(ops, fo)

        assert result.success is False
        assert fo.files["providers.py"] == original
        assert "hunk 2" in (result.error or "").lower()
