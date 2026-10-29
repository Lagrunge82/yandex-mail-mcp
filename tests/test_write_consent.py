"""Write access: scope negotiation, the consent page, upgrades and browser confirmation."""

import asyncio
import re
from contextlib import contextmanager
from types import SimpleNamespace

import anyio.to_thread
import httpx2 as httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_auth_server import CHALLENGE, PUBLIC, REDIRECT, FakeYandex, query_of, run_flow
from test_mutations import WriteIMAP
from test_permissions import signed_in

from yandex_mail_mcp import server
from yandex_mail_mcp.config import Settings
from yandex_mail_mcp.confirmations import ConfirmationFailed, PendingActions
from yandex_mail_mcp.credentials import OAuthCredentials, StaticTokenSource
from yandex_mail_mcp.mail import Mailbox, MailLookupError

FULL = "mail:imap_full login:info login:email"
READ_ONLY = "mail:imap_ro login:info login:email"
PRM = "/.well-known/oauth-protected-resource/mcp"


@pytest.fixture(autouse=True)
def allowed_users(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_USERS", "ivan")
    monkeypatch.delenv("MCP_AUTH_RATE_LIMIT", raising=False)


@pytest.fixture(autouse=True)
def run_sync_inline(monkeypatch):
    async def run_sync(fn, *args, **kwargs):
        return fn(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", run_sync)


class Client:
    """An MCP client; elicitation support is a parameter."""

    def __init__(self, elicitation=True, approve=True):
        self.session = SimpleNamespace(check_client_capability=lambda capability: elicitation)
        self.approve = approve
        self.messages = []

    async def elicit(self, message, schema):
        self.messages.append(message)
        return SimpleNamespace(action="accept", data=SimpleNamespace(approve=self.approve))


def github_folders():
    return [
        ((b"\\HasNoChildren",), b"|", "INBOX"),
        ((b"\\HasChildren",), b"|", "github"),
        ((b"\\HasNoChildren",), b"|", "github|ci/cd"),
        ((b"\\HasNoChildren", b"\\Trash"), b"|", "Удалённые"),
    ]


@contextmanager
def user_mailbox(monkeypatch, imap):
    @contextmanager
    def connect(self):
        yield imap

    monkeypatch.setattr(Mailbox, "connect", connect)
    monkeypatch.setattr(server, "_mailboxes", {})
    monkeypatch.setattr(server, "_settings", lambda: Settings(allow_write=True))
    yield imap


# --- Scope negotiation -------------------------------------------------------------


def test_resource_metadata_advertises_the_scopes_the_server_offers():
    async def scenario(flow):
        return (await flow.client.get(PRM)).json()["scopes_supported"]

    read_only, _ = run_flow(scenario)
    writable, _ = run_flow(scenario, settings=Settings(allow_write=True))
    assert read_only == ["mail.read"]
    assert writable == ["mail.read", "mail.write"]


def test_client_asking_only_for_read_gets_read_without_a_consent_page():
    async def scenario(flow):
        _, tokens = await flow.sign_in(scope="mail.read")
        return tokens, flow.consent_shown

    (tokens, consent_shown), yandex = run_flow(
        scenario, settings=Settings(allow_write=True), yandex=FakeYandex(scope=FULL)
    )
    assert not consent_shown
    assert yandex.authorized == [READ_ONLY]
    assert tokens["scope"] == "mail.read"


@pytest.mark.parametrize("scope", [None, "mail.read mail.write"])
def test_client_asking_for_write_sees_the_consent_page_and_the_user_decides(scope):
    async def scenario(flow):
        _, read = await flow.sign_in(access="read", scope=scope)
        read_shown = flow.consent_shown
        _, write = await flow.sign_in(access="write", scope=scope)
        return read, write, read_shown, flow.consent_shown

    (read, write, read_shown, write_shown), yandex = run_flow(
        scenario, settings=Settings(allow_write=True), yandex=FakeYandex(scope=FULL)
    )
    assert read_shown and write_shown
    # "Read only" on the page keeps Yandex at read-only and the token at mail.read,
    # although the client asked for mail.write and Yandex would have granted full access.
    assert yandex.authorized == [READ_ONLY, FULL]
    assert read["scope"] == "mail.read"
    assert write["scope"] == "mail.read mail.write"


def test_write_is_never_offered_without_the_server_opt_in():
    async def scenario(flow):
        client_id = await flow.register()
        explicit = await flow.client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": REDIRECT,
                "code_challenge": CHALLENGE,
                "code_challenge_method": "S256",
                "state": "s",
                "scope": "mail.read mail.write",
            },
        )
        _, tokens = await flow.sign_in()
        return explicit, tokens, flow.consent_shown

    (explicit, tokens, consent_shown), yandex = run_flow(scenario, yandex=FakeYandex(scope=FULL))
    assert query_of(explicit.headers["location"])["error"] == "invalid_scope"
    assert not consent_shown
    assert tokens["scope"] == "mail.read"
    assert yandex.authorized == [READ_ONLY]


