"""The masked-credential patch guard refuses redactor output, not ordinary three-asterisk text."""

from unittest.mock import patch

import pytest

from tools.file_tools import _contains_masked_secret_marker, patch_tool

S = "*" * 3     # what the redactor prints for a short secret
VAULT = "\u00abredacted-vault-secret\u00bb"

MASKED_SHAPES = [
    f"API_KEY={S}",
    f'"token": "{S}"',
    f"Authorization: ApiKey {S}",
    f"Authorization: Bearer {S}",
    f"password = {S}",
    f"https://user:{S}@host/path",
    f"export OPENAI_API_KEY={S}",
    f"token = {VAULT}",
]

ORDINARY_TEXT = [
    S,                                    # Markdown horizontal rule
    f"{S}bold italic{S}",                 # Markdown bold-italic
    f"char {S}table = NULL;",             # C pointer to pointer to pointer
    f"int {S}p, {S}q;",
    f"## Notes\n\n{S}\n\ntext",
    "multiply: a * b * c",
    f"auth\n{S}",                         # a word at line end, then a Markdown rule
]


@pytest.mark.parametrize("text", MASKED_SHAPES)
def test_redactor_shapes_are_still_refused(text):
    assert _contains_masked_secret_marker(text)
    assert _contains_masked_secret_marker("harmless", text)


@pytest.mark.parametrize("text", ORDINARY_TEXT)
def test_ordinary_three_asterisk_text_is_not_refused(text):
    assert not _contains_masked_secret_marker(text)
    assert not _contains_masked_secret_marker(text, text)


@patch("tools.file_tools._get_file_ops")
def test_replace_of_a_markdown_rule_reaches_the_file_layer(mock_get):
    patch_tool(mode="replace", path="/tmp/notes.md", old_string="---", new_string=S)
    mock_get.assert_called()


@patch("tools.file_tools._get_file_ops")
def test_replace_with_a_masked_credential_is_refused_before_any_file_access(mock_get):
    result = patch_tool(mode="replace", path="/tmp/c.env", old_string="API_KEY=real", new_string=f"API_KEY={S}")
    assert "masked credential" in result.lower()
    mock_get.assert_not_called()


@patch("tools.file_tools._get_file_ops")
def test_v4a_hunk_with_a_markdown_rule_is_not_refused(mock_get):
    patch_tool(
        mode="patch",
        patch=(
            "*** Begin Patch\n*** Update File: /tmp/notes.md\n@@\n"
            f"-old\n+{S}\n*** End Patch\n"
        ),
    )
    mock_get.assert_called()
