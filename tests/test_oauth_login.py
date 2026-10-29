import base64
import hashlib
import io
import json
import os
import stat
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import keyring
import pytest
from imapclient.exceptions import LoginError
from keyring.backend import KeyringBackend

from yandex_mail_mcp import config, server
from yandex_mail_mcp import login as login_module
from yandex_mail_mcp.config import ConfigurationError, Settings
from yandex_mail_mcp.credentials import (
    OAuthCredentials,
    PasswordCredentials,
    StaticTokenSource,
    StoredTokens,
    StoredTokenSource,
    TokenStore,
)
from yandex_mail_mcp.mail import Mailbox, MailError, default_credentials
from yandex_mail_mcp.yandex_oauth import (
    YandexOAuth,
    YandexOAuthError,
    YandexTokens,
    YandexUser,
    _send,
    pkce_pair,
    requested_scope,
)

STORED = StoredTokens(
    login="ivan",
    email="ivan@yandex.ru",
    access_token="access-1",
    refresh_token="refresh-1",
    expires_at=1000.0,
    scope="mail:imap_ro login:info login:email",
)


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.values = {}

    def get_password(self, service, username):
        return self.values.get((service, username))

    def set_password(self, service, username, password):
        self.values[(service, username)] = password

    def delete_password(self, service, username):
        self.values.pop((service, username), None)


class FakeIMAP:
    def __init__(self, failures=0):
        self.failures = failures
        self.logins = []

    def oauth2_login(self, user, token):
        self.logins.append((user, token))
        if len(self.logins) <= self.failures:
            raise LoginError("[AUTHENTICATIONFAILED] Invalid credentials")


class CountingSource:
    def __init__(self, scope=None):
        self.refreshes = 0
        self._scope = scope

    def access_token(self):
        return "old"

    def refresh(self):
        self.refreshes += 1
        return f"new-{self.refreshes}"

    def scope(self):
        return self._scope


def test_oauth_credentials_use_xoauth2():
    imap = FakeIMAP()
    OAuthCredentials("ivan@yandex.ru", CountingSource()).login(imap)
    assert imap.logins == [("ivan@yandex.ru", "old")]


def test_oauth_credentials_refresh_once_after_login_error():
    imap, source = FakeIMAP(failures=1), CountingSource()
    OAuthCredentials("ivan@yandex.ru", source).login(imap)
    assert imap.logins == [("ivan@yandex.ru", "old"), ("ivan@yandex.ru", "new-1")]
    assert source.refreshes == 1


def test_oauth_credentials_raise_after_second_failure():
    imap, source = FakeIMAP(failures=2), CountingSource()
    with pytest.raises(LoginError):
        OAuthCredentials("ivan@yandex.ru", source).login(imap)
    assert len(imap.logins) == 2
    assert source.refreshes == 1


def test_static_token_cannot_refresh():
    with pytest.raises(LoginError, match="cannot be refreshed"):
        OAuthCredentials("a@yandex.ru", StaticTokenSource("t")).login(FakeIMAP(failures=1))


def test_read_only_only_when_scope_is_known_read_only():
    assert "read-only" in OAuthCredentials("a", CountingSource("mail:imap_ro")).read_only_reason()
    assert OAuthCredentials("a", CountingSource("mail:imap_full")).read_only_reason() is None
    assert OAuthCredentials("a", CountingSource(None)).read_only_reason() is None
    assert PasswordCredentials("a", "x").read_only_reason() is None


def test_read_only_token_blocks_writes_with_a_hint():
    settings = Settings(email="a@yandex.ru", allow_write=True)
    box = Mailbox(settings, OAuthCredentials("a@yandex.ru", CountingSource("mail:imap_ro")))
    assert "read-only" in box.write_error()
    assert "yandex-mail-mcp login" in box.write_error()
    box = Mailbox(settings, OAuthCredentials("a@yandex.ru", CountingSource("mail:imap_full")))
    assert box.write_error() is None
    assert "YANDEX_ALLOW_WRITE" in Mailbox(Settings(email="a"), CountingSource()).write_error()


