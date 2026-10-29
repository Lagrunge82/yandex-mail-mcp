"""A small client for Yandex OAuth: authorize URL, token exchange, refresh, revoke and user info.

Uses only the standard library, since every call is a single form POST or GET.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
TOKEN_URL = "https://oauth.yandex.ru/token"
REVOKE_URL = "https://oauth.yandex.ru/revoke_token"
INFO_URL = "https://login.yandex.ru/info"

# IMAP access as named in the Yandex OAuth console; login:* lets us read the address.
SCOPE_IMAP_READ = "mail:imap_ro"
SCOPE_IMAP_FULL = "mail:imap_full"
SCOPE_LOGIN = ("login:info", "login:email")


def requested_scope(allow_write: bool) -> str:
    """The scope to ask Yandex for: full IMAP access only when writes are enabled."""
    return " ".join([SCOPE_IMAP_FULL if allow_write else SCOPE_IMAP_READ, *SCOPE_LOGIN])


def can_write(scope: str) -> bool:
    return SCOPE_IMAP_FULL in scope.split()


def pkce_pair() -> tuple[str, str]:
    """An RFC 7636 code_verifier and its S256 code_challenge."""
    verifier = secrets.token_urlsafe(64)  # 86 characters, within the 43-128 RFC 7636 allows
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class YandexOAuthError(Exception):
    """Yandex rejected an OAuth request, or could not be reached."""

    def __init__(self, error: str, description: str = "") -> None:
        super().__init__(f"{error}: {description}" if description else error)
        self.error = error


@dataclass(frozen=True)
class YandexTokens:
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str


@dataclass(frozen=True)
class YandexUser:
    login: str
    email: str


Transport = Callable[[urllib.request.Request], dict[str, Any]]


def _send(request: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            data = json.loads(body)
        except ValueError:
            raise YandexOAuthError("http_error", f"HTTP {exc.code}") from exc
        if not isinstance(data, dict):
            raise YandexOAuthError("http_error", f"HTTP {exc.code}") from exc
        raise YandexOAuthError(
            str(data.get("error", f"http_{exc.code}")), str(data.get("error_description", ""))
        ) from exc
    except OSError as exc:
        raise YandexOAuthError("unavailable", str(exc)) from exc
    try:
        data = json.loads(body) if body else {}
    except ValueError as exc:
        raise YandexOAuthError("invalid_response", "Yandex returned malformed JSON") from exc
    if not isinstance(data, dict):
        raise YandexOAuthError("invalid_response", "Yandex returned an unexpected response")
    return data


class YandexOAuth:
    def __init__(
        self,
        client_id: str,
        client_secret: str = "",
        *,
        transport: Transport = _send,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self._transport = transport
        self._clock = clock

    def authorize_url(
        self, redirect_uri: str, state: str, scope: str, code_challenge: str | None = None
    ) -> str:
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": scope,
            # Show the account chooser, so a second account can be connected.
            "force_confirm": "yes",
        }
        if code_challenge:
            params |= {"code_challenge": code_challenge, "code_challenge_method": "S256"}
        return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    def _post(self, url: str, fields: dict[str, str]) -> dict[str, Any]:
        fields = {"client_id": self.client_id, **fields}
        if self.client_secret:
            fields["client_secret"] = self.client_secret
        request = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(fields).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        return self._transport(request)

    def _tokens(
        self, data: dict[str, Any], *, refresh_token: str = "", scope: str = ""
    ) -> YandexTokens:
        access = data.get("access_token")
        if not isinstance(access, str) or not access:
            raise YandexOAuthError("invalid_response", "Yandex returned no access token")
        expires_in = data.get("expires_in")
        lifetime = float(expires_in) if isinstance(expires_in, (int, float)) else 3600.0
        return YandexTokens(
            access_token=access,
            # Yandex may keep the old refresh token, in which case it omits the field.
            refresh_token=str(data.get("refresh_token") or refresh_token),
            expires_at=self._clock() + lifetime,
            scope=str(data.get("scope") or scope),
        )

    def exchange_code(
        self,
        code: str,
        *,
        redirect_uri: str | None = None,
        code_verifier: str | None = None,
        scope: str = "",
    ) -> YandexTokens:
        """Exchange an authorization code; scope is assumed when Yandex does not echo it."""
        fields = {"grant_type": "authorization_code", "code": code}
        if redirect_uri:
            fields["redirect_uri"] = redirect_uri
        if code_verifier:
            fields["code_verifier"] = code_verifier
        return self._tokens(self._post(TOKEN_URL, fields), scope=scope)

    def refresh(self, refresh_token: str, scope: str = "") -> YandexTokens:
        if not refresh_token:
            raise YandexOAuthError("invalid_grant", "No refresh token is available")
        data = self._post(
            TOKEN_URL, {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )
        return self._tokens(data, refresh_token=refresh_token, scope=scope)

    def revoke(self, access_token: str) -> None:
        """Revoke the token and, with it, the grant: Yandex then rejects its refresh token too."""
        self._post(REVOKE_URL, {"access_token": access_token})

    def user(self, access_token: str) -> YandexUser:
        request = urllib.request.Request(
            f"{INFO_URL}?format=json", headers={"Authorization": f"OAuth {access_token}"}
        )
        data = self._transport(request)
        login = str(data.get("login") or "")
        if not login:
            raise YandexOAuthError("invalid_response", "Yandex returned no login")
        email = str(data.get("default_email") or "")
        if not email:
            # Yandex 360 domain logins are already addresses.
            email = login if "@" in login else f"{login}@yandex.ru"
        return YandexUser(login=login, email=email)
