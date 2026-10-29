import asyncio
import imaplib
import socket
import ssl
from types import SimpleNamespace
import pytest
import anyio.to_thread
from mcp.server.mcpserver.exceptions import ToolError

from yandex_mail_mcp import server
from yandex_mail_mcp.mail import MailPermissionError, MailValueError


@pytest.fixture(autouse=True)
def run_sync_with_asyncio(monkeypatch):
    # Run synchronous tool bodies inline while testing the SDK error boundary.
    async def run_sync(fn, *args, **kwargs):
        return fn(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", run_sync)


def test_expected_error_reaches_tool_caller(monkeypatch):
    class Mailbox:
        def search(self, **kwargs):
            raise MailValueError("query cannot be empty")

    monkeypatch.setattr(server, "_mailbox", lambda: Mailbox())
    with pytest.raises(ToolError, match="query cannot be empty"):
        asyncio.run(server.mcp.call_tool("search_emails", {"query": ""}))


def test_unexpected_keyerror_is_not_converted(monkeypatch):
    class Box:
        def folders(self):
            raise KeyError("internal")

    monkeypatch.setattr(server, "_mailbox", lambda: Box())
    with pytest.raises(KeyError):
        server.list_folders()


def test_imap_error_reaches_client(monkeypatch):
    class Box:
        def folders(self):
            raise imaplib.IMAP4.error("No such mailbox")

    monkeypatch.setattr(server, "_mailbox", lambda: Box())
    with pytest.raises(ToolError, match="No such mailbox"):
        asyncio.run(server.mcp.call_tool("list_folders", {}))


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (ConnectionRefusedError(111, "Connection refused"), "Cannot connect to IMAP server"),
        (socket.gaierror(-2, "Name or service not known"), "Cannot connect to IMAP server"),
        (ssl.SSLCertVerificationError("certificate verify failed"), "TLS handshake"),
        (TimeoutError(), "timed out"),
    ],
)
def test_connection_errors_reach_client(monkeypatch, error, message):
    class Box:
        def folders(self):
            raise error

    monkeypatch.setattr(server, "_mailbox", lambda: Box())
    monkeypatch.setattr(
        server,
        "_settings",
        lambda: SimpleNamespace(imap_host="imap.example", imap_port=993, allow_write=False),
    )
    with pytest.raises(ToolError, match=message) as raised:
        asyncio.run(server.mcp.call_tool("list_folders", {}))
    if isinstance(error, OSError) and not isinstance(error, TimeoutError):
        assert "imap.example:993" in str(raised.value)


def test_mail_permission_error_is_not_reported_as_connection_error(monkeypatch):
    class Box:
        def folders(self):
            raise MailPermissionError("Write operations are disabled")

    monkeypatch.setattr(server, "_mailbox", lambda: Box())
    with pytest.raises(ToolError, match="^Write operations are disabled$"):
        server.list_folders()


def test_connection_error_without_settings_reaches_client(monkeypatch):
    class Box:
        def folders(self):
            raise ConnectionRefusedError(111, "Connection refused")

    def settings():
        raise server.ConfigurationError("missing")

    monkeypatch.setattr(server, "_mailbox", lambda: Box())
    monkeypatch.setattr(server, "_settings", settings)
    with pytest.raises(ToolError, match="Cannot connect to the IMAP server; check"):
        server.list_folders()


def test_non_loopback_requires_auth_or_explicit_override(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("MCP_ALLOW_UNAUTHENTICATED_REMOTE", raising=False)
    monkeypatch.setattr(server, "load_dotenv", lambda: None)
    monkeypatch.setattr(
        server,
        "_settings",
        lambda: SimpleNamespace(imap_host="imap", imap_port=993, allow_write=False),
    )
    with pytest.raises(ValueError, match="MCP_AUTH_TOKEN"):
        server.main()


def test_specific_host_is_allowed_by_default(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("MCP_HOST", "192.168.1.5")
    monkeypatch.setenv("MCP_AUTH_TOKEN", "secret")
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(server, "load_dotenv", lambda: None)
    monkeypatch.setattr(
        server,
        "_settings",
        lambda: SimpleNamespace(imap_host="imap", imap_port=993, allow_write=False),
    )
    captured = {}

    def app(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(server.mcp, "streamable_http_app", app)
    monkeypatch.setattr(server.uvicorn, "run", lambda *args, **kwargs: None)
    server.main()
    assert "192.168.1.5:*" in captured["transport_security"].allowed_hosts


def test_settings_are_cached_and_write_flag_does_not_change(monkeypatch):
    monkeypatch.setenv("YANDEX_EMAIL", "test@example.com")
    monkeypatch.setenv("YANDEX_APP_PASSWORD", "test")
    monkeypatch.setenv("YANDEX_ALLOW_WRITE", "false")
    server._settings.cache_clear()
    try:
        first = server._mailbox().config
        monkeypatch.setenv("YANDEX_ALLOW_WRITE", "true")
        assert server._mailbox().config is first
        assert not first.allow_write
    finally:
        server._settings.cache_clear()


def test_imported_server_loads_dotenv_before_first_tool(monkeypatch):
    monkeypatch.delenv("YANDEX_EMAIL", raising=False)
    monkeypatch.delenv("YANDEX_APP_PASSWORD", raising=False)
    calls = []

    def load():
        calls.append(1)
        monkeypatch.setenv("YANDEX_EMAIL", "test@example.com")
        monkeypatch.setenv("YANDEX_APP_PASSWORD", "secret")

    monkeypatch.setattr(server, "load_dotenv", load)
    server._settings.cache_clear()
    try:
        assert server._mailbox().config.email == "test@example.com"
        server._mailbox()
        assert calls == [1]
    finally:
        server._settings.cache_clear()


def test_bearer_auth_rejects_missing_and_wrong_token():
    calls = []

    async def app(scope, receive, send):
        calls.append(scope)

    async def run(headers):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            sent.append(message)

        await server.BearerAuth(app, "secret")({"type": "http", "headers": headers}, receive, send)
        return sent

    for headers in (
        [],
        [(b"authorization", b"Bearer wrong")],
        [(b"authorization", b"Bearer secret"), (b"authorization", b"Bearer secret")],
    ):
        assert asyncio.run(run(headers))[0]["status"] == 401
    asyncio.run(run([(b"authorization", b"Bearer secret")]))
    assert len(calls) == 1


@pytest.mark.parametrize(
    "action, approved, moved",
    [
        ("decline", False, False),
        ("cancel", False, False),
        ("accept", False, False),
        ("accept", True, True),
    ],
)
def test_move_requires_client_acceptance(monkeypatch, action, approved, moved):
    calls = []

    class Box:
        config = SimpleNamespace(allow_write=True)

        def headers(self, uid, folder):
            return {"from": "alice", "subject": "example"}

        def move_to_promotions(self, uid, confirm):
            calls.append(uid)
            return {"uid": uid}

    class Client:
        async def elicit(self, message, schema):
            return SimpleNamespace(action=action, data=SimpleNamespace(approve=approved))

    monkeypatch.setattr(server, "_mailbox", lambda: Box())
    if moved:
        assert asyncio.run(server.move_to_promotions(1, Client(), True)) == {"uid": 1}
    else:
        with pytest.raises(ToolError, match="not approved"):
            asyncio.run(server.move_to_promotions(1, Client(), True))
    assert bool(calls) == moved


def test_tools_have_mutation_annotations():
    tools = asyncio.run(server.mcp.list_tools())
    annotations = {tool.name: tool.annotations for tool in tools}
    assert annotations["list_folders"].read_only_hint
    assert not annotations["move_to_promotions"].read_only_hint
