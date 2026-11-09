"""An OAuth authorization server for MCP clients, federated to Yandex.

The server stores no user tokens. Yandex tokens travel inside the encrypted
tokens it issues (see crypto.TokenCipher) and are decrypted on each request.
Each sign-in is an authorization with a random id that every token issued for
it carries, refreshed ones included. Each refresh token also carries its
generation, and only the newest one can be exchanged: a refresh token used a
second time revokes its authorization. On disk are three tables: the clients
registered through dynamic client registration, with their secrets encrypted,
the ids of revoked authorizations, and the current refresh generation of each
authorization. The last two hold random ids and counters, no user data.
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
from urllib.parse import urlencode, urlsplit

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

from .confirmations import ConfirmationFailed, PendingAction, PendingActions
from .crypto import TokenCipher
from .yandex_oauth import (
    SCOPE_LOGIN,
    YandexOAuth,
    YandexOAuthError,
    YandexUser,
    can_write,
    requested_scope,
)

LOG = logging.getLogger(__name__)

SCOPE_READ = "mail.read"
SCOPE_WRITE = "mail.write"
START_PATH = "/oauth/yandex/start"
CALLBACK_PATH = "/oauth/yandex/callback"
CONFIRM_PATH = "/confirm/{action_id}"
NONCE_COOKIE = "mcp_auth_nonce"
# Who signed in through this browser, so confirmation pages know whom they talk to.
BROWSER_COOKIE = "mcp_auth_user"
BROWSER_TTL = 30 * 24 * 3600
# Yandex limits its state parameter; the sealed state carries the client's own state.
YANDEX_MAX_STATE = 1024
MAX_CLIENT_STATE = 256

STATE_TTL = 600
CODE_TTL = 60
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
# How much longer a refresh generation is kept than the refresh token issued with it.
GENERATION_MARGIN = 24 * 3600


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


class AuthorizationStore:
    """What the server keeps about authorizations, in SQLite: random ids and counters.

    Two tables share one connection. revoked_authorizations holds the ids of revoked
    authorizations, until their last token has expired. refresh_generations holds
    the refresh generation each authorization is at.

    A refresh token carries the generation it was issued in. Exchanging it claims
    the next generation, atomically: only a token of the current generation can,
    and only once. So a used refresh token, or a copy of one, is detected however
    the upstream Yandex refresh token behaves. An authorization with no generation
    row is at generation 0, the one its first refresh token was issued in.

    The revoked ids are read into memory at start, so the check on every request is
    a dict lookup that never waits on SQLite; each revocation publishes a new dict in
    one assignment. The writes may wait on the file, so callers on an event loop run
    them in a worker thread. One server process owns the file, like the rest of its
    state, and a restart keeps both tables.
    """

    def __init__(self, path: str, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        now = int(clock())
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS revoked_authorizations ("
                "id TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS refresh_generations ("
                "id TEXT PRIMARY KEY, generation INTEGER NOT NULL, expires_at INTEGER NOT NULL)"
            )
            self._db.execute("DELETE FROM revoked_authorizations WHERE expires_at <= ?", (now,))
            self._db.execute("DELETE FROM refresh_generations WHERE expires_at <= ?", (now,))
            self._revoked: dict[str, int] = dict(
                self._db.execute("SELECT id, expires_at FROM revoked_authorizations").fetchall()
            )

    # Revocations

    def revoke(self, authorization: str) -> None:
        # No token of the authorization outlives this: a refresh token issued just now
        # lives REFRESH_TTL, and none is issued after the revocation.
        now = self._clock()
        expires_at = int(now + REFRESH_TTL) + 1
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM revoked_authorizations WHERE expires_at <= ?", (int(now),)
            )
            self._db.execute(
                "INSERT OR REPLACE INTO revoked_authorizations VALUES (?, ?)",
                (authorization, expires_at),
            )
            revoked = {key: expires for key, expires in self._revoked.items() if expires > now}
            revoked[authorization] = expires_at
            self._revoked = revoked

    def is_revoked(self, authorization: str) -> bool:
        return authorization in self._revoked

    # Refresh generations

    def claim(self, authorization: str, generation: int) -> bool:
        """Move the authorization from generation to the next one; False if not at it.

        False means the refresh token is not the newest of its authorization: it was
        exchanged before, by its holder or by someone with a copy.
        """
        now = self._clock()
        # The token of the new generation is issued after Yandex answers, so its
        # REFRESH_TTL ends a little later than now + REFRESH_TTL. The row must outlive
        # it: a missing row would read as generation 0 and flag the token as reused.
        expires_at = int(now + REFRESH_TTL + GENERATION_MARGIN)
        with self._lock, self._db:
            self._db.execute("DELETE FROM refresh_generations WHERE expires_at <= ?", (int(now),))
            row = self._db.execute(
                "SELECT generation FROM refresh_generations WHERE id = ?", (authorization,)
            ).fetchone()
            current = 0 if row is None else int(row[0])
            if current != generation:
                return False
            self._db.execute(
                "INSERT OR REPLACE INTO refresh_generations VALUES (?, ?, ?)",
                (authorization, generation + 1, expires_at),
            )
            return True

    def release(self, authorization: str, generation: int) -> None:
        """Undo a claim of generation whose exchange failed, unless another came since.

        The refresh token of that generation can then be exchanged again.
        """
        with self._lock, self._db:
            self._db.execute(
                "UPDATE refresh_generations SET generation = ? WHERE id = ? AND generation = ?",
                (generation, authorization, generation + 1),
            )

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
        self._pruned = clock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") in self.paths:
            now = self._clock()
            # Drop addresses whose window has passed: once per window in the normal
            # case, and at once if a flood of addresses grows the table past 10 000.
            if now - self._pruned >= self.window or len(self._hits) > 10_000:
                self._hits = {
                    ip: hit for ip, hit in self._hits.items() if now - hit[0] < self.window
                }
                self._pruned = now
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
    # The refresh generation the token was issued in; see AuthorizationStore.
    generation: int


_STYLE = (
    "<style>body{font:16px/1.5 system-ui,sans-serif;max-width:40em;margin:3em auto;padding:0 1em}"
    "pre{white-space:pre-wrap;background:#f4f4f4;padding:1em;border-radius:.5em}"
    "button{font:inherit;padding:.6em 1.2em;margin:.3em .6em .3em 0;border-radius:.4em;"
    "border:1px solid #888;background:#fff;cursor:pointer}"
    "button.primary{background:#1a73e8;color:#fff;border-color:#1a73e8}</style>"
)


# Every page of this server: never cached, never shown inside another site's frame,
# so a page elsewhere cannot overlay the consent or confirmation buttons.
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
}


def _document(title: str, body: str, status: int = 200) -> HTMLResponse:
    page = (
        f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>{html.escape(title)}</title>{_STYLE}<h1>{html.escape(title)}</h1>{body}"
    )
    return HTMLResponse(page, status_code=status, headers=PAGE_HEADERS)


def _origin(url: str) -> tuple[str, str, int] | None:
    """The scheme, lowercase host and port of an http(s) URL; None if it is not one.

    The port defaults by scheme, so https://host and https://host:443 are one origin.
    A malformed URL is no origin at all, never an error.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    return parts.scheme, parts.hostname.lower(), port or (443 if parts.scheme == "https" else 80)


