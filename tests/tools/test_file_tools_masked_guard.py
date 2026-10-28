"""The masked-credential patch guard refuses redactor output, not ordinary three-asterisk text."""

from unittest.mock import patch

import pytest

from agent.redact import redact_sensitive_text
from tools.file_tools import _contains_masked_secret_marker, patch_tool

S = "*" * 3     # what the redactor prints for a short secret
VAULT = "\u00abredacted-vault-secret\u00bb"

# Raw text the redactor masks. The guard is tested against the redactor's own output, so a shape the
# redactor learns to produce but the guard misses shows up here instead of in a written file.
RAW_SECRET_TEXT = [
    "Authorization: Bearer shortsecret1",
    "Authorization: Digest abc123def456",
    "DB_PASS=hunter2",
    "SMTP_PW=hunter2",
    "API_KEY=abc12345",
    "export OPENAI_API_KEY=abc12345",
    "password: hunter2",
    '{"api_key": "sk-shortkey1"}',
    'payload = "{\\"api_key\\": \\"sk-shortkey1\\"}"',
    "https://x.test/cb?sig=abcdef12345&code=zz9988",
    "https://deploy:hunter2@git.example/x",
]

HAND_WRITTEN = [
    f"API_KEY={S}",
    f'"token": "{S}"',
    f"Authorization: ApiKey {S}",
    f"bot12345678:{S}",
    f"hermes-session={S}",
    f"token = {VAULT}",
]

ORDINARY_TEXT = [
    S,                                    # Markdown horizontal rule
    f"  {S}  ",
    f"{S}bold italic{S}",                 # Markdown bold-italic
    f"char {S}table = NULL;",             # C pointer to pointer to pointer
    f"int {S}p, {S}q;",
    f"## Notes\n\n{S}\n\ntext",
    "multiply: a * b * c",
    "a ** b and ****** and ****",         # other asterisk runs
    f"auth\n{S}",                         # a word at line end, then a Markdown rule
]


@pytest.mark.parametrize("raw", RAW_SECRET_TEXT)
def test_the_redactors_own_output_is_refused(raw):
    masked = redact_sensitive_text(raw, force=True, redact_url_credentials=True)
    assert S in masked, f"the redactor no longer masks {raw!r} with the short mask"
    assert _contains_masked_secret_marker(masked)
    assert _contains_masked_secret_marker("harmless", masked)


@pytest.mark.parametrize("text", HAND_WRITTEN)
def test_other_mask_shapes_are_refused(text):
    assert _contains_masked_secret_marker(text)


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
