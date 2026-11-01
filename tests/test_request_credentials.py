"""Each request is checked against its own credentials, whatever other requests bring.

Requests share an authorization's IMAP pool, but the shared Mailbox is never changed:
a change waiting for confirmation runs with the credentials of the request that asked
for it, even after another request of the same authorization, or a read-only client,
has used the server in between.
"""

import asyncio
from types import SimpleNamespace
from typing import ClassVar

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_mutations import HEADER
from test_permissions import signed_in
from test_revocation import TokenIMAP, write_provider  # noqa: F401 (fixture)

from yandex_mail_mcp import mail, server
from yandex_mail_mcp.config import Settings

FULL = "mail:imap_full login:info login:email"
READ_ONLY = "mail:imap_ro login:info login:email"


def read_only_request(authorization="auth-1"):
    """Another request in between, with a read-only Yandex token."""
    with signed_in(READ_ONLY, token="ya-read-only", authorization=authorization):
        server._mailbox()


class AnotherRequestWhileAsked:
    """An MCP client whose user approves after another request has used the server."""

    session = SimpleNamespace(
        check_client_capability=lambda capability: True, can_send_request=True
    )

    def __init__(self, authorization):
        self.authorization = authorization
        self.asked = []

    async def elicit(self, message, schema):
        self.asked.append(message)
        read_only_request(self.authorization)
        return SimpleNamespace(action="accept", data=SimpleNamespace(approve=True))


class NoElicitation:
    session = SimpleNamespace(
        check_client_capability=lambda capability: False, can_send_request=True
    )


@pytest.mark.parametrize("authorization", ["auth-1", "auth-2"])
def test_elicited_change_is_checked_against_the_request_that_asked(
    write_provider,  # noqa: F811
    authorization,
):
    # auth-1: the same authorization with another token; auth-2: another client.
    client = AnotherRequestWhileAsked(authorization)
    with signed_in(FULL):
        result = asyncio.run(server.mark_read(3, client))
    assert client.asked, "the user was asked"
    assert result["uid"] == 3
    assert write_provider.imap.changes == [("add_flags", [3], ["\\Seen"])]


@pytest.mark.parametrize("authorization", ["auth-1", "auth-2"])
def test_browser_approval_is_checked_against_the_request_that_asked(
    write_provider,  # noqa: F811
    authorization,
):
    with signed_in(FULL):
        parked = asyncio.run(server.mark_read(3, NoElicitation()))
    read_only_request(authorization)
    action_id = parked["confirm_url"].rsplit("/", 1)[1]
    waiting = write_provider.pending.claim(action_id, "ivan", "approve")
    assert waiting.action()["uid"] == 3
    assert write_provider.imap.changes == [("add_flags", [3], ["\\Seen"])]


def test_read_only_request_still_cannot_write_after_a_full_access_one(
    write_provider,  # noqa: F811
):
    with signed_in(FULL):
        server._mailbox()
    with signed_in(READ_ONLY), pytest.raises(ToolError, match="read-only"):
        asyncio.run(server.mark_read(3, AnotherRequestWhileAsked("auth-1")))
    assert write_provider.imap.changes == []


class TokenWriteIMAP(TokenIMAP):
    """A TokenIMAP that answers the header fetch and search a flag change needs."""

    changes: ClassVar[list] = []

    def fetch(self, uids, fields):
        return {uid: {b"BODY[HEADER]": HEADER} for uid in uids}

    def search(self, criteria, charset=None):
        return [3]

    def add_flags(self, uids, flags):
        TokenWriteIMAP.changes.append((self.token, uids, flags))


@pytest.fixture
def real_pool(monkeypatch):
    """The real connection pool behind the server, with sessions that remember tokens."""
    TokenWriteIMAP.changes = []
    monkeypatch.setattr(mail, "IMAPClient", TokenWriteIMAP)
    monkeypatch.setattr(server, "_mailboxes", {})
    monkeypatch.setattr(server, "_mailbox_used", {})
    monkeypatch.setattr(server, "_settings", lambda: Settings(allow_write=True))
    return TokenWriteIMAP.changes


def test_new_connections_log_in_with_the_requests_own_token(real_pool):
    with signed_in(READ_ONLY, token="ya-first"):
        first = server._mailbox()
    with signed_in(READ_ONLY, token="ya-refreshed"):
        refreshed = server._mailbox()
    assert first.pool is refreshed.pool
    # Both connections at once, so neither can reuse the other's.
    with first.connect() as one, refreshed.connect() as other:
        assert (one.token, other.token) == ("ya-first", "ya-refreshed")
    assert first.credentials.source.access_token() == "ya-first"


def test_tokens_of_different_scopes_do_not_share_a_pool(real_pool):
    # Within a pool idle connections are shared, so a pool holds tokens of one scope
    # only: a connection logged in read-only never serves a full-access request.
    with signed_in(READ_ONLY, token="ya-read-only"):
        read_only = server._mailbox()
        read_only.folders()
    with signed_in(FULL, token="ya-full"):
        full = server._mailbox()
        with full.connect() as imap:
            assert imap.token == "ya-full"
    assert read_only.pool is not full.pool


def test_approved_write_logs_in_with_the_token_of_the_request_that_asked(real_pool):
    # The pool is empty when the user approves, so the write opens a connection, and
    # it must log in with the token of the request that asked, not the later one.
    client = AnotherRequestWhileAsked("auth-1")
    with signed_in(FULL, token="ya-asked"):
        result = asyncio.run(server.mark_read(3, client))
    assert client.asked and result["uid"] == 3
    assert real_pool == [("ya-asked", [3], ["\\Seen"])]


def test_mailbox_over_a_shared_pool_does_not_close_it(real_pool):
    with signed_in(READ_ONLY, token="ya-first"):
        request = server._mailbox()
    (shared,) = server._mailboxes.values()
    request.close()
    with request.connect() as imap:
        pass
    assert not imap.closed, "the shared pool still keeps connections"
    shared.close()
    assert imap.closed