def _page(message: str, status: int = 400) -> HTMLResponse:
    return _document("Yandex Mail MCP", f"<p>{html.escape(message)}</p>", status)


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
        pending: PendingActions | None = None,
        authorizations: AuthorizationStore | None = None,
    ) -> None:
        self.cipher = cipher
        self.clients = clients
        # Without a file (tests), revocations and refresh generations live in memory.
        self.authorizations = authorizations or AuthorizationStore(":memory:", clock)
        # Told about each revocation, with the authorization id and the Yandex login,
        # so the MCP server can close the IMAP connections opened for it.
        self.on_revoke: Callable[[str, str], None] | None = None
        self.oauth = oauth
        self.public_url = public_url.rstrip("/")
        self.resource = f"{self.public_url}/mcp"
        self.allow_write = allow_write
        # Changes waiting for approval in the browser (clients without elicitation).
        self.pending = pending
        # Casefolded Yandex logins or addresses; empty lets nobody in.
        self.allowed_users = frozenset(user.casefold() for user in allowed_users)
        self._clock = clock
        # jti of exchanged authorization codes, until they expire anyway.
        self._used_codes: dict[str, float] = {}

    @property
    def callback_url(self) -> str:
        return f"{self.public_url}{CALLBACK_PATH}"

    @property
    def offered_scopes(self) -> list[str]:
        """The MCP scopes this server offers under its current configuration."""
        return [SCOPE_READ, SCOPE_WRITE] if self.allow_write else [SCOPE_READ]

    def confirm_url(self, action_id: str) -> str:
        return f"{self.public_url}{CONFIRM_PATH.format(action_id=action_id)}"

    # Clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        client = await anyio.to_thread.run_sync(lambda: self.clients.get(client_id))
        if client is not None:
            # What a client may ask for follows the server's configuration now, not at
            # the moment it registered: the SDK rejects a request for a scope outside
            # client.scope, so a registration made while writes were off would cap every
            # later authorization at mail.read. Asking is not getting: the user still
            # chooses on the consent page, and Yandex still has to grant full access.
            client.scope = " ".join(self.offered_scopes)
        return client

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await anyio.to_thread.run_sync(lambda: self.clients.add(client_info))

    # Authorization

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource and params.resource.rstrip("/") != self.resource:
            raise AuthorizeError("invalid_target", "This server only issues tokens for its /mcp")
        if params.state and len(params.state) > MAX_CLIENT_STATE:
            raise AuthorizeError("invalid_request", f"state exceeds {MAX_CLIENT_STATE} characters")
        state = self.cipher.seal(
            "state",
            {
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_explicit": params.redirect_uri_provided_explicitly,
                "code_challenge": params.code_challenge,
                "state": params.state,
                # No scope requested means every scope the server offers; the consent
                # page lets the user keep it to reading.
                "scopes": params.scopes or self.offered_scopes,
                "nonce": secrets.token_urlsafe(16),
            },
            STATE_TTL,
        )
        if len(state) > YANDEX_MAX_STATE:
            # Yandex rejects longer states with an error page the client never learns about.
            raise AuthorizeError("invalid_request", "redirect_uri or state is too long")
        # The start route sets the nonce cookie: provider.authorize can only return a URL.
        return f"{self.public_url}{START_PATH}?{urlencode({'state': state})}"

    async def start(self, request: Request) -> Response:
        """Ask the user how much access to grant, then send them to Yandex.

        GET shows the choice when writes are on the table (the server allows them and
        the client asked for mail.write); POST carries the choice. Read-only requests go
        straight to Yandex. The choice is sealed into the state, so the callback issues
        mail.write only when the user picked it here and Yandex granted full access.
        """
        if request.method == "POST":
            form = await request.form()
            state = str(form.get("state", ""))
            access = str(form.get("access", ""))
        else:
            state = request.query_params.get("state", "")
            access = ""
        payload = self.cipher.open(state, "state")
        if payload is None or "scopes" not in payload:
            return _page("This sign-in link has expired or is invalid. Start again from your app.")
        write_offered = self.allow_write and SCOPE_WRITE in payload["scopes"]
        if write_offered and access not in {"read", "write"}:
            return self._consent_page(state)
        return self._to_yandex(payload, write=write_offered and access == "write")

    def _consent_page(self, state: str) -> Response:
        body = (
            "<p>An app wants to connect to your Yandex Mail through this server. "
            "Choose what it may do. You can change your mind later by connecting again.</p>"
            f"<form method=post action='{START_PATH}'>"
            f"<input type=hidden name=state value='{html.escape(state, quote=True)}'>"
            "<p><button name=access value=read>Read only</button> "
            "search and read mail, nothing changes.</p>"
            "<p><button name=access value=write class=primary>Read and change mail</button> "
            "also move and archive messages, mark them read or flagged, and save drafts. "
            "Each change still asks you to confirm. The server never deletes mail, "
            "never empties folders and never sends anything.</p>"
            "</form>"
            "<p>Yandex will show its own permission page next.</p>"
        )
        return _document("Connect Yandex Mail", body)

    def _to_yandex(self, payload: dict[str, Any], *, write: bool) -> Response:
        state = self.cipher.seal("state", {**payload, "write": write}, STATE_TTL)
        if len(state) > YANDEX_MAX_STATE:
            return _page("The sign-in request is too long. Start again from your app.")
        response = RedirectResponse(
            self.oauth.authorize_url(self.callback_url, state, requested_scope(write)),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )
        self._set_nonce_cookie(response, str(payload["nonce"]))
        return response

    def _set_nonce_cookie(self, response: Response, nonce: str) -> None:
        response.set_cookie(
            NONCE_COOKIE,
            nonce,
            max_age=STATE_TTL,
            path=CALLBACK_PATH,
            secure=self.public_url.startswith("https://"),
            httponly=True,
            samesite="lax",
        )

    def _set_browser_cookie(self, request: Request, response: Response, user: YandexUser) -> None:
        """Remember who signed in through this browser, for confirmation pages.

        The cookie carries a random id of the browser, which confirmation forms are
        bound to. A new sign-in of the same user in the same browser keeps the id, so
        a confirmation page opened before it still works; another user gets a new one.
        """
        session = self._browser_session(request)
        browser = (
            session[1]
            if session is not None and session[0].login.casefold() == user.login.casefold()
            else secrets.token_hex(16)
        )
        response.set_cookie(
            BROWSER_COOKIE,
            self.cipher.seal(
                "browser",
                {"login": user.login, "email": user.email, "browser": browser},
                BROWSER_TTL,
            ),
            max_age=BROWSER_TTL,
            path="/",
            secure=self.public_url.startswith("https://"),
            httponly=True,
            samesite="lax",
        )

    def _browser_session(self, request: Request) -> tuple[YandexUser, str] | None:
        """The allowed user who signed in through this browser, and the browser's id."""
        payload = self.cipher.open(request.cookies.get(BROWSER_COOKIE, ""), "browser")
        if payload is None:
            return None
        user = YandexUser(login=str(payload["login"]), email=str(payload["email"]))
        if not (self.allowed(user.login) or self.allowed(user.email)):
            return None
        # Cookies set before browsers had an id: the cookie's own id stands in, and the
        # next sign-in in this browser keeps it.
        return user, str(payload.get("browser") or payload["jti"])

    def _same_origin(self, request: Request) -> bool:
        """Whether a form was posted from a page of this server.

        Origin decides: it must be the public URL. Referer stands in when a privacy
        setting drops Origin. Without either, the request is refused.

        Modern browsers also send Sec-Fetch-Site, which pages cannot set and referrer
        policies do not touch. Anything but "same-origin" is refused, whatever Origin
        says (a sibling subdomain is "same-site"). A Referrer-Policy of no-referrer,
        from the browser or a reverse proxy, makes Origin "null" and drops Referer;
        then "same-origin" alone identifies this server's page. A concrete Origin must
        still be the public URL. Browsers send Sec-Fetch-Site only over HTTPS and to
        localhost.
        """
        fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()
        if fetch_site and fetch_site != "same-origin":
            return False
        origin = request.headers.get("origin", "").strip()
        if fetch_site and origin in {"", "null"}:
            return True
        source = origin or request.headers.get("referer") or ""
        actual = _origin(source)
        return actual is not None and actual == _origin(self.public_url)

    def _csrf_token(self, pending: PendingAction, user: YandexUser, browser: str) -> str:
        """A token for the confirmation form: this action, this user, this browser.

        It expires with the action.
        """
        assert self.pending is not None
        return self.cipher.seal(
            "csrf",
            {"action": pending.id, "login": user.login, "browser": browser},
            max(1, self.pending.seconds_left(pending)),
        )

    def _csrf_valid(
        self, token: str, pending: PendingAction, user: YandexUser, browser: str
    ) -> bool:
        payload = self.cipher.open(token, "csrf")
        return (
            payload is not None
            and payload.get("action") == pending.id
            and str(payload.get("login", "")).casefold() == user.login.casefold()
            and payload.get("browser") == browser
        )

    def _client_redirect(self, payload: dict[str, Any], **params: str | None) -> Response:
        url = construct_redirect_uri(payload["redirect_uri"], state=payload["state"], **params)
        response = RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})
        response.delete_cookie(NONCE_COOKIE, path=CALLBACK_PATH)
        return response

    def allowed(self, login: str) -> bool:
        """Whether a login may use the server; checked on every token, not only at sign-in."""
        return login.casefold() in self.allowed_users

    async def check_user(self, user: YandexUser, yandex_token: str) -> str | None:
        """An error message when this Yandex user may not sign in, else None.

        A refused user's Yandex token is revoked at once and goes nowhere.
        """
        if not self.allowed_users:
            message = (
                "This server lets no one sign in yet: its operator must list the allowed "
                "Yandex logins in MCP_ALLOWED_USERS."
            )
        elif self.allowed(user.login) or self.allowed(user.email):
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
        if "confirm" in payload:
            return await self._confirm_callback(request, payload)
        code = request.query_params.get("code")
        if not code:
            error = request.query_params.get("error") or "access_denied"
            return self._client_redirect(
                payload,
                error="access_denied" if error == "access_denied" else "server_error",
                error_description=f"Yandex sign-in failed: {error}",
            )
        # Writes only when the user chose them on the consent page; the choice is sealed
        # in the state, and the code's scopes follow it even if Yandex granted more.
        write = bool(payload.get("write"))
        scopes = [scope for scope in payload["scopes"] if write or scope != SCOPE_WRITE]
        try:
            # Yandex may not echo the granted scope; fall back to what start() requested.
            scope = requested_scope(write)
            tokens = await anyio.to_thread.run_sync(
                lambda: self.oauth.exchange_code(code, redirect_uri=self.callback_url, scope=scope)
            )
            user = await anyio.to_thread.run_sync(lambda: self.oauth.user(tokens.access_token))
        except YandexOAuthError as exc:
            LOG.warning("Yandex code exchange failed: %s", exc.error)
            return self._client_redirect(
                payload, error="server_error", error_description="Yandex rejected the sign-in"
            )
        if (denied := await self.check_user(user, tokens.access_token)) is not None:
            # Tell the MCP client at once, instead of leaving it waiting for its callback.
            return self._client_redirect(payload, error="access_denied", error_description=denied)
        grant = {
            # Every token issued for this sign-in carries the id, refreshed ones too;
            # revoking the authorization ends them all, and only them.
            "authorization": secrets.token_hex(16),
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
                "scopes": scopes,
                "grant": grant,
            },
            CODE_TTL,
        )
        response = self._client_redirect(payload, code=code_blob)
        self._set_browser_cookie(request, response, user)
        return response

    # Confirmation of changes in the browser

    def _confirm_signin(self, pending: PendingAction) -> Response:
        """Have the user prove who they are to Yandex before showing a confirmation."""
        nonce = secrets.token_urlsafe(16)
        state = self.cipher.seal("state", {"confirm": pending.id, "nonce": nonce}, STATE_TTL)
        response = RedirectResponse(
            self.oauth.authorize_url(self.callback_url, state, " ".join(SCOPE_LOGIN)),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )
        self._set_nonce_cookie(response, nonce)
        return response

    async def _confirm_callback(self, request: Request, payload: dict[str, Any]) -> Response:
        code = request.query_params.get("code")
        if not code:
            return _page("Yandex sign-in was cancelled. Nothing was changed.")
        try:
            tokens = await anyio.to_thread.run_sync(
                lambda: self.oauth.exchange_code(code, redirect_uri=self.callback_url)
            )
            user = await anyio.to_thread.run_sync(lambda: self.oauth.user(tokens.access_token))
        except YandexOAuthError as exc:
            LOG.warning("Yandex sign-in for a confirmation failed: %s", exc.error)
            return _page("Yandex rejected the sign-in. Nothing was changed.")
        if (denied := await self.check_user(user, tokens.access_token)) is not None:
            return _page(denied, 403)
        # The identity token is not kept: it expires on its own, and revoking it could
        # end the grant behind the user's MCP tokens.
        response = RedirectResponse(
            self.confirm_url(str(payload["confirm"])),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )
        response.delete_cookie(NONCE_COOKIE, path=CALLBACK_PATH)
        self._set_browser_cookie(request, response, user)
        return response

    async def confirm(self, request: Request) -> Response:
        """Show a pending change to its owner and carry out their decision."""
        if self.pending is None:
            return _page("This server does not confirm changes in the browser.", 404)
        pending = self.pending.get(request.path_params["action_id"])
        if pending is None:
            return _page(
                "This confirmation has expired or does not exist. Nothing was changed; "
                "ask the assistant again if you still want it.",
                404,
            )
        if self.authorizations.is_revoked(pending.authorization):
            return _page(
                "The connection that asked for this change was signed out. Nothing was changed.",
                404,
            )
        session = self._browser_session(request)
        if session is None or session[0].login.casefold() != pending.login.casefold():
            return self._confirm_signin(pending)
        user, browser = session
        if pending.decided:
            return self._decision_page(pending)
        if request.method != "POST":
            return self._confirm_page(pending, self._csrf_token(pending, user, browser))
        # The cookie alone proves nothing about who sent the form: a page on a sibling
        # subdomain gets it too. The decision must come from this server's own page,
        # opened in this browser for this change.
        if not self._same_origin(request):
            LOG.warning(
                "Confirmation %s refused: posted from another origin "
                "(Sec-Fetch-Site=%r, Origin=%r, Referer present=%s)",
                pending.name,
                request.headers.get("sec-fetch-site"),
                request.headers.get("origin"),
                "referer" in request.headers,
            )
            return _page(
                "This decision did not come from this server's confirmation page. "
                "Nothing was changed; open the link from the assistant and decide there.",
                403,
            )
        form = await request.form()
        if not self._csrf_valid(str(form.get("csrf", "")), pending, user, browser):
            LOG.warning("Confirmation %s refused: invalid form token", pending.name)
            return _page(
                "This confirmation form is out of date or was opened in another browser. "
                "Nothing was changed; reload the link and decide again.",
                403,
            )
        decision = str(form.get("decision", ""))
        if decision not in {"approve", "reject"}:
            return _page("Choose Approve or Reject.")
        claimed = self.pending.claim(pending.id, user.login, decision)
        if claimed is None:
            return self._decision_page(pending)
        if decision == "reject":
            self.pending.finish(pending, result=None, error=None)
            LOG.info("Confirmation %s rejected by %s", pending.name, pending.login)
            return self._decision_page(pending)
        try:
            result = await anyio.to_thread.run_sync(pending.action)
        except ConfirmationFailed as exc:
            self.pending.finish(pending, result=None, error=str(exc))
        except Exception:
            LOG.exception("Approved %s failed", pending.name)
            self.pending.finish(
                pending, result=None, error="The change failed; the server log has details."
            )
        else:
            self.pending.finish(pending, result=result, error=None)
        return self._decision_page(pending)

    def _confirm_page(self, pending: PendingAction, csrf: str) -> Response:
        assert self.pending is not None
        minutes = max(1, self.pending.seconds_left(pending) // 60)
        body = (
            f"<p>Signed in as <b>{html.escape(pending.email)}</b>. The assistant asks for this "
            f"change; it happens only if you approve it here within {minutes} min.</p>"
            f"<pre>{html.escape(pending.question)}</pre>"
            "<form method=post>"
            f"<input type=hidden name=csrf value='{html.escape(csrf, quote=True)}'>"
            "<button name=decision value=approve class=primary>Approve</button>"
            "<button name=decision value=reject>Reject</button>"
            "</form>"
        )
        return _document(f"{pending.name}: approve?", body)

    @staticmethod
    def _decision_page(pending: PendingAction) -> Response:
        if pending.decision == "reject":
            body = "<p>Rejected. Nothing was changed.</p>"
        elif pending.error is not None:
            body = f"<p>Approved, but the change failed: {html.escape(pending.error)}</p>"
        elif pending.result is not None:
            details = "".join(
                f"<li>{html.escape(str(key))}: {html.escape(str(value))}</li>"
                for key, value in pending.result.items()
            )
            body = f"<p>Done.</p><ul>{details}</ul>"
        else:
            body = "<p>This change was already decided.</p>"
        return _document(f"{pending.name}: {pending.decision or 'decided'}", body)

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

    def _grant_valid(self, grant: dict[str, Any]) -> bool:
        """Whether tokens of this grant may be used now; checked on every token."""
        # Tokens issued before authorizations had ids cannot be revoked one by one,
        # so they are refused and the client signs in again.
        authorization = grant.get("authorization")
        if not isinstance(authorization, str) or self.authorizations.is_revoked(authorization):
            return False
        # Removing someone from MCP_ALLOWED_USERS ends their tokens, not just new sign-ins.
        return self.allowed(str(grant["login"])) or self.allowed(str(grant["email"]))

    def authorization_revoked(self, authorization: str) -> bool:
        return self.authorizations.is_revoked(authorization)

    async def revoke_authorization(self, authorization: str, login: str) -> None:
        """End every token of this authorization here, without waiting for Yandex.

        Changes the authorization left waiting for the browser are dropped, and the
        MCP server closes the IMAP connections opened for it.
        """
        await anyio.to_thread.run_sync(lambda: self.authorizations.revoke(authorization))
        if self.pending is not None:
            self.pending.discard(authorization)
        LOG.info("Authorization of Yandex login %s revoked", login)
        if self.on_revoke is not None:
            callback = self.on_revoke
            await anyio.to_thread.run_sync(lambda: callback(authorization, login))

    def _issue(
        self, client_id: str, scopes: list[str], grant: dict[str, Any], generation: int = 0
    ) -> OAuthToken:
        """An access token and a refresh token of the given refresh generation."""
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
                "refresh", {**claims, "grant": refresh_grant, "generation": generation}, REFRESH_TTL
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
        if not self._grant_valid(grant):
            return None
        return AccessToken(
            token=token,
            client_id=payload["client_id"],
            scopes=payload["scopes"],
            expires_at=int(payload["exp"]),
            resource=self.resource,
            subject=grant["login"],
            # Lives only in memory for the request; never logged or stored.
            claims={
                "authorization_id": grant["authorization"],
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
        if not self._grant_valid(payload["grant"]):
            return None
        # Tokens issued before generations existed are of generation 0.
        generation = payload.get("generation", 0)
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            return None
        return YandexRefreshToken(
            token=refresh_token,
            client_id=payload["client_id"],
            scopes=payload["scopes"],
            expires_at=int(payload["exp"]),
            resource=self.resource,
            subject=payload["grant"]["login"],
            grant=payload["grant"],
            generation=generation,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: YandexRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Rotate the refresh token: the used one is spent, a reused one revokes all.

        The generation is claimed before Yandex is asked, so a replayed token never
        reaches Yandex, and of two requests with one token only one can win. If the
        exchange fails for any reason before a new token is issued, the claim is
        released and the client may retry with the same token. A token that is not of the current generation was exchanged
        before: either a copy of it is in other hands or the client replayed it. Both
        copies are then useless, the authorization is revoked, and the user signs in
        again.
        """
        grant = refresh_token.grant
        authorization, generation = str(grant["authorization"]), refresh_token.generation
        claimed = await anyio.to_thread.run_sync(
            lambda: self.authorizations.claim(authorization, generation)
        )
        if not claimed:
            LOG.warning(
                "Refresh token of Yandex login %s used again: authorization revoked",
                grant["login"],
            )
            await self.revoke_authorization(authorization, str(grant["login"]))
            raise TokenError("invalid_grant", "this refresh token was already used; sign in again")
        try:
            try:
                tokens = await anyio.to_thread.run_sync(
                    lambda: self.oauth.refresh(grant["yandex_refresh_token"], grant["yandex_scope"])
                )
            except YandexOAuthError as exc:
                raise TokenError(
                    "invalid_grant", f"Yandex refused the refresh ({exc.error})"
                ) from exc
            if not self._grant_valid(grant):
                # Revoked while Yandex was answering.
                raise TokenError("invalid_grant", "the authorization was revoked")
            fresh = {
                **grant,
                "yandex_access_token": tokens.access_token,
                "yandex_refresh_token": tokens.refresh_token,
                "yandex_expires_at": tokens.expires_at,
                "yandex_scope": tokens.scope,
            }
            return self._issue(client.client_id, scopes, fresh, generation + 1)
        except BaseException:
            # No new token went out, whatever stopped the exchange (Yandex, an error,
            # the request cancelled): the client's token must stay usable, or its retry
            # would read as a reuse and end the sign-in. Shielded, so a cancellation
            # cannot skip the release.
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(
                    lambda: self.authorizations.release(authorization, generation)
                )
            raise

    async def revoke_token(self, token: AccessToken | YandexRefreshToken) -> None:
        """Revoke the token's authorization here, then the Yandex grant inside.

        Every token of the authorization stops working at once, whatever Yandex does.
        """
        if isinstance(token, YandexRefreshToken):
            authorization, login = token.grant["authorization"], token.grant["login"]
        else:
            claims = token.claims or {}
            authorization, login = claims["authorization_id"], claims["yandex_login"]
        await self.revoke_authorization(str(authorization), str(login))
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