def test_yandex_granting_read_only_despite_the_users_choice_gives_a_read_token():
    async def scenario(flow):
        return (await flow.sign_in(access="write"))[1]

    tokens, yandex = run_flow(
        scenario, settings=Settings(allow_write=True), yandex=FakeYandex(scope=READ_ONLY)
    )
    assert yandex.authorized == [FULL]
    assert tokens["scope"] == "mail.read"


def test_client_registered_while_writes_were_off_can_ask_for_write_later():
    async def scenario(flow):
        client_id = await flow.register()
        _, before = await flow.sign_in()
        # The operator turns writes on; the client keeps its registration.
        flow.provider.allow_write = True
        code = await flow.authorize(client_id, access="write", scope="mail.read mail.write")
        after = (await flow.token(client_id, code)).json()
        return before, after

    (before, after), yandex = run_flow(scenario, yandex=FakeYandex(scope=FULL))
    assert before["scope"] == "mail.read"
    assert after["scope"] == "mail.read mail.write"
    assert yandex.authorized[-1] == FULL


def test_read_only_connection_upgrades_only_with_consent_and_old_tokens_stay_read_only():
    async def scenario(flow):
        client_id = await flow.register()
        read = (await flow.token(client_id, await flow.authorize(client_id, access="read"))).json()
        write = (
            await flow.token(client_id, await flow.authorize(client_id, access="write"))
        ).json()
        refreshed_read = await flow.client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": read["refresh_token"],
                "client_id": client_id,
            },
        )
        escalated = await flow.client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": read["refresh_token"],
                "client_id": client_id,
                "scope": "mail.read mail.write",
            },
        )
        refreshed_write = await flow.client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": write["refresh_token"],
                "client_id": client_id,
            },
        )
        return read, write, refreshed_read, escalated, refreshed_write

    (read, write, refreshed_read, escalated, refreshed_write), _ = run_flow(
        scenario, settings=Settings(allow_write=True), yandex=FakeYandex(scope=FULL)
    )
    assert read["scope"] == "mail.read"
    assert write["scope"] == "mail.read mail.write"
    assert refreshed_read.json()["scope"] == "mail.read"
    assert escalated.json()["error"] == "invalid_scope"
    assert refreshed_write.json()["scope"] == "mail.read mail.write"


def test_write_attempt_without_the_scope_explains_how_to_get_it(monkeypatch):
    with user_mailbox(monkeypatch, WriteIMAP(folders=github_folders())) as imap:
        client = Client()
        with signed_in(FULL, scopes=["mail.read"]), pytest.raises(ToolError) as narrowed:
            asyncio.run(server.move_email(3, "github", client))
        with signed_in(READ_ONLY), pytest.raises(ToolError) as read_only:
            asyncio.run(server.move_email(3, "github", client))
    assert "read-only" in str(narrowed.value) and "reconnect" in str(narrowed.value)
    assert "read-only" in str(read_only.value) and "reconnect" in str(read_only.value)
    assert client.messages == [] and imap.changes == []


# --- Confirmation: elicitation, or the browser ----------------------------------------


def test_authorized_move_with_elicitation_keeps_flags_and_uses_the_nested_name(monkeypatch):
    with user_mailbox(monkeypatch, WriteIMAP(folders=github_folders())) as imap:
        client = Client()
        with signed_in(FULL):
            result = asyncio.run(server.move_email(3, "github|ci/cd", client))
    assert result == {"uid": 3, "folder": "github|ci/cd", "action": "move", "reversible": True}
    # One MOVE and nothing else: the server never touches the Seen or Flagged flags.
    assert imap.changes == [("move", [3], "github|ci/cd")]
    assert imap.selected[-1] == ("INBOX", False)
    assert 'To folder (requested by the assistant): "github|ci/cd"' in client.messages[0]


def test_declined_or_cancelled_confirmation_changes_nothing(monkeypatch):
    with (
        user_mailbox(monkeypatch, WriteIMAP(folders=github_folders())) as imap,
        signed_in(FULL),
        pytest.raises(ToolError, match="not approved"),
    ):
        asyncio.run(server.move_email(3, "github", Client(approve=False)))
    assert imap.changes == []


