import asyncio
import base64
import hashlib
import json
import sqlite3
import urllib.parse

import httpx2 as httpx
import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from yandex_mail_mcp import server
from yandex_mail_mcp.config import ConfigurationError, Settings
from yandex_mail_mcp.crypto import TokenCipher, generate_key
from yandex_mail_mcp.yandex_oauth import YandexOAuth, YandexOAuthError, YandexTokens, YandexUser

PUBLIC = "http://127.0.0.1:8000"


@pytest.fixture(autouse=True)
def allowed_users(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_USERS", "ivan")
    monkeypatch.delenv("MCP_AUTH_RATE_LIMIT", raising=False)


REDIRECT = "http://localhost:9999/cb"
VERIFIER = "v" * 50
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
)
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def _raw_keys(value):
    for item in value.split(","):
        kid, _, encoded = item.partition(":")
        yield kid, base64.urlsafe_b64decode(encoded)


def test_container_round_trip_and_rejections():
    clock = Clock()
    key = generate_key("k1")
    cipher = TokenCipher(list(_raw_keys(key)), clock)
    token = cipher.seal("access", {"login": "ivan"}, 60)
    assert token.startswith("v1.k1.")
    payload = cipher.open(token, "access")
    assert payload["login"] == "ivan"
    assert payload["typ"] == "access"
    assert len(payload["jti"]) == 32
    # Another type, another key id, a changed byte, or expiry: all rejected.
    assert cipher.open(token, "refresh") is None
    other = TokenCipher(list(_raw_keys(generate_key("k1"))), clock)
    assert other.open(token, "access") is None
    assert cipher.open(token.replace("v1.k1.", "v1.k2."), "access") is None
    body = token.rsplit(".", 1)[1]
    flipped = body[:20] + ("A" if body[20] != "A" else "B") + body[21:]
    assert cipher.open(f"v1.k1.{flipped}", "access") is None
    assert cipher.open("garbage", "access") is None
    assert cipher.open("v1.k1.!!!", "access") is None
    clock.now += 61
    assert cipher.open(token, "access") is None


def test_key_rotation_reads_tokens_of_the_second_key():
    old, new = generate_key("old"), generate_key("new")
    clock = Clock()
    sealed_with_old = TokenCipher(list(_raw_keys(old)), clock).seal("access", {"a": 1}, 60)
    rotated = TokenCipher(list(_raw_keys(f"{new},{old}")), clock)
    assert rotated.open(sealed_with_old, "access")["a"] == 1
    assert rotated.seal("access", {}, 60).startswith("v1.new.")


@pytest.mark.parametrize(
    "value",
    ["", "k1", "k1:short", "bad kid:" + generate_key().split(":")[1], "k1:x,k1:y"],
)
def test_invalid_keys_are_rejected(value):
    if value == "k1:x,k1:y":
        key = generate_key().split(":")[1]
        value = f"k1:{key},k1:{key}"
    with pytest.raises(ConfigurationError):
        TokenCipher.from_env(value)


class FakeYandex:
    """Yandex OAuth for the integration tests; records refreshes and revocations."""

    def __init__(self, scope="mail:imap_ro login:info login:email", users=None):
        self.scope = scope
        self.users = users or {"yc": YandexUser("ivan", "ivan@yandex.ru")}
        self.refreshed = []
        self.revoked = []
        self.authorized = []

    def authorize_url(self, redirect_uri, state, scope, code_challenge=None):
        self.authorized.append(scope)
        return YandexOAuth("cid").authorize_url(redirect_uri, state, scope)

    def exchange_code(self, code, **kwargs):
        if code not in self.users:
            raise YandexOAuthError("invalid_grant", "bad code")
        return YandexTokens(f"ya-access-{code}", f"ya-refresh-{code}", 9e9, self.scope)

    def user(self, token):
        return self.users[token.removeprefix("ya-access-")]

    def refresh(self, refresh_token, scope=""):
        self.refreshed.append(refresh_token)
        code = refresh_token.removeprefix("ya-refresh-")
        return YandexTokens(f"ya-access-{code}", refresh_token, 9e9, scope or self.scope)

    def revoke(self, token):
        self.revoked.append(token)


