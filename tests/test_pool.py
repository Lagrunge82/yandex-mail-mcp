import asyncio
import imaplib
from typing import ClassVar

import pytest

from yandex_mail_mcp import mail, server
from yandex_mail_mcp.config import Settings
from yandex_mail_mcp.mail import ConnectionPool, Mailbox, MailError


class FakeIMAP:
    """A logged-in IMAP session that records commands and tracks the selected folder."""

    instances: ClassVar[list["FakeIMAP"]] = []

    def __init__(self, host="imap.example", port=993, ssl=True, timeout=30):
        self.calls = []
        self.logins = 0
        self.closed = False
        self.noop_error = None
        self.capabilities = {"MOVE", "UNSELECT"}
        self._imap = type("State", (), {"state": "NONAUTH"})()
        FakeIMAP.instances.append(self)

    def login(self, user, password):
        self.logins += 1
        self._imap.state = "AUTH"

    def logout(self):
        self.closed = True

    def noop(self):
        self.calls.append("noop")
        if self.noop_error:
            raise self.noop_error

    def has_capability(self, name):
        return name in self.capabilities

    def list_folders(self):
        return [((b"\\HasNoChildren",), b"|", "INBOX"), ((), b"|", "AI-Promotions")]

    def select_folder(self, name, readonly=False):
        self.calls.append(("select", name, readonly))
        self._imap.state = "SELECTED"

    def unselect_folder(self):
        # Like IMAPClient: sends UNSELECT but leaves imaplib's state as it was.
        self.calls.append("unselect")
        if self._imap.state != "SELECTED":
            raise imaplib.IMAP4.error("UNSELECT command error: BAD [b'No mailbox selected']")

    def search(self, criteria, charset=None):
        return [3]

    def move(self, uids, folder):
        self.calls.append(("move", uids, folder))

    def add_flags(self, uids, flags):
        self.calls.append(("add_flags", uids, flags))


@pytest.fixture
def fake_imap(monkeypatch):
    FakeIMAP.instances = []
    monkeypatch.setattr(mail, "IMAPClient", FakeIMAP)
    return FakeIMAP.instances


def test_sequential_calls_reuse_one_login(fake_imap):
    box = Mailbox(Settings(email="test@yandex.com", app_password="x"))
    box.folders()
    box.folders()
    assert len(fake_imap) == 1
    assert fake_imap[0].logins == 1
    assert fake_imap[0].calls == ["noop"]


@pytest.mark.parametrize(
    "error", [imaplib.IMAP4.abort("socket error: EOF"), ConnectionResetError(104, "reset")]
)
def test_connection_failing_noop_is_replaced(fake_imap, error):
    box = Mailbox(Settings(email="test@yandex.com", app_password="x"))
    box.folders()
    fake_imap[0].noop_error = error
    box.folders()
    assert len(fake_imap) == 2
    assert fake_imap[0].closed
    assert fake_imap[1].logins == 1
    assert not fake_imap[1].closed


def test_idle_connection_is_replaced_without_noop():
    now = [0.0]
    opened = []

    def factory():
        opened.append(FakeIMAP())
        return opened[-1]

    pool = ConnectionPool(factory, size=1, idle_timeout=300, clock=lambda: now[0])
    with pool.connection():
        pass
    now[0] = 301
    with pool.connection() as imap:
        assert imap is opened[1]
    assert opened[0].closed
    assert "noop" not in opened[0].calls


def test_move_returns_connection_without_selected_folder(fake_imap):
    box = Mailbox(Settings(email="test@yandex.com", app_password="x", allow_write=True))
    box.move_to_promotions(3, confirm=True)
    imap = fake_imap[0]
    assert ("select", "INBOX", False) in imap.calls
    assert imap.calls[-1] == "unselect"
    assert imap._imap.state == "AUTH"
    assert not imap.closed
    box.folders()
    assert len(fake_imap) == 1