def test_folder_written_with_slash_gets_a_hint_about_the_delimiter(monkeypatch):
    with (
        user_mailbox(monkeypatch, WriteIMAP(folders=github_folders())),
        signed_in(FULL),
        pytest.raises(ToolError) as raised,
    ):
        asyncio.run(server.move_email(3, "github/ci/cd", Client()))
    assert "did you mean 'github|ci/cd'" in str(raised.value)
    with pytest.raises(MailLookupError, match=r"written with '\|'"):
        Mailbox(Settings(allow_write=True))._destination(
            SimpleNamespace(list_folders=lambda: github_folders(), has_capability=lambda c: True),
            "projects/2026",
        )


def test_move_from_a_client_without_elicitation_is_confirmed_in_the_browser(monkeypatch):
    imap = WriteIMAP(folders=github_folders())
    seen = {}

    async def scenario(flow):
        await flow.sign_in()  # The browser that signed in gets the session cookie.
        with user_mailbox(monkeypatch, imap), signed_in(FULL):
            parked = await server.move_email(3, "github|ci/cd", Client(elicitation=False))
            seen["parked"] = parked
            url = parked["confirm_url"]
            seen["page"] = await flow.client.get(url)
            seen["approved"] = await flow.client.post(url, data={"decision": "approve"})
            seen["again"] = await flow.client.post(url, data={"decision": "approve"})
            # A second change, rejected this time.
            rejected = await server.move_email(3, "github", Client(elicitation=False))
            seen["rejected"] = await flow.client.post(
                rejected["confirm_url"], data={"decision": "reject"}
            )
        return seen

    seen, _ = run_flow(scenario, settings=Settings(allow_write=True), yandex=FakeYandex(scope=FULL))
    parked = seen["parked"]
    assert parked["status"] == "confirmation_required"
    assert parked["confirm_url"].startswith(f"{PUBLIC}/confirm/")
    assert 0 < parked["expires_in_seconds"] <= 600
    assert "ivan@yandex.ru" in parked["message"]
    assert imap.changes == [("move", [3], "github|ci/cd")], "moved only once, on approval"
    page = seen["page"]
    assert page.status_code == 200
    assert "github|ci/cd" in page.text and "Approve" in page.text and "Sale" in page.text
    assert "Done." in seen["approved"].text
    assert "Done." in seen["again"].text  # The decision is final; nothing moved twice.
    assert "Rejected. Nothing was changed." in seen["rejected"].text


def test_confirmation_page_demands_the_owners_browser_session(monkeypatch):
    imap = WriteIMAP(folders=github_folders())

    async def scenario(flow):
        await flow.sign_in()
        with user_mailbox(monkeypatch, imap), signed_in(FULL):
            url = (await server.move_email(3, "github", Client(elicitation=False)))["confirm_url"]
            stranger = httpx.AsyncClient(transport=flow.client._transport, base_url=PUBLIC)
            async with stranger:
                anonymous = await stranger.get(url)
                # Posting without the cookie cannot approve either.
                blind = await stranger.post(url, data={"decision": "approve"})
                stranger.cookies.set("mcp_auth_user", "v1.k1.forged")
                forged = await stranger.get(url)
                # Signing in with Yandex in that browser makes the page available. Each
                # redirect set a fresh nonce cookie, so the newest state is the valid one.
                state = query_of(forged.headers["location"])["state"]
                signed = await stranger.get(
                    "/oauth/yandex/callback", params={"state": state, "code": "yc"}
                )
                after = await stranger.get(signed.headers["location"])
            unknown = await flow.client.get(f"{PUBLIC}/confirm/{'x' * 43}")
        return anonymous, blind, forged, signed, after, unknown

    (anonymous, blind, forged, signed, after, unknown), yandex = run_flow(
        scenario, settings=Settings(allow_write=True), yandex=FakeYandex(scope=FULL)
    )
    for response in (anonymous, blind, forged):
        assert response.status_code == 302
        location = response.headers["location"]
        assert location.startswith("https://oauth.yandex.ru/authorize")
        assert query_of(location)["scope"] == "login:info login:email"
    assert imap.changes == []
    assert signed.status_code == 302 and "/confirm/" in signed.headers["location"]
    assert after.status_code == 200 and "Approve" in after.text
    # The identity sign-in asked Yandex for no mail access and revoked nothing.
    assert yandex.authorized[-1] == "login:info login:email"
    assert yandex.revoked == []
    assert unknown.status_code == 404