@pytest.fixture
def env(monkeypatch):
    for name in ("YANDEX_EMAIL", "YANDEX_APP_PASSWORD", "YANDEX_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_environment_token_comes_first(env):
    TokenStore().save(STORED)
    env.setenv("YANDEX_EMAIL", "a@yandex.ru")
    env.setenv("YANDEX_APP_PASSWORD", "pw")
    env.setenv("YANDEX_OAUTH_TOKEN", "env-token")
    settings = Settings.from_env()
    assert settings.oauth_token.get_secret_value() == "env-token"
    assert settings.app_password is None
    assert not settings.stored_tokens


def test_stored_tokens_come_before_password(env):
    TokenStore().save(STORED)
    env.setenv("YANDEX_EMAIL", "a@yandex.ru")
    env.setenv("YANDEX_APP_PASSWORD", "pw")
    settings = Settings.from_env()
    assert settings.stored_tokens
    assert settings.email == "ivan@yandex.ru"
    assert settings.app_password is None


def test_password_is_the_deprecated_fallback(env, caplog):
    env.setenv("YANDEX_EMAIL", "a@yandex.ru")
    env.setenv("YANDEX_APP_PASSWORD", "pw")
    settings = Settings.from_env()
    assert settings.app_password.get_secret_value() == "pw"
    assert "deprecated" in caplog.text
    assert "yandex-mail-mcp login" in caplog.text


def test_token_requires_email_and_something_is_required(env):
    env.setenv("YANDEX_OAUTH_TOKEN", "t")
    with pytest.raises(ConfigurationError, match="YANDEX_EMAIL"):
        Settings.from_env()
    env.delenv("YANDEX_OAUTH_TOKEN")
    with pytest.raises(ConfigurationError, match="yandex-mail-mcp login"):
        Settings.from_env()
    assert Settings.from_env(require_credentials=False).email == ""


def test_server_without_own_credentials_leaves_the_keyring_alone(env, monkeypatch):
    TokenStore().save(STORED)
    lookups = []

    class RecordingStore(TokenStore):
        def load(self):
            lookups.append(1)
            return super().load()

    monkeypatch.setattr(config, "TokenStore", RecordingStore)
    settings = Settings.from_env(require_credentials=False)
    assert (settings.email, settings.stored_tokens) == ("", False)
    assert lookups == []
    assert Settings.from_env().stored_tokens
    assert lookups == [1]


def test_default_credentials_follow_settings():
    token = default_credentials(Settings(email="a@yandex.ru", oauth_token="t"))
    assert isinstance(token, OAuthCredentials)
    assert token.source.access_token() == "t"
    assert isinstance(
        default_credentials(Settings(email="a", app_password="p")), PasswordCredentials
    )
    TokenStore().save(STORED)
    with pytest.raises(ConfigurationError, match="YANDEX_OAUTH_CLIENT_ID"):
        default_credentials(Settings(email="ivan@yandex.ru", stored_tokens=True))
    stored = default_credentials(
        Settings(email="ivan@yandex.ru", stored_tokens=True, oauth_client_id="cid")
    )
    assert isinstance(stored.source, StoredTokenSource)
    TokenStore().delete()
    with pytest.raises(MailError, match="login"):
        default_credentials(
            Settings(email="ivan@yandex.ru", stored_tokens=True, oauth_client_id="cid")
        )
    with pytest.raises(MailError, match="No Yandex credentials"):
        default_credentials(Settings(email="a"))


def test_pkce_pair_follows_rfc_7636():
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert set(verifier) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    )
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).decode().rstrip("=")
    assert pkce_pair()[0] != verifier


def test_requested_scope_depends_on_write_flag():
    assert requested_scope(False).split()[0] == "mail:imap_ro"
    assert requested_scope(True).split()[0] == "mail:imap_full"


