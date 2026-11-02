"""Revoking an authorization ends its tokens, and each authorization has its own IMAP pool."""

import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_auth_server import (
    PUBLIC,
    Clock,
    FakeYandex,
    _raw_keys,
    run_flow,
    sse_json,
)
from test_mutations import WriteIMAP
from test_permissions import signed_in
from test_pool import FakeIMAP

from yandex_mail_mcp import mail, server
from yandex_mail_mcp.auth_server import AuthorizationStore, ClientStore, YandexAuthProvider
from yandex_mail_mcp.config import Settings
from yandex_mail_mcp.confirmations import ConfirmationFailed, PendingActions
from yandex_mail_mcp.crypto import TokenCipher, generate_key
from yandex_mail_mcp.yandex_oauth import YandexOAuthError, YandexUser

FULL = "mail:imap_full login:info login:email"
IVAN = YandexUser("ivan", "ivan@yandex.ru")


@pytest.fixture(autouse=True)
def allowed_users(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_USERS", "ivan")
    monkeypatch.delenv("MCP_AUTH_RATE_LIMIT", raising=False)


class TokenIMAP(FakeIMAP):
    """An IMAP session that remembers the token it logged in with and what it served."""

    def oauth2_login(self, user, token):
        super().oauth2_login(user, token)
        self.token = token

    def list_folders(self):
        self.calls.append("list")
        return super().list_folders()


@pytest.fixture
def imap(monkeypatch):
    FakeIMAP.instances = []
    monkeypatch.setattr(mail, "IMAPClient", TokenIMAP)
    monkeypatch.setattr(server, "_mailboxes", {})
    monkeypatch.setattr(server, "_mailbox_used", {})
    monkeypatch.setattr(server, "_settings", lambda: Settings())
    return FakeIMAP.instances


def two_logins_of_ivan():
    """Yandex gives each of the user's two sign-ins its own token: codes yc and yd."""
    return FakeYandex(users={"yc": IVAN, "yd": IVAN})


async def call_tool(flow, token, name, arguments=None):
    """A fresh MCP session that calls one tool; the HTTP response if it was refused."""
    params = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    }
    initialized = await flow.mcp("initialize", params, token)
    if initialized.status_code != 200:
        return initialized
    session = initialized.headers["mcp-session-id"]
    await flow.mcp("notifications/initialized", {}, token, session, request_id=None)
    called = await flow.mcp(
        "tools/call", {"name": name, "arguments": arguments or {}}, token, session, request_id=2
    )
    return sse_json(called)["result"]


async def refresh(flow, client_id, tokens):
    return await flow.client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client_id,
        },
    )


async def revoke(flow, client_id, token):
    return await flow.client.post(
        "/revoke", data={"token": token, "client_id": client_id, "client_secret": ""}
    )


def served_by(imap, before):
    """Tokens of the IMAP sessions that served a LIST since `before` calls were counted."""
    return [
        session.token
        for session in imap
        for call in session.calls[before.get(id(session), 0) :]
        if call == "list"
    ]


def snapshot(imap):
    return {id(session): len(session.calls) for session in imap}


def test_independent_authorizations_never_share_imap_connections(imap):
    async def scenario(flow):
        _, first = await flow.sign_in("yc")
        _, second = await flow.sign_in("yd")
        seen = []
        for token in (first, second, first, second):
            before = snapshot(imap)
            result = await call_tool(flow, token["access_token"], "list_folders")
            assert not result.get("isError"), result
            seen.append(served_by(imap, before))
        return seen

    seen, _ = run_flow(scenario, yandex=two_logins_of_ivan())
    # Each call of a sign-in is served by a session logged in with its own Yandex token,
    # the second time from its own pool.
    assert seen == [["ya-access-yc"], ["ya-access-yd"], ["ya-access-yc"], ["ya-access-yd"]]
    assert sorted(session.token for session in imap) == ["ya-access-yc", "ya-access-yd"]


