"""`yandex-mail-mcp login` and `logout`: Yandex OAuth for the local stdio server.

login runs the authorization code flow with PKCE through a temporary HTTP server on
the loopback interface, then keeps the tokens in the system keyring (or a 0600 file).
"""

from __future__ import annotations

import hmac
import html
import os
import secrets
import threading
import urllib.parse
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from .config import ConfigurationError, env_bool, env_int
from .credentials import StoredTokens, TokenStore
from .yandex_oauth import YandexOAuth, YandexOAuthError, pkce_pair, requested_scope

CALLBACK_PATH = "/callback"


class LoginError(Exception):
    """The login flow failed; the message is meant for the person at the terminal."""


def _oauth() -> YandexOAuth:
    client_id = os.getenv("YANDEX_OAUTH_CLIENT_ID", "").strip()
    if not client_id:
        raise ConfigurationError(
            "Set YANDEX_OAUTH_CLIENT_ID to the ID of your Yandex OAuth application (see README)"
        )
    return YandexOAuth(client_id, os.getenv("YANDEX_OAUTH_CLIENT_SECRET", "").strip())


def wait_for_callback(
    server: HTTPServer, expected_state: str, timeout: float = 300
) -> tuple[str | None, str | None]:
    """Serve the loopback callback until it arrives; return (code, error)."""
    result: dict[str, str | None] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            if url.path != CALLBACK_PATH:
                self.send_error(404)
                return
            query = urllib.parse.parse_qs(url.query)
            state = query.get("state", [""])[0]
            if not hmac.compare_digest(state.encode(), expected_state.encode()):
                result.update(code=None, error="the state parameter did not match")
            elif "code" in query:
                result.update(code=query["code"][0], error=None)
            else:
                error = query.get("error_description", query.get("error", ["no code"]))[0]
                result.update(code=None, error=error)
            message = (
                "Signed in. You can close this tab."
                if result.get("code")
                else f"Sign-in failed: {result.get('error')}"
            )
            body = f"<!doctype html><meta charset=utf-8><p>{html.escape(message)}</p>".encode()
            self.send_response(200 if result.get("code") else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass  # The query string holds the authorization code.

    server.RequestHandlerClass = Handler
    server.timeout = 1
    timer = threading.Timer(timeout, lambda: result.setdefault("error", "timed out"))
    timer.start()
    try:
        while "error" not in result:
            server.handle_request()
    finally:
        timer.cancel()
    return result.get("code"), result.get("error")


def login(
    *,
    oauth: YandexOAuth | None = None,
    store: TokenStore | None = None,
    open_browser: Callable[[str], object] = webbrowser.open,
    output: Callable[[str], None] = print,
    server: HTTPServer | None = None,
) -> StoredTokens:
    oauth = oauth or _oauth()
    store = store or TokenStore()
    if server is None:
        port = env_int("YANDEX_OAUTH_LOOPBACK_PORT", 8765)
        server = HTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}{CALLBACK_PATH}"
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(32)
    scope = requested_scope(env_bool("YANDEX_ALLOW_WRITE"))
    url = oauth.authorize_url(redirect_uri, state, scope, challenge)
    output(f"Opening the Yandex sign-in page. If it does not open, visit:\n{url}")
    open_browser(url)
    try:
        code, error = wait_for_callback(server, state)
    finally:
        server.server_close()
    if code is None:
        raise LoginError(f"Sign-in failed: {error}")
    try:
        tokens = oauth.exchange_code(
            code, redirect_uri=redirect_uri, code_verifier=verifier, scope=scope
        )
        user = oauth.user(tokens.access_token)
    except YandexOAuthError as exc:
        raise LoginError(f"Yandex rejected the sign-in: {exc}") from exc
    stored = StoredTokens(
        login=user.login,
        email=user.email,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_at=tokens.expires_at,
        scope=tokens.scope,
    )
    where = store.save(stored)
    access = "read and write" if "mail:imap_full" in stored.scope.split() else "read-only"
    output(f"Signed in as {user.email} ({access} IMAP access); tokens saved in {where}.")
    return stored


def logout(
    *,
    oauth: YandexOAuth | None = None,
    store: TokenStore | None = None,
    output: Callable[[str], None] = print,
) -> None:
    store = store or TokenStore()
    tokens = store.load()
    if tokens is None:
        output("Not signed in.")
        return
    try:
        (oauth or _oauth()).revoke(tokens.access_token)
    except (YandexOAuthError, ConfigurationError) as exc:
        output(f"Could not revoke the token at Yandex ({exc}); deleting it locally anyway.")
    store.delete()
    output(f"Signed out {tokens.email}.")