def test_approved_change_that_fails_shows_the_reason(monkeypatch):
    imap = WriteIMAP(folders=github_folders())

    async def scenario(flow):
        await flow.sign_in()
        with user_mailbox(monkeypatch, imap), signed_in(FULL):
            url = (await server.move_email(3, "github", Client(elicitation=False)))["confirm_url"]
            imap.inbox.clear()  # The message is gone before the user approves.
            return await flow.client.post(url, data={"decision": "approve"})

    response, _ = run_flow(
        scenario, settings=Settings(allow_write=True), yandex=FakeYandex(scope=FULL)
    )
    assert "the change failed" in response.text
    assert "UID 3 not found" in response.text
    assert imap.changes == []


def test_local_client_without_elicitation_still_gets_the_plain_error(monkeypatch):
    monkeypatch.setattr(server, "_confirmations", None)
    monkeypatch.setattr(server, "_auth_provider", None)
    with (
        user_mailbox(monkeypatch, WriteIMAP(folders=github_folders())) as imap,
        signed_in(FULL),
        pytest.raises(ToolError, match="does not support elicitation"),
    ):
        asyncio.run(server.move_email(3, "github", Client(elicitation=False)))
    assert imap.changes == []


def test_pending_actions_expire_and_are_decided_once():
    clock = [1000.0]
    pending = PendingActions(clock=lambda: clock[0], ttl=600)
    first = pending.create(login="ivan", email="i@y", name="Move", question="q", action=lambda: {})
    assert pending.get(first.id) is first
    assert pending.claim(first.id, "petr", "approve") is None
    assert pending.claim(first.id, "IVAN", "approve") is None  # logins are compared exactly here
    assert pending.claim(first.id, "ivan", "approve") is first
    assert pending.claim(first.id, "ivan", "reject") is None
    second = pending.create(login="ivan", email="i@y", name="Move", question="q", action=lambda: {})
    clock[0] += 601
    assert pending.get(second.id) is None
    with pytest.raises(ConfirmationFailed):
        raise ConfirmationFailed("x")


# --- Diagnostics -------------------------------------------------------------------


def test_connection_capabilities_for_http_users(monkeypatch):
    with user_mailbox(monkeypatch, WriteIMAP(folders=github_folders())):
        with signed_in(READ_ONLY, scopes=["mail.read"]):
            read_only = asyncio.run(server.get_connection_capabilities(Client()))
        with signed_in(FULL):
            full = asyncio.run(server.get_connection_capabilities(Client()))
            monkeypatch.setattr(server, "_confirmations", None)
            no_browser = asyncio.run(server.get_connection_capabilities(Client(elicitation=False)))
    assert read_only["connection"] == "http_oauth"
    assert read_only["mcp_scopes"] == ["mail.read"]
    assert read_only["yandex_full_mail_access"] is False
    assert read_only["write_available"] is False
    assert "reconnect" in read_only["action_needed"]
    assert full["write_available"] is True
    assert full["confirmation"] == "elicitation"
    assert full["action_needed"] is None
    assert no_browser["confirmation"] == "unavailable"
    assert "elicitation" in no_browser["action_needed"]
    for info in (read_only, full, no_browser):
        assert not re.search(r"ya-|secret|token", str(info), re.I)


def test_connection_capabilities_mention_browser_confirmation_when_available(monkeypatch):
    imap = WriteIMAP(folders=github_folders())

    async def scenario(flow):
        with user_mailbox(monkeypatch, imap), signed_in(FULL):
            return await server.get_connection_capabilities(Client(elicitation=False))

    info, _ = run_flow(scenario, settings=Settings(allow_write=True))
    assert info["confirmation"] == "browser_link"
    assert info["client_supports_elicitation"] is False
    assert info["write_available"] is True


def test_connection_capabilities_for_the_local_server(monkeypatch):
    settings = Settings(email="a@yandex.ru", oauth_token="t", allow_write=False)
    monkeypatch.setattr(server, "_settings", lambda: settings)
    monkeypatch.setattr(server, "_mailboxes", {})
    monkeypatch.setattr(server, "_confirmations", None)
    info = asyncio.run(server.get_connection_capabilities(Client()))
    assert info["connection"] == "local"
    assert info["mcp_scopes"] is None
    assert info["yandex_full_mail_access"] is None  # YANDEX_OAUTH_TOKEN carries no scope
    assert info["server_allows_write"] is False
    assert "YANDEX_ALLOW_WRITE" in info["action_needed"]
    box = Mailbox(settings, OAuthCredentials("a@yandex.ru", StaticTokenSource("t", FULL)))
    monkeypatch.setattr(server, "_mailbox", lambda: box)
    assert asyncio.run(server.get_connection_capabilities(Client()))["yandex_full_mail_access"]