def query_of(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


class Flow:
    """Drives the OAuth flow against the ASGI app, as an MCP client and its browser would."""

    def __init__(self, client, provider=None):
        self.client = client
        self.provider = provider
        self.consent_shown = False

    async def register(self):
        response = await self.client.post(
            "/register", json={"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"}
        )
        assert response.status_code == 201, response.text
        return response.json()["client_id"]

    async def to_callback(self, client_id, code="yc", *, access="write", scope=None):
        """Walk from /authorize to the Yandex redirect; return the sealed state and callback params.

        access is what the user picks on the consent page when writes are offered;
        scope is the client's own scope request, None for no scope parameter.
        """
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "state": "client-state",
            "resource": f"{PUBLIC}/mcp",
        }
        if scope is not None:
            params["scope"] = scope
        response = await self.client.get("/authorize", params=params)
        assert response.status_code == 302, response.text
        start = await self.client.get(response.headers["location"])
        self.consent_shown = start.status_code == 200
        if self.consent_shown:
            assert "Read and change mail" in start.text
            sealed = query_of(response.headers["location"])["state"]
            start = await self.client.post(
                "/oauth/yandex/start", data={"state": sealed, "access": access}
            )
        assert start.status_code == 302, start.text
        state = query_of(start.headers["location"])["state"]
        return state, {"state": state, "code": code}

    async def authorize(self, client_id, code="yc", *, access="write", scope=None):
        _, params = await self.to_callback(client_id, code, access=access, scope=scope)
        callback = await self.client.get("/oauth/yandex/callback", params=params)
        assert callback.status_code == 302, callback.text
        assert callback.headers["location"].startswith(REDIRECT)
        query = query_of(callback.headers["location"])
        assert query["state"] == "client-state"
        return query["code"]

    async def token(self, client_id, code, verifier=VERIFIER):
        return await self.client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
                "code_verifier": verifier,
                "client_id": client_id,
            },
        )

    async def sign_in(self, code="yc", *, access="write", scope=None):
        client_id = await self.register()
        code = await self.authorize(client_id, code, access=access, scope=scope)
        response = await self.token(client_id, code)
        assert response.status_code == 200, response.text
        return client_id, response.json()

    async def mcp(self, method, params, token, session=None, request_id=1):
        headers = {**MCP_HEADERS}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if session:
            headers["Mcp-Session-Id"] = session
            headers["MCP-Protocol-Version"] = "2025-06-18"
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            body["id"] = request_id
        return await self.client.post("/mcp", headers=headers, json=body)


def run_flow(scenario, *, yandex=None, settings=None, db=":memory:"):
    yandex = yandex or FakeYandex()

    async def main():
        providers = []
        original = server.YandexAuthProvider

        def capture(**kwargs):
            providers.append(original(**kwargs))
            return providers[-1]

        server.YandexAuthProvider = capture
        try:
            app = server.build_oauth_app(
                settings or Settings(), PUBLIC, generate_key(), db, oauth=yandex
            )
        finally:
            server.YandexAuthProvider = original
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=PUBLIC) as client:
                return await scenario(Flow(client, providers[0]))

    return asyncio.run(main()), yandex


def sse_json(response):
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return response.json()


def test_metadata_is_published():
    async def scenario(flow):
        metadata = (await flow.client.get("/.well-known/oauth-authorization-server")).json()
        resource = (await flow.client.get("/.well-known/oauth-protected-resource/mcp")).json()
        return metadata, resource

    (metadata, resource), _ = run_flow(scenario)
    assert metadata["issuer"] == PUBLIC
    assert metadata["registration_endpoint"] == f"{PUBLIC}/register"
    assert metadata["revocation_endpoint"] == f"{PUBLIC}/revoke"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert resource["resource"] == f"{PUBLIC}/mcp"


def test_full_flow_lists_tools_with_bearer_and_401_without():
    async def scenario(flow):
        _, tokens = await flow.sign_in()
        params = {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        }
        anonymous = await flow.mcp("initialize", params, None)
        initialized = await flow.mcp("initialize", params, tokens["access_token"])
        session = initialized.headers["mcp-session-id"]
        await flow.mcp(
            "notifications/initialized", {}, tokens["access_token"], session, request_id=None
        )
        listed = await flow.mcp("tools/list", {}, tokens["access_token"], session, request_id=2)
        return tokens, anonymous, initialized, listed

    (tokens, anonymous, initialized, listed), yandex = run_flow(scenario)
    assert tokens["token_type"] == "Bearer"
    assert tokens["scope"] == "mail.read"
    assert 0 < tokens["expires_in"] <= 3600
    assert anonymous.status_code == 401
    assert (
        'resource_metadata="http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp"'
        in (anonymous.headers["www-authenticate"])
    )
    assert initialized.status_code == 200
    names = {tool["name"] for tool in sse_json(listed)["result"]["tools"]}
    assert {"list_folders", "search_emails", "get_thread"} <= names
    # Read-only server: Yandex is asked for read-only IMAP access.
    assert yandex.authorized == ["mail:imap_ro login:info login:email"]
    # The Yandex token never reaches the client in the clear.
    assert "ya-access" not in json.dumps(tokens)


def test_healthz_is_public_on_the_oauth_server():
    async def scenario(flow):
        return await flow.client.get("/healthz"), await flow.client.get("/healthz/")

    (health, other), _ = run_flow(scenario)
    assert (health.status_code, health.text) == (200, "ok")
    # Starlette may answer the trailing slash with a redirect; it never serves it.
    assert other.status_code in {307, 404}


