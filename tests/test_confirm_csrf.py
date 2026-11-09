"""The browser confirmation accepts a decision only from its own page in the user's browser.

The page's cookie is SameSite=Lax, which a page on a sibling subdomain of the same
site still gets. So a decision must also come from this server's origin and carry
the form token issued to this browser for this change: knowing the confirm_url is
not enough to decide on the user's behalf.
"""

import re
import time

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
        "http://[evil",  # Malformed: refused, not a server error.
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


def test_page_opened_before_a_new_sign_in_in_the_same_browser_still_works(monkeypatch):
    async def scenario(flow, park):
        url = await park()
        page = await flow.client.get(url)
        # The same browser connects another MCP client, which sets a new cookie.
        await flow.sign_in()
        return await flow.client.post(
            url,
            data={"decision": "approve", "csrf": token_of(page)},
            headers={"Origin": PUBLIC},
        )

    response, imap = run(scenario, monkeypatch)
    assert "Done." in response.text
    assert imap.changes == [("move", [3], "github")]


def test_form_token_expires_with_the_change(monkeypatch):
    async def scenario(flow, park):
        url = await park()
        # Five of the change's ten minutes pass before the page is opened.
        monkeypatch.setattr(server._confirmations, "_clock", lambda: time.time() + 300)
        token = token_of(await flow.client.get(url))
        return flow.provider.cipher.open(token, "csrf")["exp"] - time.time()

    token_lifetime, _ = run(scenario, monkeypatch)
    assert 290 <= token_lifetime <= 301, "the token lives as long as the change has left"


def test_pages_cannot_be_framed_by_another_site(monkeypatch):
    async def scenario(flow, park):
        return await flow.client.get(await park())

    page, _ = run(scenario, monkeypatch)
    assert page.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["cache-control"] == "no-store"


@pytest.fixture
def provider_at():
    """Providers at a given public URL, with their stores closed whatever the test does."""
    providers = []

    def build(public_url):
        cipher = TokenCipher(list(_raw_keys(generate_key())), Clock())
        providers.append(
            YandexAuthProvider(
                cipher=cipher,
                clients=ClientStore(":memory:", cipher),
                oauth=FakeYandex(),
                public_url=public_url,
                allow_write=True,
                allowed_users=frozenset({"ivan"}),
            )
        )
        return providers[-1]

    yield build
    for provider in providers:
        provider.clients.close()
        provider.authorizations.close()


def posted(headers):
    return Request(
        {
            "type": "http",
            "method": "POST",
            "headers": [(name.encode(), value.encode()) for name, value in headers.items()],
        }
    )


@pytest.mark.parametrize(
    ("headers", "same"),
    [
        ({"origin": "https://mail.example.com"}, True),
        ({"origin": "https://MAIL.example.com"}, True),
        ({"origin": "https://mail.example.com:443"}, True),
        ({"referer": "https://mail.example.com/confirm/x"}, True),
        ({"origin": "https://evil.example.com"}, False),
        ({"origin": "https://mail.example.com.evil.net"}, False),
        ({"origin": "http://mail.example.com"}, False),
        ({"origin": "https://mail.example.com:8443"}, False),
        ({"origin": "null", "referer": "https://mail.example.com/confirm/x"}, False),
        ({"origin": "http://[evil"}, False),
        ({"origin": "https://mail.example.com:99999"}, False),
        ({"origin": "javascript:alert(1)"}, False),
        ({}, False),
        # Sec-Fetch-Site: "same-origin" stands in for a nulled or missing Origin only.
        ({"sec-fetch-site": "same-origin", "origin": "null"}, True),
        ({"sec-fetch-site": "same-origin"}, True),
        ({"sec-fetch-site": " Same-Origin ", "origin": "null"}, True),
        ({"sec-fetch-site": "same-origin", "origin": "https://mail.example.com"}, True),
        ({"sec-fetch-site": "same-origin", "origin": "https://evil.example.com"}, False),
        ({"sec-fetch-site": "same-origin", "origin": "http://mail.example.com"}, False),
        # Anything else is refused, whatever Origin says.
        ({"sec-fetch-site": "same-site", "origin": "https://mail.example.com"}, False),
        ({"sec-fetch-site": "cross-site", "origin": "https://mail.example.com"}, False),
        ({"sec-fetch-site": "none", "origin": "https://mail.example.com"}, False),
        ({"sec-fetch-site": "same-site", "origin": "null"}, False),
        ({"sec-fetch-site": "unknown", "origin": "https://mail.example.com"}, False),
        # An empty header is no header: Origin decides as before.
        ({"sec-fetch-site": "", "origin": "https://mail.example.com"}, True),
        ({"sec-fetch-site": "", "origin": "null"}, False),
    ],
)
def test_same_origin_matches_only_the_public_url(provider_at, headers, same):
    provider = provider_at("https://mail.example.com/")
    assert provider._same_origin(posted(headers)) is same


@pytest.mark.parametrize(
    ("public_url", "origin"),
    [
        ("https://mail.example.com:443", "https://mail.example.com"),
        ("https://mail.example.com", "https://mail.example.com:443"),
        ("http://mail.example.com:80", "http://mail.example.com"),
    ],
)
def test_default_port_written_out_or_not_is_the_same_origin(provider_at, public_url, origin):
    provider = provider_at(public_url)
    assert provider._same_origin(posted({"origin": origin}))


def test_same_origin_fetch_metadata_passes_when_a_referrer_policy_nulls_origin(monkeypatch):
    """Referrer-Policy: no-referrer (browser setting or reverse proxy) makes a browser
    post its own page's form with Origin: null and no Referer; Sec-Fetch-Site still
    says the form came from this server's page."""

    async def scenario(flow, park):
        url = await park()
        token = token_of(await flow.client.get(url))
        return await flow.client.post(
            url,
            data={"decision": "approve", "csrf": token},
            headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"},
        )

    response, imap = run(scenario, monkeypatch)
    assert "Done." in response.text
    assert imap.changes == [("move", [3], "github")]


@pytest.mark.parametrize("fetch_site", ["same-site", "cross-site", "none"])
def test_fetch_metadata_other_than_same_origin_changes_nothing(monkeypatch, fetch_site):
    """A sibling subdomain is same-site: it gets the Lax cookie, but not the decision,
    even if it could somehow present this server's Origin."""

    async def scenario(flow, park):
        url = await park()
        token = token_of(await flow.client.get(url))
        forged = await flow.client.post(
            url,
            data={"decision": "approve", "csrf": token},
            headers={"Origin": PUBLIC, "Sec-Fetch-Site": fetch_site},
        )
        # The change still waits for the user, who can approve it from the page.
        later = await decide(flow.client, url, "approve")
        return forged, later

    (forged, later), imap = run(scenario, monkeypatch)
    assert forged.status_code == 403
    assert "did not come from this server" in forged.text
    assert "Done." in later.text
    assert imap.changes == [("move", [3], "github")], "once, from the page"
