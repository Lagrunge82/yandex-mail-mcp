"""Configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from pydantic import SecretStr


class ConfigurationError(ValueError):
    """An actionable configuration error."""


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


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

    def __post_init__(self) -> None:
        if isinstance(self.app_password, str):
            object.__setattr__(self, "app_password", SecretStr(self.app_password))

    @classmethod
    def from_env(cls) -> "Settings":
        email = os.getenv("YANDEX_EMAIL", "").strip()
        password = SecretStr(os.getenv("YANDEX_APP_PASSWORD", "").strip())
        if not email or not password.get_secret_value():
            raise ConfigurationError(
                "Set YANDEX_EMAIL and YANDEX_APP_PASSWORD in the environment (see .env.example)"
            )
        return cls(
            email=email,
            app_password=password,
            imap_host=os.getenv("YANDEX_IMAP_HOST", "imap.yandex.com"),
            imap_port=env_int("YANDEX_IMAP_PORT", 993),
            allow_write=env_bool("YANDEX_ALLOW_WRITE"),
            promotions_folder=os.getenv("YANDEX_PROMOTIONS_FOLDER", "AI-Promotions"),
        )
