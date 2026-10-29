from dataclasses import asdict

import pytest
from pydantic import SecretStr

from yandex_mail_mcp.config import ConfigurationError, Settings


def test_settings_repr_does_not_expose_password():
    settings = Settings(email="test@yandex.com", app_password=SecretStr("SUPERSECRET"))

    assert "SUPERSECRET" not in repr(settings)
    assert "test@yandex.com" in repr(settings)
    assert "SUPERSECRET" not in str(settings)
    assert "SUPERSECRET" not in f"{settings}"
    assert "SUPERSECRET" not in str(asdict(settings))
    assert "SUPERSECRET" not in str(vars(settings))
    assert settings.app_password.get_secret_value() == "SUPERSECRET"


def test_invalid_port_identifies_variable(monkeypatch):
    monkeypatch.setenv("YANDEX_EMAIL", "a@example.com")
    monkeypatch.setenv("YANDEX_APP_PASSWORD", "SUPERSECRET")
    monkeypatch.setenv("YANDEX_IMAP_PORT", "bad")
    with pytest.raises(ValueError, match="YANDEX_IMAP_PORT"):
        Settings.from_env()


def test_blank_port_uses_default(monkeypatch):
    monkeypatch.setenv("YANDEX_EMAIL", "a@example.com")
    monkeypatch.setenv("YANDEX_APP_PASSWORD", "secret")
    monkeypatch.setenv("YANDEX_IMAP_PORT", "  ")
    assert Settings.from_env().imap_port == 993


def test_promotions_folder_is_stripped(monkeypatch):
    monkeypatch.setenv("YANDEX_EMAIL", "a@example.com")
    monkeypatch.setenv("YANDEX_APP_PASSWORD", "secret")
    monkeypatch.setenv("YANDEX_PROMOTIONS_FOLDER", " AI-Promotions ")
    assert Settings.from_env().promotions_folder == "AI-Promotions"


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_promotions_folder_is_rejected(monkeypatch, value):
    monkeypatch.setenv("YANDEX_EMAIL", "a@example.com")
    monkeypatch.setenv("YANDEX_APP_PASSWORD", "secret")
    monkeypatch.setenv("YANDEX_PROMOTIONS_FOLDER", value)
    with pytest.raises(ConfigurationError, match="YANDEX_PROMOTIONS_FOLDER"):
        Settings.from_env()


def test_imap_host_is_stripped(monkeypatch):
    monkeypatch.setenv("YANDEX_EMAIL", "a@example.com")
    monkeypatch.setenv("YANDEX_APP_PASSWORD", "secret")
    monkeypatch.setenv("YANDEX_IMAP_HOST", " imap.yandex.com ")
    assert Settings.from_env().imap_host == "imap.yandex.com"


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_imap_host_uses_default(monkeypatch, value):
    monkeypatch.setenv("YANDEX_EMAIL", "a@example.com")
    monkeypatch.setenv("YANDEX_APP_PASSWORD", "secret")
    monkeypatch.setenv("YANDEX_IMAP_HOST", value)
    assert Settings.from_env().imap_host == "imap.yandex.com"


@pytest.mark.parametrize("value", ["INBOX", " spam ", "Черновики"])
def test_system_promotions_folder_is_rejected_at_startup(monkeypatch, value):
    monkeypatch.setenv("YANDEX_EMAIL", "a@example.com")
    monkeypatch.setenv("YANDEX_APP_PASSWORD", "secret")
    monkeypatch.setenv("YANDEX_PROMOTIONS_FOLDER", value)
    with pytest.raises(ConfigurationError, match="YANDEX_PROMOTIONS_FOLDER"):
        Settings.from_env()


def test_pool_settings_from_env(monkeypatch):
    monkeypatch.setenv("YANDEX_EMAIL", "a@example.com")
    monkeypatch.setenv("YANDEX_APP_PASSWORD", "SUPERSECRET")
    monkeypatch.delenv("YANDEX_IMAP_POOL_SIZE", raising=False)
    monkeypatch.delenv("YANDEX_IMAP_IDLE_TIMEOUT", raising=False)
    settings = Settings.from_env()
    assert (settings.imap_pool_size, settings.imap_idle_timeout) == (2, 300)
    monkeypatch.setenv("YANDEX_IMAP_POOL_SIZE", "4")
    monkeypatch.setenv("YANDEX_IMAP_IDLE_TIMEOUT", "60")
    settings = Settings.from_env()
    assert (settings.imap_pool_size, settings.imap_idle_timeout) == (4, 60)


@pytest.mark.parametrize(
    ("name", "value"), [("YANDEX_IMAP_POOL_SIZE", "0"), ("YANDEX_IMAP_IDLE_TIMEOUT", "-1")]
)
def test_invalid_pool_settings_are_rejected(monkeypatch, name, value):
    monkeypatch.setenv("YANDEX_EMAIL", "a@example.com")
    monkeypatch.setenv("YANDEX_APP_PASSWORD", "SUPERSECRET")
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError, match=name):
        Settings.from_env()


def test_archive_and_confirmation_settings_from_env(monkeypatch):
    monkeypatch.setenv("YANDEX_EMAIL", "a@example.com")
    monkeypatch.setenv("YANDEX_APP_PASSWORD", "SUPERSECRET")
    monkeypatch.delenv("YANDEX_ARCHIVE_FOLDER", raising=False)
    monkeypatch.delenv("MCP_CONFIRM_REVERSIBLE", raising=False)
    settings = Settings.from_env()
    assert (settings.archive_folder, settings.confirm_reversible) == ("", True)
    monkeypatch.setenv("YANDEX_ARCHIVE_FOLDER", " Архив ")
    monkeypatch.setenv("MCP_CONFIRM_REVERSIBLE", "false")
    settings = Settings.from_env()
    assert (settings.archive_folder, settings.confirm_reversible) == ("Архив", False)


def test_blank_boolean_uses_default(monkeypatch):
    monkeypatch.setenv("YANDEX_EMAIL", "a@example.com")
    monkeypatch.setenv("YANDEX_APP_PASSWORD", "SUPERSECRET")
    monkeypatch.setenv("MCP_CONFIRM_REVERSIBLE", " ")
    monkeypatch.setenv("YANDEX_ALLOW_WRITE", "")
    settings = Settings.from_env()
    assert settings.confirm_reversible is True
    assert settings.allow_write is False


@pytest.mark.parametrize("value", ["0", "-1", "70000"])
def test_port_outside_the_valid_range_is_rejected(monkeypatch, value):
    monkeypatch.setenv("YANDEX_EMAIL", "a@example.com")
    monkeypatch.setenv("YANDEX_APP_PASSWORD", "secret")
    monkeypatch.setenv("YANDEX_IMAP_PORT", value)
    with pytest.raises(ConfigurationError, match="YANDEX_IMAP_PORT must be a port number"):
        Settings.from_env()
