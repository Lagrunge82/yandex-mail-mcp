"""The browser confirmation accepts a decision only from its own page in the user's browser.

The page's cookie is SameSite=Lax, which a page on a sibling subdomain of the same
site still gets. So a decision must also come from this server's origin and carry
the form token issued to this browser for this change: knowing the confirm_url is
not enough to decide on the user's behalf.
"""

import re

import httpx2 as httpx
import pytest
from starlette.requests import Request
from test_auth_server import PUBLIC, Clock, FakeYandex, _raw_keys, query_of, run_flow
from test_mutations import WriteIMAP
from test_permissions import signed_in
from test_write_consent import FULL, Client, decide, github_folders, user_mailbox

from yandex_mail_mcp import server
from yandex_mail_mcp.auth_server import ClientStore, YandexAuthProvider
from yandex_mail_mcp.config import Settings
from yandex_mail_mcp.crypto import TokenCipher, generate_key


@pytest.fixture(autouse=True)
def allowed_users(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_USERS", "ivan")
    monkeypatch.delenv("MCP_AUTH_RATE_LIMIT", raising=False)


def token_of(page):
    match = re.search(r"name=csrf value='([^']*)'", page.text)
    return match.group(1) if match else None


def run(scenario, monkeypatch):
    """Sign the browser in, park a move for it, and run the scenario on that link."""
    imap = WriteIMAP(folders=github_folders())

    async def main(flow):
        await flow.sign_in()
        with user_mailbox(monkeypatch, imap), signed_in(FULL):

            async def park():
                parked = await server.move_email(3, "github", Client(elicitation=False))
                return parked["confirm_url"]

            return await scenario(flow, park)

    result, _ = run_flow(main, settings=Settings(allow_write=True), yandex=FakeYandex(scope=FULL))
    return result, imap


def test_decision_from_the_page_with_its_token_is_carried_out(monkeypatch):
    async def scenario(flow, park):
        url = await park()
        page = await flow.client.get(url)
        approved = await decide(flow.client, url, "approve")
        rejected = await decide(flow.client, await park(), "reject")
        return page, approved, rejected

    (page, approved, rejected), imap = run(scenario, monkeypatch)
    assert token_of(page), "the form carries a token"
    assert "Done." in approved.text
    assert "Rejected. Nothing was changed." in rejected.text
    assert imap.changes == [("move", [3], "github")]


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.127.0.0.1:8000",  # A sibling subdomain: the Lax cookie still goes along.
        "https://127.0.0.1:8000",  # Another scheme.
        "http://127.0.0.1:9000",  # Another port.
        "null",  # A sandboxed frame or a redirect chain.
    ],
)
def test_decision_posted_from_another_origin_changes_nothing(monkeypatch, origin):
    async def scenario(flow, park):
        url = await park()
        token = token_of(await flow.client.get(url))
        forged = await flow.client.post(
            url, data={"decision": "approve", "csrf": token}, headers={"Origin": origin}
        )
        # The change still waits for the user, who can approve it from the page.
        later = await decide(flow.client, url, "approve")
        return forged, later

    (forged, later), imap = run(scenario, monkeypatch)
    assert forged.status_code == 403
    assert "did not come from this server" in forged.text
    assert "Done." in later.text
    assert imap.changes == [("move", [3], "github")], "once, from the page"


def test_decision_without_origin_or_referer_changes_nothing(monkeypatch):
    async def scenario(flow, park):
        url = await park()
        token = token_of(await flow.client.get(url))
        return await flow.client.post(url, data={"decision": "approve", "csrf": token})

    response, imap = run(scenario, monkeypatch)
    assert response.status_code == 403
    assert imap.changes == []


def test_referer_of_this_server_stands_in_for_a_dropped_origin(monkeypatch):
    async def scenario(flow, park):
        url = await park()
        token = token_of(await flow.client.get(url))
        return await flow.client.post(
            url, data={"decision": "approve", "csrf": token}, headers={"Referer": url}
        )

    response, imap = run(scenario, monkeypatch)
    assert "Done." in response.text
    assert imap.changes == [("move", [3], "github")]


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.parametrize("token", ["missing", "forged", "of another change"])
def test_decision_without_this_changes_token_changes_nothing(monkeypatch, token, decision):
    async def scenario(flow, park):
        url, other = await park(), await park()
        data = {"decision": decision}
        if token == "forged":
            data["csrf"] = "v1.k1.forged"
        elif token == "of another change":
            data["csrf"] = token_of(await flow.client.get(other))
        response = await flow.client.post(url, data=data, headers={"Origin": PUBLIC})
        # A refused reject does not cancel the change either: it still waits.
        page = await flow.client.get(url)
        return response, page

    (response, page), imap = run(scenario, monkeypatch)
    assert response.status_code == 403
    assert "reload the link" in response.text
    assert "Approve" in page.text
    assert imap.changes == []


def test_token_of_another_browser_of_the_same_user_changes_nothing(monkeypatch):
    async def scenario(flow, park):
        url = await park()
        token = token_of(await flow.client.get(url))
        # The same user signs in to the page from a second browser.
        other = httpx.AsyncClient(transport=flow.client._transport, base_url=PUBLIC)
        async with other:
            state = query_of((await other.get(url)).headers["location"])["state"]
            await other.get("/oauth/yandex/callback", params={"state": state, "code": "yc"})
            replayed = await other.post(
                url, data={"decision": "approve", "csrf": token}, headers={"Origin": PUBLIC}
            )
            own = await decide(other, url, "approve")
        return replayed, own

    (replayed, own), imap = run(scenario, monkeypatch)
    assert replayed.status_code == 403
    # Its own page works in that browser.
    assert "Done." in own.text
    assert imap.changes == [("move", [3], "github")]


def test_pages_cannot_be_framed_by_another_site(monkeypatch):
    async def scenario(flow, park):
        return await flow.client.get(await park())

    page, _ = run(scenario, monkeypatch)
    assert page.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("headers", "same"),
    [
        ({"origin": "https://mail.example.com"}, True),
        ({"origin": "https://MAIL.example.com"}, True),
        ({"referer": "https://mail.example.com/confirm/x"}, True),
        ({"origin": "https://evil.example.com"}, False),
        ({"origin": "https://mail.example.com.evil.net"}, False),
        ({"origin": "http://mail.example.com"}, False),
        ({"origin": "null", "referer": "https://mail.example.com/confirm/x"}, False),
        ({}, False),
    ],
)
def test_same_origin_matches_only_the_public_url(headers, same):
    cipher = TokenCipher(list(_raw_keys(generate_key())), Clock())
    provider = YandexAuthProvider(
        cipher=cipher,
        clients=ClientStore(":memory:", cipher),
        oauth=FakeYandex(),
        public_url="https://mail.example.com/",
        allow_write=True,
        allowed_users=frozenset({"ivan"}),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "headers": [(name.encode(), value.encode()) for name, value in headers.items()],
        }
    )
    assert provider._same_origin(request) is same
    provider.clients.close()
    provider.revoked.close()