def test_revoke_ends_every_token_of_the_authorization_and_only_them(imap):
    async def scenario(flow):
        first_client, first = await flow.sign_in("yc")
        refreshed = (await refresh(flow, first_client, first)).json()
        _, second = await flow.sign_in("yd")
        await call_tool(flow, first["access_token"], "list_folders")
        await call_tool(flow, second["access_token"], "list_folders")
        first_sessions = [session for session in imap if session.token == "ya-access-yc"]
        revoked = await revoke(flow, first_client, first["access_token"])
        return {
            "revoked": revoked,
            "first_sessions": first_sessions,
            "old_access": await call_tool(flow, first["access_token"], "list_folders"),
            "refreshed_access": await call_tool(flow, refreshed["access_token"], "list_folders"),
            "refresh": await refresh(flow, first_client, first),
            "refresh_of_refreshed": await refresh(flow, first_client, refreshed),
            "second": await call_tool(flow, second["access_token"], "list_folders"),
        }

    seen, yandex = run_flow(scenario, yandex=two_logins_of_ivan())
    assert seen["revoked"].status_code == 200
    assert yandex.revoked == ["ya-access-yc"]
    # Every token of the revoked sign-in, the refreshed pair included, is refused here,
    # whatever Yandex does with its own token.
    assert seen["old_access"].status_code == 401
    assert seen["refreshed_access"].status_code == 401
    assert seen["refresh"].json()["error"] == "invalid_grant"
    assert seen["refresh_of_refreshed"].json()["error"] == "invalid_grant"
    # Its IMAP sessions are logged out, not left for the next caller.
    assert seen["first_sessions"] and all(s.closed for s in seen["first_sessions"])
    # The user's other sign-in goes on, logged in with its own token.
    assert not seen["second"].get("isError"), seen["second"]
    assert all(s.closed for s in imap if s.token == "ya-access-yc")


def test_logout_ends_the_connection_and_a_new_sign_in_does_not_revive_it(imap):
    async def scenario(flow):
        client_id, old = await flow.sign_in("yc")
        await call_tool(flow, old["access_token"], "list_folders")
        signed_out = await call_tool(flow, old["access_token"], "logout")
        _, new = await flow.sign_in("yc")
        return {
            "signed_out": signed_out,
            "old_access": await call_tool(flow, old["access_token"], "list_folders"),
            "old_refresh": await refresh(flow, client_id, old),
            "new": await call_tool(flow, new["access_token"], "list_folders"),
            "old_after_new": await call_tool(flow, old["access_token"], "list_folders"),
        }

    # Yandex hands out the same token again after the new sign-in: only the server's
    # own record keeps the old MCP tokens dead.
    seen, yandex = run_flow(scenario)
    assert json.loads(seen["signed_out"]["content"][0]["text"]) == {
        "signed_out": "ivan",
        "revoked": True,
        "yandex_revoked": True,
    }
    assert yandex.revoked == ["ya-access-yc"]
    assert seen["old_access"].status_code == 401
    assert seen["old_refresh"].json()["error"] == "invalid_grant"
    assert not seen["new"].get("isError"), seen["new"]
    assert seen["old_after_new"].status_code == 401


def test_revocation_survives_a_restart(tmp_path):
    clock = Clock()
    cipher = TokenCipher(list(_raw_keys(generate_key())), clock)
    db = str(tmp_path / "auth.db")
    grant = {
        "authorization": "a" * 32,
        "yandex_access_token": "ya",
        "yandex_refresh_token": "yr",
        "yandex_expires_at": clock.now + 3600,
        "yandex_scope": "mail:imap_ro",
        "login": "ivan",
        "email": "ivan@yandex.ru",
    }

    def provider():
        return YandexAuthProvider(
            cipher=cipher,
            clients=ClientStore(db, cipher),
            oauth=FakeYandex(),
            public_url=PUBLIC,
            allow_write=False,
            allowed_users=frozenset({"ivan"}),
            clock=clock,
            authorizations=AuthorizationStore(db, clock),
        )

    before = provider()
    issued = before._issue("client", ["mail.read"], grant)
    assert asyncio.run(before.load_access_token(issued.access_token)) is not None
    asyncio.run(before.revoke_authorization("a" * 32, "ivan"))
    before.clients.close()
    before.authorizations.close()
    after = provider()
    assert asyncio.run(after.load_access_token(issued.access_token)) is None
    after.clients.close()
    after.authorizations.close()
    # The record goes once the last token it could stop has expired anyway.
    clock.now += 31 * 24 * 3600
    later = AuthorizationStore(db, clock)
    assert not later.is_revoked("a" * 32)
    later.close()


