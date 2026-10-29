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


_STYLE = (
    "<style>body{font:16px/1.5 system-ui,sans-serif;max-width:40em;margin:3em auto;padding:0 1em}"
    "pre{white-space:pre-wrap;background:#f4f4f4;padding:1em;border-radius:.5em}"
    "button{font:inherit;padding:.6em 1.2em;margin:.3em .6em .3em 0;border-radius:.4em;"
    "border:1px solid #888;background:#fff;cursor:pointer}"
    "button.primary{background:#1a73e8;color:#fff;border-color:#1a73e8}</style>"
)


def _document(title: str, body: str, status: int = 200) -> HTMLResponse:
    page = (
        f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>{html.escape(title)}</title>{_STYLE}<h1>{html.escape(title)}</h1>{body}"
    )
    return HTMLResponse(page, status_code=status, headers={"Cache-Control": "no-store"})


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
    ) -> None:
        self.cipher = cipher
        self.clients = clients
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

    def _set_browser_cookie(self, response: Response, user: YandexUser) -> None:
        """Remember who signed in through this browser, for confirmation pages."""
        response.set_cookie(
            BROWSER_COOKIE,
            self.cipher.seal("browser", {"login": user.login, "email": user.email}, BROWSER_TTL),
            max_age=BROWSER_TTL,
            path="/",
            secure=self.public_url.startswith("https://"),
            httponly=True,
            samesite="lax",
        )

    def browser_user(self, request: Request) -> YandexUser | None:
        """The allowed Yandex user who signed in through this browser, if any."""
        payload = self.cipher.open(request.cookies.get(BROWSER_COOKIE, ""), "browser")
        if payload is None:
            return None
        user = YandexUser(login=str(payload["login"]), email=str(payload["email"]))
        if not (self.allowed(user.login) or self.allowed(user.email)):
            return None
        return user

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
        self._set_browser_cookie(response, user)
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
        self._set_browser_cookie(response, user)
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
        user = self.browser_user(request)
        if user is None or user.login.casefold() != pending.login.casefold():
            return self._confirm_signin(pending)
        if pending.decided:
            return self._decision_page(pending)
        if request.method != "POST":
            return self._confirm_page(pending)
        form = await request.form()
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

    def _confirm_page(self, pending: PendingAction) -> Response:
        assert self.pending is not None
        minutes = max(1, self.pending.seconds_left(pending) // 60)
        body = (
            f"<p>Signed in as <b>{html.escape(pending.email)}</b>. The assistant asks for this "
            f"change; it happens only if you approve it here within {minutes} min.</p>"
            f"<pre>{html.escape(pending.question)}</pre>"
            "<form method=post>"
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

    def _grant_allowed(self, grant: dict[str, Any]) -> bool:
        # Removing someone from MCP_ALLOWED_USERS ends their tokens, not just new sign-ins.
        return self.allowed(str(grant["login"])) or self.allowed(str(grant["email"]))

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
        if not self._grant_allowed(grant):
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
        if not self._grant_allowed(payload["grant"]):
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
