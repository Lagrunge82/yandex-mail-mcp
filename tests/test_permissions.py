import asyncio
import json
import logging
from contextlib import contextmanager
from types import SimpleNamespace

import anyio.to_thread
import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from test_auth_server import FakeYandex, query_of, run_flow
from test_mutations import WriteIMAP

from yandex_mail_mcp import server
from yandex_mail_mcp.auth_server import RateLimit
from yandex_mail_mcp.config import Settings
from yandex_mail_mcp.mail import Mailbox
from yandex_mail_mcp.yandex_oauth import YandexTokens, YandexUser


@pytest.fixture(autouse=True)
def run_sync_inline(monkeypatch):
    async def run_sync(fn, *args, **kwargs):
        return fn(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", run_sync)


@contextmanager
def signed_in(scope, login="ivan", token="ya-secret-token-123", scopes=("mail.read", "mail.write")):
    access = AccessToken(
        token="bearer",
        client_id="client-1",
        scopes=list(scopes),
        claims={
            "yandex_access_token": token,
            "yandex_login": login,
            "yandex_email": f"{login}@yandex.ru",
            "yandex_scope": scope,
        },
    )
    reset = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(reset)


class Client:
    session = SimpleNamespace(check_client_capability=lambda capability: True)

    def __init__(self):
        self.messages = []

    async def elicit(self, message, schema):
        self.messages.append(message)
        return SimpleNamespace(action="accept", data=SimpleNamespace(approve=True))


@pytest.fixture
def user_mailboxes(monkeypatch):
    imap = WriteIMAP()

    @contextmanager
    def connect(self):
        yield imap

    monkeypatch.setattr(Mailbox, "connect", connect)
    monkeypatch.setattr(server, "_mailboxes", {})
    monkeypatch.setattr(server, "_settings", lambda: Settings(allow_write=True))
    return imap


def test_read_only_user_is_refused_before_elicitation(user_mailboxes):
    client = Client()
    with signed_in("mail:imap_ro login:info"), pytest.raises(ToolError) as raised:
        asyncio.run(server.mark_read(3, client))
    assert "read-only" in str(raised.value)
    assert "reconnect this server" in str(raised.value)
    assert client.messages == []
    assert user_mailboxes.changes == []


def test_full_access_user_passes(user_mailboxes):
    client = Client()
    with signed_in("mail:imap_full login:info"):
        result = asyncio.run(server.mark_read(3, client))
    assert result["action"] == "mark_read"
    assert len(client.messages) == 1
    assert user_mailboxes.changes == [("add_flags", [3], ["\\Seen"])]


def test_http_user_is_told_to_reconnect_the_client(user_mailboxes):
    with signed_in("mail:imap_full"):
        credentials = server._mailbox().credentials
    assert credentials.relogin_hint == server.HTTP_RELOGIN_HINT
    assert "MCP client" in credentials.relogin_hint
    assert "yandex-mail-mcp login" not in credentials.relogin_hint


def test_server_flag_is_the_upper_bound(user_mailboxes, monkeypatch):
    monkeypatch.setattr(server, "_settings", lambda: Settings(allow_write=False))
    with signed_in("mail:imap_full"), pytest.raises(ToolError, match="YANDEX_ALLOW_WRITE"):
        asyncio.run(server.create_draft(["a@example.com"], "s", "b", Client()))


def callback_with(yandex, allowed, monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_USERS", allowed)

    async def scenario(flow):
        client_id = await flow.register()
        _, params = await flow.to_callback(client_id)
        return await flow.client.get("/oauth/yandex/callback", params=params)

    response, _ = run_flow(scenario, yandex=yandex)
    return response


def test_user_outside_allowlist_is_refused_and_revoked(monkeypatch):
    yandex = FakeYandex(users={"yc": YandexUser("mallory", "mallory@yandex.ru")})
    response = callback_with(yandex, "ivan, anna@company.ru", monkeypatch)
    # The client hears at once, with the reason, and gets no code.
    query = query_of(response.headers["location"])
    assert query["error"] == "access_denied"
    assert "mallory is not allowed" in query["error_description"]
    assert "code" not in query
    assert yandex.revoked == ["ya-access-yc"]


def test_empty_allowlist_refuses_everyone(monkeypatch):
    yandex = FakeYandex()
    response = callback_with(yandex, "", monkeypatch)
    query = query_of(response.headers["location"])
    assert query["error"] == "access_denied"
    assert "MCP_ALLOWED_USERS" in query["error_description"]
    assert yandex.revoked == ["ya-access-yc"]


def test_allowlist_matches_login_or_address_case_insensitively(monkeypatch):
    yandex = FakeYandex(users={"yc": YandexUser("Anna@Company.ru", "anna@company.ru")})
    response = callback_with(yandex, "anna@company.RU", monkeypatch)
    assert response.status_code == 302
    assert "code" in query_of(response.headers["location"])
    assert yandex.revoked == []


def test_logout_revokes_the_token_of_the_request(monkeypatch, user_mailboxes):
    yandex = FakeYandex()
    monkeypatch.setattr(server, "_yandex_oauth", yandex)
    with signed_in("mail:imap_ro", token="ya-token-of-ivan"):
        server._mailbox()
        assert asyncio.run(server.logout()) == {"signed_out": "ivan", "revoked": True}
    assert yandex.revoked == ["ya-token-of-ivan"]
    assert ("user", "ivan") not in server._mailboxes


def test_logout_without_a_signed_in_user_points_to_the_command(monkeypatch):
    monkeypatch.setattr(server, "_yandex_oauth", FakeYandex())
    with pytest.raises(ToolError, match="yandex-mail-mcp logout"):
        asyncio.run(server.logout())


def test_audit_log_names_user_and_tool_but_not_content(user_mailboxes, caplog):
    caplog.set_level(logging.INFO, logger="yandex_mail_mcp.audit")
    with signed_in("mail:imap_full", token="ya-secret-token-123"):
        asyncio.run(server.move_email(3, "Налоги", Client()))
        with pytest.raises(ToolError):
            asyncio.run(server.move_email(3, "Спам", Client()))
    records = [json.loads(record.getMessage()) for record in caplog.records]
    assert [record["result"] for record in records] == ["ok", "MailValueError"]
    first = records[0]
    assert first["yandex_login"] == "ivan"
    assert first["client_id"] == "client-1"
    assert first["tool"] == "move_email"
    assert (first["folder"], first["uid"]) == ("Налоги", 3)
    assert isinstance(first["duration_ms"], int)
    text = caplog.text
    assert "Sale" not in text  # The subject the confirmation showed.
    assert "shop@example.com" not in text
    assert "ya-secret" not in text
    assert "bearer" not in text


def test_audit_log_for_local_calls_has_no_user(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="yandex_mail_mcp.audit")

    class Box:
        def folders(self):
            return ["INBOX"]

    monkeypatch.setattr(server, "_mailbox", lambda: Box())
    server.list_folders()
    record = json.loads(caplog.records[-1].getMessage())
    assert record["yandex_login"] is None
    assert record["tool"] == "list_folders"
    assert record["result"] == "ok"


def test_rate_limit_answers_429_per_ip_and_window():
    now = [0.0]
    app = Starlette(routes=[Route("/token", lambda request: PlainTextResponse("ok"))])
    limited = RateLimit(app, frozenset({"/token"}), limit=2, clock=lambda: now[0])
    client = TestClient(limited)
    assert [client.get("/token").status_code for _ in range(3)] == [200, 200, 429]
    assert client.get("/token").headers["retry-after"] == "60"
    now[0] = 61
    assert client.get("/token").status_code == 200
    other = TestClient(limited, client=("10.0.0.2", 1234))
    assert other.get("/token").status_code == 200


def test_oauth_app_limits_auth_endpoints(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_RATE_LIMIT", "2")

    async def scenario(flow):
        return [
            (
                await flow.client.post("/register", json={"redirect_uris": ["http://x/cb"]})
            ).status_code
            for _ in range(3)
        ]

    statuses, _ = run_flow(scenario)
    assert statuses == [201, 201, 429]


def test_token_without_mail_write_cannot_change_mail(user_mailboxes):
    # Yandex granted full access, but this client's token was narrowed to mail.read.
    client = Client()
    with signed_in("mail:imap_full", scopes=["mail.read"]), pytest.raises(ToolError) as raised:
        asyncio.run(server.mark_read(3, client))
    assert "read-only" in str(raised.value)
    assert client.messages == []


def test_removed_user_loses_access_and_refresh(monkeypatch):
    async def scenario(flow):
        client_id, tokens = await flow.sign_in()
        app_provider = flow.provider
        app_provider.allowed_users = frozenset()
        listed = await flow.mcp(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
            tokens["access_token"],
        )
        refreshed = await flow.client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client_id,
            },
        )
        return listed, refreshed

    monkeypatch.setenv("MCP_ALLOWED_USERS", "ivan")
    (listed, refreshed), yandex = run_flow(scenario)
    assert listed.status_code == 401
    assert refreshed.json()["error"] == "invalid_grant"
    assert yandex.refreshed == []


class SilentScopeYandex(FakeYandex):
    """Yandex that does not echo the granted scope in its token response."""

    def exchange_code(self, code, **kwargs):
        tokens = super().exchange_code(code, **kwargs)
        return YandexTokens(
            tokens.access_token, tokens.refresh_token, tokens.expires_at, kwargs.get("scope", "")
        )


def test_requested_scope_is_kept_when_yandex_does_not_echo_it(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_USERS", "ivan")

    async def scenario(flow):
        return (await flow.sign_in())[1]

    tokens, _ = run_flow(
        scenario, yandex=SilentScopeYandex(scope=""), settings=Settings(allow_write=True)
    )
    assert tokens["scope"] == "mail.read mail.write"


def test_overlong_client_state_is_refused(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_USERS", "ivan")

    async def scenario(flow):
        client_id = await flow.register()
        return await flow.client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": "http://localhost:9999/cb",
                "code_challenge": "x" * 43,
                "code_challenge_method": "S256",
                "state": "s" * 300,
            },
        )

    response, _ = run_flow(scenario)
    query = query_of(response.headers["location"])
    assert query["error"] == "invalid_request"
    assert "state" in query["error_description"]


def test_typical_sealed_state_fits_yandex_limit(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_USERS", "ivan")

    async def scenario(flow):
        client_id = await flow.register()
        state, _ = await flow.to_callback(client_id)
        return state

    state, _ = run_flow(scenario)
    assert len(state) < 1024