def test_code_cannot_be_exchanged_twice():
    async def scenario(flow):
        client_id = await flow.register()
        code = await flow.authorize(client_id)
        first = await flow.token(client_id, code)
        second = await flow.token(client_id, code)
        return first, second

    (first, second), _ = run_flow(scenario)
    assert first.status_code == 200
    assert second.status_code == 400
    assert second.json()["error"] == "invalid_grant"


def test_wrong_code_verifier_is_rejected():
    async def scenario(flow):
        client_id = await flow.register()
        return await flow.token(client_id, await flow.authorize(client_id), verifier="w" * 50)

    response, _ = run_flow(scenario)
    assert response.json() == {
        "error": "invalid_grant",
        "error_description": "incorrect code_verifier",
    }


def test_callback_without_the_browser_nonce_is_rejected():
    async def scenario(flow):
        client_id = await flow.register()
        _, params = await flow.to_callback(client_id)
        flow.client.cookies.clear()
        flow.client.cookies.set("mcp_auth_nonce", "forged", path="/oauth/yandex/callback")
        return await flow.client.get("/oauth/yandex/callback", params=params)

    response, _ = run_flow(scenario)
    assert response.status_code == 400
    assert "another browser" in response.text


def test_callback_with_forged_state_is_rejected():
    async def scenario(flow):
        return await flow.client.get("/oauth/yandex/callback", params={"state": "x", "code": "yc"})

    response, _ = run_flow(scenario)
    assert response.status_code == 400
    assert "expired or is invalid" in response.text


def test_denied_consent_redirects_with_access_denied():
    async def scenario(flow):
        client_id = await flow.register()
        state, _ = await flow.to_callback(client_id)
        return await flow.client.get(
            "/oauth/yandex/callback", params={"state": state, "error": "access_denied"}
        )

    response, _ = run_flow(scenario)
    query = query_of(response.headers["location"])
    assert query["error"] == "access_denied"
    assert query["state"] == "client-state"
    assert "code" not in query


def test_foreign_resource_is_refused():
    async def scenario(flow):
        client_id = await flow.register()
        return await flow.client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": REDIRECT,
                "code_challenge": CHALLENGE,
                "code_challenge_method": "S256",
                "resource": "https://elsewhere.example/mcp",
            },
        )

    response, _ = run_flow(scenario)
    assert query_of(response.headers["location"])["error"] == "invalid_target"


def test_refresh_calls_yandex_and_issues_a_new_pair():
    async def scenario(flow):
        client_id, tokens = await flow.sign_in()
        refreshed = await flow.client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client_id,
            },
        )
        return tokens, refreshed

    (tokens, refreshed), yandex = run_flow(scenario)
    assert refreshed.status_code == 200, refreshed.text
    assert yandex.refreshed == ["ya-refresh-yc"]
    assert refreshed.json()["access_token"] != tokens["access_token"]


def test_write_scope_needs_server_opt_in_and_yandex_grant():
    async def scenario(flow):
        return (await flow.sign_in())[1]

    tokens, yandex = run_flow(
        scenario,
        settings=Settings(allow_write=True),
        yandex=FakeYandex(scope="mail:imap_full login:info login:email"),
    )
    assert tokens["scope"] == "mail.read mail.write"
    assert yandex.authorized[0].startswith("mail:imap_full")
    # Yandex granted read-only access: the write scope is dropped.
    tokens, _ = run_flow(scenario, settings=Settings(allow_write=True), yandex=FakeYandex())
    assert tokens["scope"] == "mail.read"


def test_revoke_revokes_the_yandex_grant():
    async def scenario(flow):
        client_id, tokens = await flow.sign_in()
        return await flow.client.post(
            # The SDK's revocation request requires the field, even for public clients.
            "/revoke",
            data={"token": tokens["access_token"], "client_id": client_id, "client_secret": ""},
        )

    response, yandex = run_flow(scenario)
    assert response.status_code == 200
    assert yandex.revoked == ["ya-access-yc"]


def test_expired_access_token_is_rejected():
    clock = Clock()
    cipher = TokenCipher(list(_raw_keys(generate_key())), clock)
    from yandex_mail_mcp.auth_server import ClientStore, YandexAuthProvider

    provider = YandexAuthProvider(
        cipher=cipher,
        clients=ClientStore(":memory:", cipher),
        oauth=FakeYandex(),
        public_url=PUBLIC,
        allow_write=False,
        allowed_users=frozenset({"ivan"}),
        clock=clock,
    )
    grant = {
        "yandex_access_token": "ya",
        "yandex_refresh_token": "yr",
        "yandex_expires_at": clock.now + 600,
        "yandex_scope": "mail:imap_ro",
        "login": "ivan",
        "email": "ivan@yandex.ru",
    }
    issued = provider._issue("client", ["mail.read"], grant)
    # The access token expires a minute before the Yandex token inside it.
    assert issued.expires_in == 540
    token = asyncio.run(provider.load_access_token(issued.access_token))
    assert token.subject == "ivan"
    assert token.claims["yandex_access_token"] == "ya"
    clock.now += 541
    assert asyncio.run(provider.load_access_token(issued.access_token)) is None
    provider.clients.close()