def test_tokens_issued_without_an_authorization_id_are_refused():
    clock = Clock()
    cipher = TokenCipher(list(_raw_keys(generate_key())), clock)
    provider = YandexAuthProvider(
        cipher=cipher,
        clients=ClientStore(":memory:", cipher),
        oauth=FakeYandex(),
        public_url=PUBLIC,
        allow_write=False,
        allowed_users=frozenset({"ivan"}),
        clock=clock,
    )
    legacy = {
        "yandex_access_token": "ya",
        "yandex_refresh_token": "yr",
        "yandex_expires_at": clock.now + 3600,
        "yandex_scope": "mail:imap_ro",
        "login": "ivan",
        "email": "ivan@yandex.ru",
    }
    issued = provider._issue("client", ["mail.read"], legacy)
    assert asyncio.run(provider.load_access_token(issued.access_token)) is None
    client = SimpleNamespace(client_id="client")
    assert asyncio.run(provider.load_refresh_token(client, issued.refresh_token)) is None
    provider.clients.close()
    provider.authorizations.close()


# --- Changes waiting for confirmation when the connection is signed out ----------------


@pytest.fixture
def write_provider(monkeypatch):
    """A real provider behind the tools, with an in-memory revocation list."""
    cipher = TokenCipher(list(_raw_keys(generate_key())))
    pending = PendingActions()
    provider = YandexAuthProvider(
        cipher=cipher,
        clients=ClientStore(":memory:", cipher),
        oauth=FakeYandex(scope=FULL),
        public_url=PUBLIC,
        allow_write=True,
        allowed_users=frozenset({"ivan"}),
        pending=pending,
    )
    provider.on_revoke = server._close_user_mailboxes
    imap = WriteIMAP()

    @contextmanager
    def connect(self, *_args, **_kwargs):
        yield imap

    monkeypatch.setattr(mail.Mailbox, "connect", connect)
    monkeypatch.setattr(server, "_mailboxes", {})
    monkeypatch.setattr(server, "_mailbox_used", {})
    monkeypatch.setattr(server, "_settings", lambda: Settings(allow_write=True))
    monkeypatch.setattr(server, "_auth_provider", provider)
    monkeypatch.setattr(server, "_confirmations", pending)
    yield SimpleNamespace(provider=provider, pending=pending, imap=imap)
    provider.clients.close()
    provider.authorizations.close()


class SignedOutWhileAsked:
    """An MCP client whose user approves, but whose connection is signed out meanwhile."""

    session = SimpleNamespace(
        check_client_capability=lambda capability: True, can_send_request=True
    )

    def __init__(self, provider):
        self.provider = provider

    async def elicit(self, message, schema):
        await self.provider.revoke_authorization("auth-1", "ivan")
        return SimpleNamespace(action="accept", data=SimpleNamespace(approve=True))


class NoElicitation:
    session = SimpleNamespace(
        check_client_capability=lambda capability: False, can_send_request=True
    )


def test_approval_after_the_connection_was_signed_out_changes_nothing(write_provider):
    with signed_in(FULL), pytest.raises(ToolError, match="signed out; nothing was changed"):
        asyncio.run(server.mark_read(3, SignedOutWhileAsked(write_provider.provider)))
    assert write_provider.imap.changes == []


def test_browser_confirmation_of_a_revoked_connection_is_dropped(write_provider):
    with signed_in(FULL):
        parked = asyncio.run(server.mark_read(3, NoElicitation()))
    action_id = parked["confirm_url"].rsplit("/", 1)[1]
    waiting = write_provider.pending.get(action_id)
    assert waiting.authorization == "auth-1"
    asyncio.run(write_provider.provider.revoke_authorization("auth-1", "ivan"))
    assert write_provider.pending.get(action_id) is None
    # Even an approval already on its way changes nothing.
    with pytest.raises(ConfirmationFailed, match="signed out"):
        waiting.action()
    assert write_provider.imap.changes == []