def test_file_store_is_private_without_keyring(tmp_path):
    store = TokenStore(tmp_path / "dir" / "tokens.json")
    assert store.save(STORED) == str(store.path)
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert store.load() == STORED
    store.delete()
    assert store.load() is None


def test_keyring_store_is_preferred(tmp_path):
    previous = keyring.get_keyring()
    memory = MemoryKeyring()
    keyring.set_keyring(memory)
    try:
        store = TokenStore(tmp_path / "tokens.json")
        assert store.save(STORED) == "the system keyring"
        assert not store.path.exists()
        assert json.loads(memory.values[("yandex-mail-mcp", "tokens")])["email"] == "ivan@yandex.ru"
        assert store.load() == STORED
        store.delete()
        assert memory.values == {}
    finally:
        keyring.set_keyring(previous)


def test_unreadable_store_is_ignored(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    store.path.write_text("{not json")
    assert store.load() is None


class FakeOAuth:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def refresh(self, refresh_token, scope=""):
        self.calls.append(("refresh", refresh_token))
        if self.error:
            raise self.error
        return YandexTokens("access-2", "refresh-2", 5000.0, "")

    def revoke(self, token):
        self.calls.append(("revoke", token))
        if self.error:
            raise self.error


def test_stored_source_refreshes_near_expiry_and_saves(tmp_path):
    store, oauth = TokenStore(tmp_path / "t.json"), FakeOAuth()
    source = StoredTokenSource(store, STORED, oauth, clock=lambda: 900.0)
    assert source.access_token() == "access-1"
    later = StoredTokenSource(store, STORED, oauth, clock=lambda: 950.0)
    assert later.access_token() == "access-2"
    saved = store.load()
    assert (saved.refresh_token, saved.expires_at, saved.scope) == (
        "refresh-2",
        5000.0,
        STORED.scope,
    )


def test_stored_source_refreshes_once_for_concurrent_rejections(tmp_path):
    store, oauth = TokenStore(tmp_path / "t.json"), FakeOAuth()
    source = StoredTokenSource(store, STORED, oauth, clock=lambda: 0.0)
    assert source.access_token() == "access-1"
    # Another connection, in its own thread, already replaced the token this
    # connection was given.
    other = threading.Thread(target=lambda: (source.access_token(), source.refresh()))
    other.start()
    other.join()
    assert [call for call in oauth.calls if call[0] == "refresh"] == [("refresh", "refresh-1")]
    # This connection's rejected token is stale: it gets the replacement, no second refresh.
    assert source.refresh() == "access-2"
    assert len(oauth.calls) == 1
    # A rejection of the replacement itself does refresh again.
    assert source.refresh() == "access-2"
    assert len(oauth.calls) == 2


def test_stored_source_refresh_failure_asks_to_log_in(tmp_path):
    oauth = FakeOAuth(YandexOAuthError("invalid_grant", "expired"))
    source = StoredTokenSource(TokenStore(tmp_path / "t.json"), STORED, oauth)
    with pytest.raises(LoginError, match="yandex-mail-mcp login"):
        source.refresh()


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


def form(request):
    return dict(urllib.parse.parse_qsl(request.data.decode()))


def test_authorize_url_carries_pkce_and_state():
    url = YandexOAuth("cid").authorize_url(
        "http://127.0.0.1:1/callback", "st", "mail:imap_ro", "ch"
    )
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert url.startswith("https://oauth.yandex.ru/authorize?")
    assert query == {
        "response_type": "code",
        "client_id": "cid",
        "redirect_uri": "http://127.0.0.1:1/callback",
        "state": "st",
        "scope": "mail:imap_ro",
        "force_confirm": "yes",
        "code_challenge": "ch",
        "code_challenge_method": "S256",
    }


def test_exchange_refresh_revoke_and_user():
    transport = Recorder(
        {"access_token": "a", "refresh_token": "r", "expires_in": 100},
        {"access_token": "a2", "expires_in": 100, "scope": "mail:imap_full"},
        {},
        {"login": "ivan", "default_email": "ivan@ya.ru"},
        {"login": "petr@company.ru"},
        {"login": "anna"},
    )
    oauth = YandexOAuth("cid", "secret", transport=transport, clock=lambda: 10.0)
    tokens = oauth.exchange_code("c", redirect_uri="http://x/cb", code_verifier="v", scope="s")
    assert tokens == YandexTokens("a", "r", 110.0, "s")
    assert form(transport.requests[0]) == {
        "client_id": "cid",
        "client_secret": "secret",
        "grant_type": "authorization_code",
        "code": "c",
        "redirect_uri": "http://x/cb",
        "code_verifier": "v",
    }
    assert oauth.refresh("r") == YandexTokens("a2", "r", 110.0, "mail:imap_full")
    assert form(transport.requests[1])["grant_type"] == "refresh_token"
    oauth.revoke("a2")
    assert transport.requests[2].full_url == "https://oauth.yandex.ru/revoke_token"
    assert form(transport.requests[2])["access_token"] == "a2"
    assert oauth.user("a2").email == "ivan@ya.ru"
    assert transport.requests[3].get_header("Authorization") == "OAuth a2"
    assert oauth.user("a2").email == "petr@company.ru"
    assert oauth.user("a2").email == "anna@yandex.ru"


def test_missing_access_token_or_refresh_token_is_an_error():
    oauth = YandexOAuth("cid", transport=Recorder({"error": "x"}))
    with pytest.raises(YandexOAuthError, match="no access token"):
        oauth.exchange_code("c")
    with pytest.raises(YandexOAuthError, match="invalid_grant"):
        oauth.refresh("")


def test_send_reports_yandex_errors(monkeypatch):
    def failing(request, timeout):
        body = io.BytesIO(b'{"error": "invalid_grant", "error_description": "Code has expired"}')
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, body)

    monkeypatch.setattr(urllib.request, "urlopen", failing)
    with pytest.raises(YandexOAuthError, match="invalid_grant: Code has expired") as raised:
        _send(urllib.request.Request("https://oauth.yandex.ru/token"))
    assert raised.value.error == "invalid_grant"

    def unreachable(request, timeout):
        raise OSError("Network is unreachable")

    def listed(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 502, "Bad Gateway", {}, io.BytesIO(b"[]"))

    monkeypatch.setattr(urllib.request, "urlopen", listed)
    with pytest.raises(YandexOAuthError, match="http_error: HTTP 502"):
        _send(urllib.request.Request("https://oauth.yandex.ru/token"))

    monkeypatch.setattr(urllib.request, "urlopen", unreachable)
    with pytest.raises(YandexOAuthError, match="unavailable"):
        _send(urllib.request.Request("https://oauth.yandex.ru/token"))


