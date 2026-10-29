"""How the server logs in to IMAP: an app password, or an OAuth token via XOAUTH2."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import keyring
import keyring.errors
from imapclient import IMAPClient
from imapclient.exceptions import LoginError
from pydantic import SecretStr

from .yandex_oauth import YandexOAuth, YandexOAuthError, YandexTokens, can_write

LOG = logging.getLogger(__name__)

KEYRING_SERVICE = "yandex-mail-mcp"
KEYRING_USER = "tokens"


class Credentials(Protocol):
    @property
    def email(self) -> str: ...

    def login(self, imap: IMAPClient) -> None: ...

    def read_only_reason(self) -> str | None:
        """Why these credentials cannot change mail, when they are known to be read-only."""
        ...


@dataclass(frozen=True)
class PasswordCredentials:
    email: str
    password: SecretStr

    def login(self, imap: IMAPClient) -> None:
        imap.login(self.email, self.password.get_secret_value())

    def read_only_reason(self) -> str | None:
        return None


class TokenSource(Protocol):
    def access_token(self) -> str: ...

    def refresh(self) -> str:
        """Return a new access token, or raise LoginError when none can be had."""
        ...

    def scope(self) -> str | None:
        """The granted scope, or None when unknown."""
        ...


class StaticTokenSource:
    """A token given from outside (YANDEX_OAUTH_TOKEN or an HTTP request); it cannot refresh."""

    def __init__(self, token: str, scope: str | None = None) -> None:
        self._token = token
        self._scope = scope

    def access_token(self) -> str:
        return self._token

    def refresh(self) -> str:
        raise LoginError("The OAuth token was rejected and cannot be refreshed here")

    def scope(self) -> str | None:
        return self._scope


LOCAL_REAUTH_HINT = "run `yandex-mail-mcp login` with YANDEX_ALLOW_WRITE=true"


class OAuthCredentials:
    def __init__(
        self, email: str, source: TokenSource, reauth_hint: str = LOCAL_REAUTH_HINT
    ) -> None:
        self.email = email
        self.source = source
        self.reauth_hint = reauth_hint

    def login(self, imap: IMAPClient) -> None:
        try:
            imap.oauth2_login(self.email, self.source.access_token())
        except LoginError:
            # An expired or revoked token: refresh once and retry; a second failure is final.
            imap.oauth2_login(self.email, self.source.refresh())

    def read_only_reason(self) -> str | None:
        scope = self.source.scope()
        if scope is None or can_write(scope):
            return None
        return (
            f"Your Yandex access is read-only. Sign in again with write access: {self.reauth_hint}"
        )


@dataclass(frozen=True)
class StoredTokens:
    """What `yandex-mail-mcp login` keeps for the stdio server."""

    login: str
    email: str
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str

    def with_tokens(self, tokens: YandexTokens) -> StoredTokens:
        return StoredTokens(
            login=self.login,
            email=self.email,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            expires_at=tokens.expires_at,
            scope=tokens.scope or self.scope,
        )


def _config_dir() -> Path:
    base = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "yandex-mail-mcp"


class TokenStore:
    """The system keyring, or a 0600 file where no keyring exists (containers, headless)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _config_dir() / "tokens.json"

    def load(self) -> StoredTokens | None:
        raw: str | None
        try:
            raw = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        except keyring.errors.KeyringError:
            raw = None
        if raw is None and self.path.exists():
            raw = self.path.read_text(encoding="utf-8")
        if not raw:
            return None
        try:
            return StoredTokens(**json.loads(raw))
        except (TypeError, ValueError) as exc:
            LOG.warning("Ignoring unreadable stored tokens: %s", type(exc).__name__)
            return None

    def save(self, tokens: StoredTokens) -> str:
        """Save the tokens; return where they went."""
        raw = json.dumps(asdict(tokens))
        try:
            keyring.set_password(KEYRING_SERVICE, KEYRING_USER, raw)
        except keyring.errors.KeyringError:
            pass
        else:
            self.path.unlink(missing_ok=True)
            return "the system keyring"
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(raw)
        # O_CREAT keeps the mode of an existing file, so set it explicitly.
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        LOG.warning("No system keyring is available; tokens are stored in %s", self.path)
        return str(self.path)

    def delete(self) -> None:
        with contextlib.suppress(keyring.errors.KeyringError):
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        self.path.unlink(missing_ok=True)


class StoredTokenSource:
    """Tokens from the store, refreshed shortly before they expire and saved back."""

    def __init__(
        self,
        store: TokenStore,
        tokens: StoredTokens,
        oauth: YandexOAuth,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._tokens = tokens
        self._oauth = oauth
        self._clock = clock
        self._lock = threading.Lock()

    def access_token(self) -> str:
        with self._lock:
            if self._tokens.expires_at - 60 > self._clock():
                return self._tokens.access_token
        return self.refresh()

    def refresh(self) -> str:
        with self._lock:
            try:
                fresh = self._oauth.refresh(self._tokens.refresh_token, self._tokens.scope)
            except YandexOAuthError as exc:
                raise LoginError(
                    f"Could not refresh the Yandex token ({exc.error}); "
                    "run `yandex-mail-mcp login` again"
                ) from exc
            self._tokens = self._tokens.with_tokens(fresh)
            self._store.save(self._tokens)
            return self._tokens.access_token

    def scope(self) -> str | None:
        return self._tokens.scope