def test_move_email_returns_connection_without_selected_folder(fake_imap):
    box = Mailbox(Settings(email="test@yandex.com", app_password="x", allow_write=True))
    assert box.move(3, "AI-Promotions")["folder"] == "AI-Promotions"
    imap = fake_imap[0]
    assert imap.calls[-2:] == [("move", [3], "AI-Promotions"), "unselect"]
    assert imap._imap.state == "AUTH"
    box.set_flag(3, "\\Seen", True)
    assert len(fake_imap) == 1
    assert imap.calls[-1] == "unselect"


def test_selected_connection_without_unselect_is_closed(fake_imap):
    box = Mailbox(Settings(email="test@yandex.com", app_password="x"))
    with box.connect() as imap:
        imap.capabilities.discard("UNSELECT")
        imap.select_folder("INBOX", readonly=False)
    assert fake_imap[0].closed
    box.folders()
    assert len(fake_imap) == 2


def test_connection_is_closed_after_an_error(fake_imap):
    box = Mailbox(Settings(email="test@yandex.com", app_password="x"))
    with pytest.raises(KeyError), box.connect():
        raise KeyError("boom")
    assert fake_imap[0].closed
    box.folders()
    assert len(fake_imap) == 2


def test_failed_login_closes_the_socket_and_frees_the_slot(monkeypatch):
    class BadLogin(FakeIMAP):
        def login(self, user, password):
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    FakeIMAP.instances = []
    monkeypatch.setattr(mail, "IMAPClient", BadLogin)
    box = Mailbox(Settings(email="test@yandex.com", app_password="x", imap_pool_size=1))
    for _ in range(2):
        with pytest.raises(imaplib.IMAP4.error):
            box.folders()
    assert [imap.closed for imap in FakeIMAP.instances] == [True, True]


def test_pool_limits_open_connections():
    pool = ConnectionPool(FakeIMAP, size=1, wait_timeout=0.01)
    with pool.connection(), pytest.raises(MailError, match="busy"), pool.connection():
        pass
    with pool.connection():
        pass


def test_close_logs_out_idle_connections_and_later_returns():
    pool = ConnectionPool(FakeIMAP, size=2)
    with pool.connection() as first:
        pass
    with pool.connection() as again, pool.connection() as second:
        assert again is first
        pool.close()
        assert not first.closed
    assert first.closed
    assert second.closed


def test_server_lifespan_closes_pools(monkeypatch, fake_imap):
    settings = Settings(email="test@yandex.com", app_password="x")
    monkeypatch.setattr(server, "_settings", lambda: settings)
    monkeypatch.setattr(server, "_mailboxes", {})
    box = server._mailbox()
    assert server._mailbox() is box
    box.folders()

    async def run():
        async with server._lifespan(server.mcp):
            pass

    asyncio.run(run())
    assert fake_imap[0].closed
    assert server._mailboxes == {}


def test_unselect_then_call_without_select_keeps_connection(fake_imap):
    box = Mailbox(Settings(email="test@yandex.com", app_password="x"))
    with box.connect() as imap:
        imap.select_folder("INBOX", readonly=True)
    box.folders()
    box.folders()
    assert len(fake_imap) == 1
    assert fake_imap[0].calls.count("unselect") == 1
    assert not fake_imap[0].closed


def test_mail_error_keeps_healthy_connection(fake_imap):
    box = Mailbox(Settings(email="test@yandex.com", app_password="x", allow_write=True))
    for _ in range(3):
        with pytest.raises(MailError, match="not found"):
            box.move(3, "Missing")
    assert len(fake_imap) == 1
    assert fake_imap[0].logins == 1


def test_checkin_failure_does_not_replace_the_result(fake_imap):
    box = Mailbox(Settings(email="test@yandex.com", app_password="x", allow_write=True))

    def capability(name):
        raise imaplib.IMAP4.abort("socket error: EOF")

    with box.connect() as imap:
        imap.has_capability = capability
        imap.select_folder("INBOX", readonly=False)
    assert fake_imap[0].closed
    box.folders()
    assert len(fake_imap) == 2