def loopback():
    return HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)


def visit(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def browser_returning(**params):
    """A fake browser that follows the authorize URL straight to the callback."""

    def open_browser(url):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        callback = query["redirect_uri"]
        values = {"state": query["state"], **params}
        threading.Thread(
            target=visit,
            args=(f"{callback}?{urllib.parse.urlencode(values)}",),
            daemon=True,
        ).start()

    return open_browser


class LoginOAuth:
    def __init__(self):
        self.real = YandexOAuth("cid")
        self.exchanged = []

    def authorize_url(self, *args):
        return self.real.authorize_url(*args)

    def exchange_code(self, code, **kwargs):
        self.exchanged.append((code, kwargs))
        return YandexTokens("a", "r", 99.0, kwargs["scope"])

    def user(self, token):
        return YandexUser("ivan", "ivan@yandex.ru")


def test_login_runs_pkce_flow_and_saves_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("YANDEX_ALLOW_WRITE", "true")
    oauth = LoginOAuth()
    store = TokenStore(tmp_path / "t.json")
    lines = []
    stored = login_module.login(
        oauth=oauth,
        store=store,
        open_browser=browser_returning(code="the-code"),
        output=lines.append,
        server=loopback(),
    )
    assert stored.email == "ivan@yandex.ru"
    assert store.load() == stored
    [(code, kwargs)] = oauth.exchanged
    assert code == "the-code"
    assert kwargs["redirect_uri"].endswith("/callback")
    assert len(kwargs["code_verifier"]) >= 43
    assert kwargs["scope"].startswith("mail:imap_full")
    assert "read and write" in lines[-1]


def test_wrong_state_is_refused_and_the_real_callback_still_works(tmp_path):
    statuses = []
    browsers = []

    def open_browser(url):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        callback = query["redirect_uri"]

        def browse():
            # A stale tab with another attempt's state arrives first.
            statuses.append(visit(f"{callback}?state=forged&code=stolen"))
            real = urllib.parse.urlencode({"state": query["state"], "code": "real"})
            statuses.append(visit(f"{callback}?{real}"))

        browsers.append(threading.Thread(target=browse, daemon=True))
        browsers[0].start()

    oauth = LoginOAuth()
    login_module.login(
        oauth=oauth,
        store=TokenStore(tmp_path / "t.json"),
        open_browser=open_browser,
        output=lambda line: None,
        server=loopback(),
    )
    assert [code for code, _ in oauth.exchanged] == ["real"]
    # login returns once the server has answered; the browser may still be reading.
    browsers[0].join(timeout=5)
    assert statuses == [400, 200]


def test_login_reports_denied_access(tmp_path):
    with pytest.raises(login_module.LoginError, match="access_denied"):
        login_module.login(
            oauth=LoginOAuth(),
            store=TokenStore(tmp_path / "t.json"),
            open_browser=browser_returning(error="access_denied"),
            output=lambda line: None,
            server=loopback(),
        )


def test_callback_wait_ignores_other_paths_and_times_out():
    server_ = loopback()
    port = server_.server_address[1]
    threading.Thread(
        target=visit, args=(f"http://127.0.0.1:{port}/favicon.ico",), daemon=True
    ).start()
    try:
        assert login_module.wait_for_callback(server_, "s", timeout=1.5) == (None, "timed out")
    finally:
        server_.server_close()


def test_logout_revokes_and_deletes(tmp_path):
    store, oauth, lines = TokenStore(tmp_path / "t.json"), FakeOAuth(), []
    store.save(STORED)
    login_module.logout(oauth=oauth, store=store, output=lines.append)
    assert oauth.calls == [("revoke", "access-1")]
    assert store.load() is None
    login_module.logout(oauth=oauth, store=store, output=lines.append)
    assert lines[-1] == "Not signed in."


def test_logout_deletes_even_if_revoke_fails(tmp_path):
    store, lines = TokenStore(tmp_path / "t.json"), []
    store.save(STORED)
    oauth = FakeOAuth(YandexOAuthError("unavailable"))
    login_module.logout(oauth=oauth, store=store, output=lines.append)
    assert store.load() is None
    assert "Could not revoke" in lines[0]


def test_login_requires_client_id():
    with pytest.raises(ConfigurationError, match="YANDEX_OAUTH_CLIENT_ID"):
        login_module._oauth()


def test_main_dispatches_login_and_rejects_unknown_commands(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "load_dotenv", lambda: None)
    monkeypatch.setattr(login_module, "login", lambda: calls.append("login"))
    monkeypatch.setattr(login_module, "logout", lambda: calls.append("logout"))
    server.main(["login"])
    server.main(["logout"])
    assert calls == ["login", "logout"]
    with pytest.raises(SystemExit, match="usage"):
        server.main(["frobnicate"])

    def failing():
        raise login_module.LoginError("Sign-in failed: timed out")

    monkeypatch.setattr(login_module, "login", failing)
    with pytest.raises(SystemExit, match="login: Sign-in failed"):
        server.main(["login"])
