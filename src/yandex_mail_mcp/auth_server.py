"""A stateless OAuth authorization server for MCP clients, federated to Yandex.

The server stores nothing about users. Yandex tokens travel inside the
encrypted tokens it issues (see crypto.TokenCipher) and are decrypted on each
request. The only table on disk holds the clients registered through dynamic
client registration, with their secrets encrypted.
"""

from __future__ import annotations

import html
import logging
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import anyio.to_thread
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .crypto import TokenCipher
from .yandex_oauth import YandexOAuth, YandexOAuthError, YandexUser, can_write, requested_scope

LOG = logging.getLogger(__name__)

SCOPE_READ = "mail.read"
SCOPE_WRITE = "mail.write"
START_PATH = "/oauth/yandex/start"
CALLBACK_PATH = "/oauth/yandex/callback"
NONCE_COOKIE = "mcp_auth_nonce"

STATE_TTL = 600
CODE_TTL = 60
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600


class ClientStore:
    """Registered clients in SQLite; client secrets are sealed with the token cipher."""

    def __init__(self, path: str, cipher: TokenCipher) -> None:
        self._cipher = cipher
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS clients ("
                "client_id TEXT PRIMARY KEY, metadata TEXT NOT NULL, "
                "client_secret TEXT, created_at INTEGER NOT NULL)"
            )

    def add(self, client: OAuthClientInformationFull) -> None:
        secret = client.client_secret
        metadata = client.model_copy(update={"client_secret": None}).model_dump_json()
        sealed = (
            # Sealed for 100 years: a registration lasts until its key is rotated out.
            self._cipher.seal("client_secret", {"secret": secret}, 100 * 365 * 24 * 3600)
            if secret
            else None
        )
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO clients VALUES (?, ?, ?, ?)",
                (client.client_id, metadata, sealed, int(time.time())),
            )

    def get(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._lock:
            row = self._db.execute(
                "SELECT metadata, client_secret FROM clients WHERE client_id = ?", (client_id,)
            ).fetchone()
        if row is None:
            return None
        client = OAuthClientInformationFull.model_validate_json(row[0])
        if row[1] is not None:
            opened = self._cipher.open(row[1], "client_secret")
            if opened is None:
                # Its key was rotated out: the client has to register again.
                return None
            client.client_secret = str(opened["secret"])
        return client

    def close(self) -> None:
        self._db.close()


class RateLimit:
    """At most `limit` requests per client IP and minute to the given paths.

    In memory, like the rest of the server's state: a restart resets the counts.
    """

    def __init__(
        self,
        app: ASGIApp,
        paths: frozenset[str],
        limit: int,
        window: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.paths = paths
        self.limit = limit
        self.window = window
        self._clock = clock
        self._hits: dict[str, tuple[float, int]] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") in self.paths:
            now = self._clock()
            if len(self._hits) > 10_000:
                self._hits = {
                    ip: hit for ip, hit in self._hits.items() if now - hit[0] < self.window
                }
            ip = str((scope.get("client") or ("unknown",))[0])
            started, count = self._hits.get(ip, (now, 0))
            if now - started >= self.window:
                started, count = now, 0
            self._hits[ip] = (started, count + 1)
            if count + 1 > self.limit:
                retry = max(1, int(self.window - (now - started)))
                response = JSONResponse(
                    {"error": "too_many_requests", "error_description": "Retry later"},
                    status_code=429,
                    headers={"Retry-After": str(retry)},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class YandexAuthorizationCode(AuthorizationCode):
    grant: dict[str, Any]
    jti: str


class YandexRefreshToken(RefreshToken):
    grant: dict[str, Any]


def _page(message: str, status: int = 400) -> HTMLResponse:
    body = (
        "<!doctype html><meta charset=utf-8><title>Yandex Mail MCP</title>"
        f"<p>{html.escape(message)}</p>"
    )
    return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


class YandexAuthProvider(
    OAuthAuthorizationServerProvider[YandexAuthorizationCode, YandexRefreshToken, AccessToken]
):
    """The authorization server provider; it delegates sign-in to Yandex."""

    def __init__(
        self,
        *,
        cipher: TokenCipher,
        clients: ClientStore,
        oauth: YandexOAuth,
        public_url: str,
        allow_write: bool,
        allowed_users: frozenset[str] = frozenset(),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cipher = cipher
        self.clients = clients
        self.oauth = oauth
        self.public_url = public_url.rstrip("/")
        self.resource = f"{self.public_url}/mcp"
        self.allow_write = allow_write
        # Casefolded Yandex logins or addresses; empty lets nobody in.
        self.allowed_users = frozenset(user.casefold() for user in allowed_users)
        self._clock = clock
        # jti of exchanged authorization codes, until they expire anyway.
        self._used_codes: dict[str, float] = {}

    @property
    def callback_url(self) -> str:
        return f"{self.public_url}{CALLBACK_PATH}"

    # Clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await anyio.to_thread.run_sync(lambda: self.clients.get(client_id))

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await anyio.to_thread.run_sync(lambda: self.clients.add(client_info))

    # Authorization

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource and params.resource.rstrip("/") != self.resource:
            raise AuthorizeError("invalid_target", "This server only issues tokens for its /mcp")
        state = self.cipher.seal(
            "state",
            {
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_explicit": params.redirect_uri_provided_explicitly,
                "code_challenge": params.code_challenge,
                "state": params.state,
                # No scope requested means the scopes the client registered with.
                "scopes": params.scopes or (client.scope or SCOPE_READ).split(),
                "nonce": secrets.token_urlsafe(16),
            },
            STATE_TTL,
        )
        # The start route sets the nonce cookie: provider.authorize can only return a URL.
        return f"{self.public_url}{START_PATH}?{urlencode({'state': state})}"

    async def start(self, request: Request) -> Response:
        state = request.query_params.get("state", "")
        payload = self.cipher.open(state, "state")
        if payload is None:
            return _page("This sign-in link has expired or is invalid. Start again from your app.")
        scope = requested_scope(self.allow_write and SCOPE_WRITE in payload["scopes"])
        response = RedirectResponse(
            self.oauth.authorize_url(self.callback_url, state, scope),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )
        response.set_cookie(
            NONCE_COOKIE,
            payload["nonce"],
            max_age=STATE_TTL,
            path=CALLBACK_PATH,
            secure=self.public_url.startswith("https://"),
            httponly=True,
            samesite="lax",
        )
        return response

    def _client_redirect(self, payload: dict[str, Any], **params: str | None) -> Response:
        url = construct_redirect_uri(payload["redirect_uri"], state=payload["state"], **params)
        response = RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})
        response.delete_cookie(NONCE_COOKIE, path=CALLBACK_PATH)
        return response

    async def check_user(self, user: YandexUser, yandex_token: str) -> str | None:
        """An error message when this Yandex user may not sign in, else None.

        A refused user's Yandex token is revoked at once and goes nowhere.
        """
        if not self.allowed_users:
            message = (
                "This server lets no one sign in yet: its operator must list the allowed "
                "Yandex logins in MCP_ALLOWED_USERS."
            )
        elif {user.login.casefold(), user.email.casefold()} & self.allowed_users:
            return None
        else:
            message = f"The Yandex account {user.login} is not allowed on this server."
        LOG.warning("Refused sign-in for Yandex login %s", user.login)
        try:
            await anyio.to_thread.run_sync(lambda: self.oauth.revoke(yandex_token))
        except YandexOAuthError as exc:
            LOG.warning("Could not revoke the refused user's token: %s", exc.error)
        return message

    async def callback(self, request: Request) -> Response:
        payload = self.cipher.open(request.query_params.get("state", ""), "state")
        if payload is None:
            return _page("This sign-in has expired or is invalid. Start again from your app.")
        cookie = request.cookies.get(NONCE_COOKIE, "")
        if not secrets.compare_digest(cookie.encode(), str(payload["nonce"]).encode()):
            return _page("This sign-in was started in another browser. Start again from your app.")
        code = request.query_params.get("code")
        if not code:
            error = request.query_params.get("error") or "access_denied"
            return self._client_redirect(
                payload,
                error="access_denied" if error == "access_denied" else "server_error",
                error_description=f"Yandex sign-in failed: {error}",
            )
        try:
            tokens = await anyio.to_thread.run_sync(
                lambda: self.oauth.exchange_code(code, redirect_uri=self.callback_url)
            )
            user = await anyio.to_thread.run_sync(lambda: self.oauth.user(tokens.access_token))
        except YandexOAuthError as exc:
            LOG.warning("Yandex code exchange failed: %s", exc.error)
            return self._client_redirect(
                payload, error="server_error", error_description="Yandex rejected the sign-in"
            )
        if (denied := await self.check_user(user, tokens.access_token)) is not None:
            return _page(denied, status=403)
        grant = {
            "yandex_access_token": tokens.access_token,
            "yandex_refresh_token": tokens.refresh_token,
            "yandex_expires_at": tokens.expires_at,
            "yandex_scope": tokens.scope,
            "login": user.login,
            "email": user.email,
        }
        code_blob = self.cipher.seal(
            "code",
            {
                "client_id": payload["client_id"],
                "redirect_uri": payload["redirect_uri"],
                "redirect_uri_explicit": payload["redirect_uri_explicit"],
                "code_challenge": payload["code_challenge"],
                "scopes": payload["scopes"],
                "grant": grant,
            },
            CODE_TTL,
        )
        return self._client_redirect(payload, code=code_blob)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> YandexAuthorizationCode | None:
        payload = self.cipher.open(authorization_code, "code")
        if payload is None or payload["client_id"] != client.client_id:
            return None
        return YandexAuthorizationCode(
            code=authorization_code,
            scopes=payload["scopes"],
            expires_at=payload["exp"],
            client_id=payload["client_id"],
            code_challenge=payload["code_challenge"],
            redirect_uri=AnyUrl(payload["redirect_uri"]),
            redirect_uri_provided_explicitly=payload["redirect_uri_explicit"],
            resource=self.resource,
            subject=payload["grant"]["login"],
            grant=payload["grant"],
            jti=payload["jti"],
        )

    def _issue(self, client_id: str, scopes: list[str], grant: dict[str, Any]) -> OAuthToken:
        now = self._clock()
        if not can_write(str(grant["yandex_scope"])):
            scopes = [scope for scope in scopes if scope != SCOPE_WRITE]
        # Expire before the Yandex token inside, so clients refresh instead of failing.
        ttl = max(60.0, min(ACCESS_TTL, float(grant["yandex_expires_at"]) - 60 - now))
        claims = {"client_id": client_id, "scopes": scopes, "grant": grant}
        refresh_grant = {key: value for key, value in grant.items() if key != "yandex_access_token"}
        return OAuthToken(
            access_token=self.cipher.seal("access", claims, ttl),
            expires_in=int(ttl),
            scope=" ".join(scopes),
            refresh_token=self.cipher.seal(
                "refresh", {**claims, "grant": refresh_grant}, REFRESH_TTL
            ),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: YandexAuthorizationCode
    ) -> OAuthToken:
        now = self._clock()
        self._used_codes = {jti: exp for jti, exp in self._used_codes.items() if exp > now}
        if authorization_code.jti in self._used_codes:
            raise TokenError("invalid_grant", "authorization code was already used")
        self._used_codes[authorization_code.jti] = authorization_code.expires_at
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.grant)

    async def load_access_token(self, token: str) -> AccessToken | None:
        payload = self.cipher.open(token, "access")
        if payload is None:
            return None
        grant = payload["grant"]
        return AccessToken(
            token=token,
            client_id=payload["client_id"],
            scopes=payload["scopes"],
            expires_at=int(payload["exp"]),
            resource=self.resource,
            subject=grant["login"],
            # Lives only in memory for the request; never logged or stored.
            claims={
                "yandex_access_token": grant["yandex_access_token"],
                "yandex_login": grant["login"],
                "yandex_email": grant["email"],
                "yandex_scope": grant["yandex_scope"],
            },
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> YandexRefreshToken | None:
        payload = self.cipher.open(refresh_token, "refresh")
        if payload is None or payload["client_id"] != client.client_id:
            return None
        return YandexRefreshToken(
            token=refresh_token,
            client_id=payload["client_id"],
            scopes=payload["scopes"],
            expires_at=int(payload["exp"]),
            resource=self.resource,
            subject=payload["grant"]["login"],
            grant=payload["grant"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: YandexRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        grant = refresh_token.grant
        try:
            tokens = await anyio.to_thread.run_sync(
                lambda: self.oauth.refresh(grant["yandex_refresh_token"], grant["yandex_scope"])
            )
        except YandexOAuthError as exc:
            raise TokenError("invalid_grant", f"Yandex refused the refresh ({exc.error})") from exc
        fresh = {
            **grant,
            "yandex_access_token": tokens.access_token,
            "yandex_refresh_token": tokens.refresh_token,
            "yandex_expires_at": tokens.expires_at,
            "yandex_scope": tokens.scope,
        }
        return self._issue(client.client_id, scopes, fresh)

    async def revoke_token(self, token: AccessToken | YandexRefreshToken) -> None:
        """Revoke the Yandex grant inside: every token issued for it then stops working."""
        try:
            if isinstance(token, YandexRefreshToken):
                # Yandex revokes by access token; a refresh yields one for this grant.
                tokens = await anyio.to_thread.run_sync(
                    lambda: self.oauth.refresh(token.grant["yandex_refresh_token"])
                )
                yandex_token = tokens.access_token
            else:
                yandex_token = str((token.claims or {})["yandex_access_token"])
            await anyio.to_thread.run_sync(lambda: self.oauth.revoke(yandex_token))
        except (YandexOAuthError, KeyError) as exc:
            # RFC 7009: an invalid or already revoked token is not an error.
            LOG.info("Yandex token revocation did not complete: %s", type(exc).__name__)
