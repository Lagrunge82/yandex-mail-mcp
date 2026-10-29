"""Configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from pydantic import SecretStr

from .credentials import TokenStore

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


def env_port(name: str, default: int) -> int:
    port = env_int(name, default)
    if not 1 <= port <= 65535:
        raise ConfigurationError(f"{name} must be a port number from 1 to 65535")
    return port


@dataclass(frozen=True)
class Settings:
    """Server configuration.

    IMAP credentials come from one source, in priority order: oauth_token
    (YANDEX_OAUTH_TOKEN) or stored_tokens (saved by `yandex-mail-mcp login`).
    """

    email: str = ""
    oauth_token: SecretStr | None = field(default=None, repr=False)
    stored_tokens: bool = False
    oauth_client_id: str = ""
    oauth_client_secret: SecretStr = field(default=SecretStr(""), repr=False)
    imap_host: str = "imap.yandex.com"
    imap_port: int = 993
    allow_write: bool = False
    promotions_folder: str = "AI-Promotions"
    imap_pool_size: int = 2
    imap_idle_timeout: int = 300
    archive_folder: str = ""
    confirm_reversible: bool = True

    def __post_init__(self) -> None:
        for name in ("oauth_token", "oauth_client_secret"):
            if isinstance(getattr(self, name), str):
                object.__setattr__(self, name, SecretStr(getattr(self, name)))

    @classmethod
    def from_env(cls, *, require_credentials: bool = True) -> Settings:
        """Read the settings; require_credentials=False is for the multi-user HTTP server.

        That server never logs in with credentials of its own, so missing ones are
        not an error and the tokens saved by `yandex-mail-mcp login` are not looked
        up: a keyring lookup in a container is slow and logs warnings for nothing.
        """
        if os.getenv("YANDEX_APP_PASSWORD", "").strip():
            # Removed in 0.7.0; a silent fallback to "no credentials" would be confusing.
            raise ConfigurationError(
                "YANDEX_APP_PASSWORD is no longer supported: run `yandex-mail-mcp login`, or "
                "set YANDEX_OAUTH_TOKEN and YANDEX_EMAIL, and remove the password"
            )
        email = os.getenv("YANDEX_EMAIL", "").strip()
        token = os.getenv("YANDEX_OAUTH_TOKEN", "").strip()
        credentials: dict[str, object] = {}
        if token:
            if not email:
                raise ConfigurationError("YANDEX_OAUTH_TOKEN requires YANDEX_EMAIL")
            credentials = {"email": email, "oauth_token": SecretStr(token)}
        elif require_credentials and (stored := TokenStore().load()) is not None:
            credentials = {"email": stored.email, "stored_tokens": True}
        elif require_credentials:
            raise ConfigurationError(
                "No Yandex credentials: run `yandex-mail-mcp login`, or set YANDEX_OAUTH_TOKEN "
                "and YANDEX_EMAIL (see .env.example)"
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
            **credentials,  # type: ignore[arg-type]
            oauth_client_id=os.getenv("YANDEX_OAUTH_CLIENT_ID", "").strip(),
            oauth_client_secret=SecretStr(os.getenv("YANDEX_OAUTH_CLIENT_SECRET", "").strip()),
            imap_host=(os.getenv("YANDEX_IMAP_HOST") or "").strip() or "imap.yandex.com",
            imap_port=env_port("YANDEX_IMAP_PORT", 993),
            allow_write=env_bool("YANDEX_ALLOW_WRITE"),
            promotions_folder=promotions_folder,
            imap_pool_size=pool_size,
            imap_idle_timeout=idle_timeout,
            archive_folder=os.getenv("YANDEX_ARCHIVE_FOLDER", "").strip(),
            confirm_reversible=env_bool("MCP_CONFIRM_REVERSIBLE", True),
        )