def test_confirmation_page_of_a_revoked_connection_changes_nothing(write_provider):
    with signed_in(FULL):
        parked = asyncio.run(server.mark_read(3, NoElicitation()))
    action_id = parked["confirm_url"].rsplit("/", 1)[1]
    # Revoked in the list only, as by another server path: the page still refuses.
    write_provider.provider.authorizations.revoke("auth-1")
    request = SimpleNamespace(path_params={"action_id": action_id}, cookies={}, method="GET")
    page = asyncio.run(write_provider.provider.confirm(request))
    assert page.status_code == 404
    assert b"signed out" in page.body
    assert write_provider.imap.changes == []


class RevokedJustBeforeTheLock:
    """The registry lock, with the revocation landing just before a request takes it.

    That is the latest moment a revocation can race a request that already passed
    the token check: the revocation is recorded and the user's pools closed, then the
    request gets the lock.
    """

    def __init__(self, provider, lock):
        self.provider = provider
        self.lock = lock
        self.revoked = False

    def __enter__(self):
        if not self.revoked:
            self.revoked = True
            self.provider.authorizations.revoke("auth-1")
            server._close_user_mailboxes("auth-1", "ivan")  # Takes the lock itself.
        return self.lock.__enter__()

    def __exit__(self, *exc):
        return self.lock.__exit__(*exc)


def test_request_admitted_before_the_revocation_opens_no_new_pool(write_provider, monkeypatch):
    lock = RevokedJustBeforeTheLock(write_provider.provider, server._mailboxes_lock)
    monkeypatch.setattr(server, "_mailboxes_lock", lock)
    with signed_in(FULL), pytest.raises(ToolError, match="signed out"):
        server.list_folders()
    assert lock.revoked
    assert server._mailboxes == {}


# --- logout and idle pools -------------------------------------------------------------


def test_logout_revokes_here_even_when_yandex_cannot_be_reached(write_provider, monkeypatch):
    class Unreachable(FakeYandex):
        def revoke(self, token):
            raise YandexOAuthError("unavailable", "no route")

    monkeypatch.setattr(server, "_yandex_oauth", Unreachable())
    with signed_in(FULL):
        server._mailbox()
        result = asyncio.run(server.logout())
    assert result["revoked"] is True and result["yandex_revoked"] is False
    assert "Yandex ID" in result["message"]
    assert write_provider.provider.authorization_revoked("auth-1")
    assert server._mailboxes == {}


def test_pools_of_abandoned_authorizations_are_closed(imap, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])
    with signed_in("mail:imap_ro", authorization="old"):
        server._mailbox().folders()
    now[0] += 299
    with signed_in("mail:imap_ro", authorization="new"):
        server._mailbox()
    server._sweep_idle_mailboxes()
    assert server._authorization_key("ivan", "old", "mail:imap_ro") in server._mailboxes
    # Past YANDEX_IMAP_IDLE_TIMEOUT since the old pool was last used; no other request
    # is needed for it to go.
    now[0] += 2
    server._sweep_idle_mailboxes()
    assert server._authorization_key("ivan", "old", "mail:imap_ro") not in server._mailboxes
    assert server._authorization_key("ivan", "new", "mail:imap_ro") in server._mailboxes
    assert imap[0].closed


def test_idle_timeout_of_zero_keeps_busy_pools_of_other_sign_ins(imap, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(server, "_settings", lambda: Settings(imap_idle_timeout=0))
    with signed_in("mail:imap_ro", authorization="first"):
        first = server._mailbox()
    now[0] += 30
    with signed_in("mail:imap_ro", authorization="second"):
        server._mailbox()
    server._sweep_idle_mailboxes()
    with signed_in("mail:imap_ro", authorization="first"):
        assert server._mailbox().pool is first.pool
    now[0] += server.MIN_POOL_IDLE + 1
    server._sweep_idle_mailboxes()
    assert server._mailboxes == {}


def test_the_server_sweeps_idle_pools_while_it_runs(imap, monkeypatch):
    monkeypatch.setattr(server, "SWEEP_INTERVAL", 0.01)
    monkeypatch.setattr(server, "MIN_POOL_IDLE", 0.0)
    monkeypatch.setattr(server, "_settings", lambda: Settings(imap_idle_timeout=0))
    with signed_in("mail:imap_ro", authorization="old"):
        server._mailbox().folders()

    async def run():
        async with server._lifespan(server.mcp):
            for _ in range(200):
                if not server._mailboxes:
                    return True
                await asyncio.sleep(0.01)
            return False

    assert asyncio.run(run())
    assert imap[0].closed
