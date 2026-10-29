"""Configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from pydantic import SecretStr

# Names a promotions folder must never take, compared case-insensitively.
RESERVED_FOLDER_NAMES = frozenset(
    {
        "inbox",
        "trash",
        "spam",
        "deleted",
        "junk",
        "drafts",
        "sent",
        "outbox",
        "спам",
        "удалённые",
        "удаленные",
        "черновики",
        "отправленные",
        "исходящие",
    }
)


class ConfigurationError(ValueError):
    """An actionable configuration error."""


def env_bool(name: str, default: bool = False) -> bool:
    # A blank value, as in "NAME=" or an unset Compose substitution, means the default.
    value = (os.getenv(name) or "").strip().lower()
    return default if not value else value in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = (os.getenv(name) or "").strip() or str(default)
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


@dataclass(frozen=True)
class Settings:
    email: str
    app_password: SecretStr = field(repr=False)
    imap_host: str = "imap.yandex.com"
    imap_port: int = 993
    allow_write: bool = False
    promotions_folder: str = "AI-Promotions"
    imap_pool_size: int = 2
    imap_idle_timeout: int = 300
    archive_folder: str = ""
    confirm_reversible: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.app_password, str):
            object.__setattr__(self, "app_password", SecretStr(self.app_password))

    @classmethod
    def from_env(cls) -> Settings:
        email = os.getenv("YANDEX_EMAIL", "").strip()
        password = SecretStr(os.getenv("YANDEX_APP_PASSWORD", "").strip())
        if not email or not password.get_secret_value():
            raise ConfigurationError(
                "Set YANDEX_EMAIL and YANDEX_APP_PASSWORD in the environment (see .env.example)"
            )
        promotions_folder = os.getenv("YANDEX_PROMOTIONS_FOLDER", "AI-Promotions").strip()
        if not promotions_folder:
            raise ConfigurationError("YANDEX_PROMOTIONS_FOLDER cannot be empty")
        if promotions_folder.casefold() in RESERVED_FOLDER_NAMES:
            raise ConfigurationError(
                "YANDEX_PROMOTIONS_FOLDER must name a dedicated folder, not a system folder"
            )
        pool_size = env_int("YANDEX_IMAP_POOL_SIZE", 2)
        if pool_size < 1:
            raise ConfigurationError("YANDEX_IMAP_POOL_SIZE must be at least 1")
        idle_timeout = env_int("YANDEX_IMAP_IDLE_TIMEOUT", 300)
        if idle_timeout < 0:
            raise ConfigurationError("YANDEX_IMAP_IDLE_TIMEOUT cannot be negative")
        return cls(
            email=email,
            app_password=password,
            imap_host=(os.getenv("YANDEX_IMAP_HOST") or "").strip() or "imap.yandex.com",
            imap_port=env_int("YANDEX_IMAP_PORT", 993),
            allow_write=env_bool("YANDEX_ALLOW_WRITE"),
            promotions_folder=promotions_folder,
            imap_pool_size=pool_size,
            imap_idle_timeout=idle_timeout,
            archive_folder=os.getenv("YANDEX_ARCHIVE_FOLDER", "").strip(),
            confirm_reversible=env_bool("MCP_CONFIRM_REVERSIBLE", True),
        )