def test_only_the_clients_table_is_on_disk(tmp_path):
    db = tmp_path / "auth.db"

    async def scenario(flow):
        return await flow.sign_in()

    run_flow(scenario, db=str(db))
    connection = sqlite3.connect(db)
    try:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master")]
    finally:
        connection.close()
    assert tables == ["clients", "sqlite_autoindex_clients_1"] or tables == ["clients"]
    raw = db.read_bytes()
    assert b"ya-access" not in raw
    assert b"ya-refresh" not in raw
    assert b"ivan" not in raw


def test_confidential_client_secret_is_sealed(tmp_path):
    db = tmp_path / "auth.db"

    async def scenario(flow):
        response = await flow.client.post("/register", json={"redirect_uris": [REDIRECT]})
        return response.json()

    registered, _ = run_flow(scenario, db=str(db))
    assert registered["client_secret"]
    assert registered["client_secret"].encode() not in db.read_bytes()


def _as_user(login, email, token):
    access = AccessToken(
        token="t",
        client_id="c",
        scopes=["mail.read"],
        claims={
            "yandex_access_token": token,
            "yandex_login": login,
            "yandex_email": email,
            "yandex_scope": "mail:imap_ro",
        },
    )
    return auth_context_var.set(AuthenticatedUser(access))


def test_two_users_get_separate_mailboxes(monkeypatch):
    monkeypatch.setattr(server, "_mailboxes", {})
    monkeypatch.setattr(server, "_settings", lambda: Settings())
    reset = _as_user("ivan", "ivan@yandex.ru", "token-1")
    try:
        ivan = server._mailbox()
    finally:
        auth_context_var.reset(reset)
    reset = _as_user("anna", "anna@yandex.ru", "token-2")
    try:
        anna = server._mailbox()
    finally:
        auth_context_var.reset(reset)
    reset = _as_user("ivan", "ivan@yandex.ru", "token-3")
    try:
        assert server._mailbox() is ivan
    finally:
        auth_context_var.reset(reset)
    assert ivan is not anna
    assert ivan.credentials.email == "ivan@yandex.ru"
    assert anna.credentials.email == "anna@yandex.ru"
    # A refreshed token replaces the old one for new connections.
    assert ivan.credentials.source.access_token() == "token-3"
    assert ivan.write_error() is not None


def test_public_url_requires_keys_and_client_id(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://mail.example.com")
    monkeypatch.delenv("MCP_TOKEN_ENCRYPTION_KEYS", raising=False)
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(server, "_settings", lambda: Settings())
    with pytest.raises(ConfigurationError, match="MCP_TOKEN_ENCRYPTION_KEYS"):
        server.main([])
    monkeypatch.setenv("MCP_TOKEN_ENCRYPTION_KEYS", generate_key())
    with pytest.raises(ConfigurationError, match="YANDEX_OAUTH_CLIENT_ID"):
        server.main([])
    monkeypatch.setenv("MCP_AUTH_TOKEN", "secret")
    with pytest.raises(ConfigurationError, match="MCP_AUTH_TOKEN is no longer supported"):
        server.main([])


def test_oauth_server_starts_and_allows_the_public_host(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://mail.example.com/")
    monkeypatch.setenv("MCP_TOKEN_ENCRYPTION_KEYS", generate_key())
    monkeypatch.setenv("MCP_AUTH_DB", str(tmp_path / "auth.db"))
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)
    monkeypatch.setattr(server, "_settings", lambda: Settings(oauth_client_id="cid"))
    captured = {}
    original = server.build_oauth_app

    def build(*args, **kwargs):
        captured.update(kwargs, public_url=args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(server, "build_oauth_app", build)
    stores = []

    class Store(server.ClientStore):
        def __init__(self, *args):
            super().__init__(*args)
            stores.append(self)

    monkeypatch.setattr(server, "ClientStore", Store)
    monkeypatch.setattr(server.uvicorn, "run", lambda *args, **kwargs: None)
    server.main([])
    # uvicorn did not run the lifespan that closes the store.
    stores[0].close()
    assert captured["public_url"] == "https://mail.example.com"
    assert "mail.example.com" in captured["security"].allowed_hosts
    assert "https://mail.example.com" in captured["security"].allowed_origins
    assert (tmp_path / "auth.db").exists()
