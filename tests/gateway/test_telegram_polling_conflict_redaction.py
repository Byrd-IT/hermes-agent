"""Polling conflict diagnostics must not reveal credential fragments."""
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter, _redact_telegram_error_text


@pytest.mark.parametrize("prefix,body", [
    ("sk-", "abCD1234567890SecretEnd"),
    ("ghp_", "abCD1234567890SecretEnd"),
    ("AKIA", "1234567890ABCDEF"),
    ("eyJ", "abCD1234567890SecretEnd"),
])
def test_bare_token_body_fully_hidden(prefix, body):
    result = _redact_telegram_error_text(RuntimeError("transport " + prefix + body))
    assert body not in result
    assert body[-4:] not in result


@pytest.mark.parametrize("text,secret", [
    ("Authorization: Bearer syntheticBearerValue9876", "syntheticBearerValue9876"),
    ("X-API-Key: syntheticHeaderValue9876", "syntheticHeaderValue9876"),
    ("request https://proxyuser:proxysecret@example.test/?api_key=syntheticQueryValue9876", "syntheticQueryValue9876"),
])
def test_diagnostic_credentials_hidden_without_suffix(text, secret):
    result = _redact_telegram_error_text(RuntimeError(text))
    assert secret not in result
    assert secret[-4:] not in result


@pytest.mark.asyncio
async def test_conflict_bare_token_fully_masked_in_log(caplog):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake", extra={}))
    adapter._ptb_proxy_url = None
    adapter._stop_updater_or_go_fatal = AsyncMock(return_value=False)
    body = "abCD1234567890SecretEnd"
    with caplog.at_level("WARNING"):
        await adapter._handle_polling_conflict(RuntimeError("transport " + "ghp_" + body))
    text = " ".join(record.getMessage() for record in caplog.records)
    assert "polling conflict" in text
    assert body[-4:] not in text


@pytest.mark.asyncio
async def test_conflict_does_not_log_proxy_credentials(caplog):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake", extra={}))
    adapter._ptb_proxy_url = "http://proxyuser:proxysecret@proxy.example:8080"
    adapter._stop_updater_or_go_fatal = AsyncMock(return_value=False)
    with caplog.at_level("WARNING"):
        await adapter._handle_polling_conflict(RuntimeError("invalid proxyuser and proxysecret"))
    text = " ".join(record.getMessage() for record in caplog.records)
    assert "polling conflict" in text
    assert "proxyuser" not in text
    assert "proxysecret" not in text
