"""Each request is checked against its own credentials, whatever other requests bring.

Requests share an authorization's IMAP pool, but the shared Mailbox is never changed:
a change waiting for confirmation runs with the credentials of the request that asked
for it, even after another request of the same authorization, or a read-only client,
has used the server in between.
"""

import asyncio
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_permissions import signed_in
from test_pool import FakeIMAP
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


def test_new_connections_log_in_with_the_requests_own_token(monkeypatch):
    FakeIMAP.instances = []
    monkeypatch.setattr(mail, "IMAPClient", TokenIMAP)
    monkeypatch.setattr(server, "_mailboxes", {})
    monkeypatch.setattr(server, "_mailbox_used", {})
    monkeypatch.setattr(server, "_settings", lambda: Settings())
    with signed_in(READ_ONLY, token="ya-first"):
        first = server._mailbox()
    with signed_in(READ_ONLY, token="ya-refreshed"):
        refreshed = server._mailbox()
    assert first.pool is refreshed.pool
    # Both connections at once, so neither can reuse the other's.
    with first.connect() as one, refreshed.connect() as other:
        assert (one.token, other.token) == ("ya-first", "ya-refreshed")
    assert first.credentials.source.access_token() == "ya-first"
