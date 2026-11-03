"""Read-only operations survive one dropped IMAP session; changes are never repeated."""

import imaplib
import logging
import ssl
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import ClassVar

import pytest
from imapclient.exceptions import LoginError

from yandex_mail_mcp import mail
from yandex_mail_mcp.config import Settings
from yandex_mail_mcp.mail import ConnectionInterrupted, LoginInterrupted, Mailbox

HEADER = b"From: Alice <alice@example.com>\r\nSubject: Secret subject\r\n\r\n"
TEXT_PART = (b"TEXT", b"PLAIN", (b"CHARSET", b"UTF-8"), None, None, b"7BIT", 5, 1, None, None)
RAW = HEADER + b"hello"


class FakeIMAP:
    """A session whose commands fail as the test scripts, by connection number."""

    instances: ClassVar[list["FakeIMAP"]] = []
    # Connection number -> (command, error) raised by that command.
    failures: ClassVar[dict[int, tuple[str, BaseException]]] = {}

    def __init__(self, host="imap.example", port=993, ssl=True, timeout=30):
        self.number = len(FakeIMAP.instances)
        self.calls = []
        self.logins = 0
        self.closed = False
        self.logged_out = False
        self._imap = type("State", (), {"state": "NONAUTH"})()
        FakeIMAP.instances.append(self)

    def oauth2_login(self, user, token):
        command, error = FakeIMAP.failures.get(self.number, (None, None))
        if command == "login":
            # Like IMAPClient.oauth2_login, which wraps every IMAP error, aborts
            # included, in LoginError; socket errors on the read pass unwrapped.
            try:
                raise error
            except imaplib.IMAP4.error as exc:
                raise LoginError(str(exc))  # noqa: B904
        self.logins += 1
        self._imap.state = "AUTH"

    def logout(self):
        self.logged_out = True
        self.closed = True

    def shutdown(self):
        self.closed = True

    def _command(self, name):
        self.calls.append(name)
        command, error = FakeIMAP.failures.get(self.number, (None, None))
        if command == name:
            raise error

    def noop(self):
        self._command("noop")

    def has_capability(self, name):
        return name == "UNSELECT"

    def list_folders(self):
        self._command("list")
        return [((b"\\HasNoChildren",), b"|", "INBOX")]

    def select_folder(self, name, readonly=False):
        self._command("select")
        self._imap.state = "SELECTED"

    def unselect_folder(self):
        self._command("unselect")

    def search(self, criteria, charset=None):
        self._command("search")
        return [7]

    def fetch(self, uids, fields):
        self._command("fetch")
        if fields == ["BODY.PEEK[HEADER]", "BODYSTRUCTURE"]:
            return {7: {b"BODY[HEADER]": HEADER, b"BODYSTRUCTURE": TEXT_PART}}
        if fields == ["RFC822.SIZE"]:
            return {7: {b"RFC822.SIZE": len(RAW)}}
        if fields == ["BODY.PEEK[]"]:
            return {7: {b"BODY[]": RAW}}
        return {7: {b"BODY[1]<0>": b"hello"}}

    def move(self, uids, folder):
        self._command("move")

    def add_flags(self, uids, flags):
        self._command("add_flags")


@pytest.fixture
def fake_imap(monkeypatch):
    FakeIMAP.instances = []
    FakeIMAP.failures = {}
    monkeypatch.setattr(mail, "IMAPClient", FakeIMAP)
    monkeypatch.setattr(mail, "LOGIN_RETRY_DELAY", 0)
    return FakeIMAP


def mailbox(**settings):
    return Mailbox(Settings(email="test@yandex.com", oauth_token="x", **settings))


READS = {
    "recent": lambda box: box.recent(limit=5),
    "search": lambda box: box.search(sender="example.com"),
    "message": lambda box: [box.message(7)],
    "folders": lambda box: box.folders(),
}


@pytest.mark.parametrize(
    ("read", "command"),
    [
        ("recent", "fetch"),
        ("recent", "search"),
        ("search", "search"),
        ("search", "fetch"),
        ("message", "fetch"),
        ("folders", "list"),
    ],
)
def test_read_is_retried_once_on_a_new_connection(fake_imap, read, command):
    fake_imap.failures = {0: (command, imaplib.IMAP4.abort("socket error: EOF"))}
    box = mailbox()
    assert READS[read](box)
    first, second = fake_imap.instances
    # Closed without LOGOUT, which would wait on a dead session.
    assert first.closed and not first.logged_out
    assert second.logins == 1
    assert not second.closed
    # The retry repeats the whole operation, SELECT included.
    if read != "folders":
        assert second.calls[0] == "select"


@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError(104, "Connection reset by peer"),
        BrokenPipeError(32, "Broken pipe"),
        ssl.SSLEOFError(8, "EOF occurred in violation of protocol"),
    ],
)
def test_socket_errors_are_retried_like_abort(fake_imap, error):
    fake_imap.failures = {0: ("fetch", error)}
    assert mailbox().recent(limit=5)[0]["uid"] == 7
    assert len(fake_imap.instances) == 2
    assert fake_imap.instances[0].closed


def test_second_drop_reaches_the_caller_without_more_retries(fake_imap):
    error = imaplib.IMAP4.abort("command: FETCH => socket error: EOF")
    fake_imap.failures = {0: ("fetch", error), 1: ("fetch", error), 2: ("fetch", error)}
    with pytest.raises(ConnectionInterrupted, match="dropped twice") as raised:
        mailbox().recent(limit=5)
    assert isinstance(raised.value, imaplib.IMAP4.abort)
    assert raised.value.__cause__ is error
    assert len(fake_imap.instances) == 2
    assert [imap.logins for imap in fake_imap.instances] == [1, 1]
    assert all(imap.closed for imap in fake_imap.instances)


def test_timeout_is_not_retried(fake_imap):
    fake_imap.failures = {0: ("fetch", TimeoutError("timed out"))}
    with pytest.raises(TimeoutError):
        mailbox().recent(limit=5)
    assert len(fake_imap.instances) == 1


def test_imap_error_is_not_retried(fake_imap):
    fake_imap.failures = {0: ("select", imaplib.IMAP4.error("SELECT failed: no such folder"))}
    with pytest.raises(imaplib.IMAP4.error, match="no such folder"):
        mailbox().recent(limit=5)
    assert len(fake_imap.instances) == 1


@pytest.mark.parametrize(
    ("change", "command"),
    [
        (lambda box: box.move(7, "Archive", uidvalidity=None), "move"),
        (lambda box: box.set_flag(7, "\\Flagged", True, uidvalidity=None), "add_flags"),
        (lambda box: box.move_to_promotions(7, confirm=True, uidvalidity=None), "move"),
    ],
)
def test_change_is_never_repeated(fake_imap, monkeypatch, change, command):
    # The session may drop after the server made the change.
    monkeypatch.setattr(Mailbox, "_destination", staticmethod(lambda imap, folder, *_: folder))
    monkeypatch.setattr(
        Mailbox, "validate_promotions_folder", lambda self, imap: ("AI-Promotions", True)
    )
    fake_imap.failures = {0: (command, imaplib.IMAP4.abort("socket error: EOF"))}
    with pytest.raises(imaplib.IMAP4.abort) as raised:
        change(mailbox(allow_write=True))
    assert not isinstance(raised.value, ConnectionInterrupted)
    assert len(fake_imap.instances) == 1
    assert fake_imap.instances[0].calls.count(command) == 1


def test_drop_closes_the_idle_connections_and_retry_opens_a_new_one(fake_imap):
    box = mailbox(imap_pool_size=2)
    with box.connect() as first, box.connect() as second:
        pass
    assert {first.number, second.number} == {0, 1}
    # The pool is last in, first out: first comes out and fails; second is closed
    # without being tried.
    fake_imap.failures = {first.number: ("fetch", imaplib.IMAP4.abort("socket error: EOF"))}
    assert box.recent(limit=5)
    assert first.closed and second.closed
    assert "fetch" in first.calls
    assert second.calls == []
    assert not second.logged_out
    assert len(fake_imap.instances) == 3
    assert not fake_imap.instances[2].closed


def test_retry_does_not_reuse_an_idle_connection(fake_imap):
    box = mailbox(imap_pool_size=2)
    with box.connect():
        pass
    fake_imap.failures = {0: ("fetch", imaplib.IMAP4.abort("socket error: EOF"))}
    # Another request returns a connection to the pool between the two attempts.
    calls = []
    original = box.connect

    def connect(label="imap", *, fresh=False):
        calls.append(fresh)
        if fresh:
            with original(label):
                pass
        return original(label, fresh=fresh)

    box.connect = connect
    assert box.recent(limit=5)
    assert calls == [False, True]
    failed, returned, fresh = fake_imap.instances
    assert failed.closed
    # Closed untried: kept, it would be one connection more than the pool's size.
    assert returned.calls == []
    assert returned.closed
    assert fresh.logins == 1
    assert "fetch" in fresh.calls


def test_retry_keeps_the_pool_size(fake_imap):
    box = mailbox(imap_pool_size=1)
    fake_imap.failures = {0: ("fetch", imaplib.IMAP4.abort("socket error: EOF"))}
    original = box.connect
    open_at_retry = []

    def connect(label="imap", *, fresh=False):
        if not fresh:
            return original(label)
        # Another request runs between the two attempts and leaves its connection idle.
        box.folders()

        @contextmanager
        def retry():
            with original(label, fresh=True) as imap:
                open_at_retry.append(sum(not i.closed for i in fake_imap.instances))
                yield imap

        return retry()

    box.connect = connect
    assert box.recent(limit=5)
    assert open_at_retry == [1]


def test_idle_connections_dropped_by_the_server_then_a_dropped_login(
    fake_imap, monkeypatch, caplog
):
    """The failure seen on Yandex: both idle sessions fail NOOP after about four
    minutes, and the login of a replacement is dropped once."""
    sleeps = []
    monkeypatch.setattr(mail, "LOGIN_RETRY_DELAY", 1.0)
    monkeypatch.setattr(
        mail, "time", SimpleNamespace(sleep=sleeps.append, monotonic=time.monotonic)
    )
    box = mailbox(imap_pool_size=2)
    with box.connect(), box.connect():
        pass
    fake_imap.failures = {
        0: ("noop", imaplib.IMAP4.abort("socket error: [Errno 32] Broken pipe")),
        1: ("noop", imaplib.IMAP4.abort("socket error: [Errno 104] Connection reset by peer")),
        2: ("login", imaplib.IMAP4.abort("socket error: EOF")),
    }
    with caplog.at_level(logging.INFO, logger="yandex_mail_mcp.mail"):
        assert box.recent(limit=5)[0]["uid"] == 7
    assert sleeps == [1.0]
    assert len(fake_imap.instances) == 4
    dropped = fake_imap.instances[2]
    # Closed without LOGOUT, which would wait on a dropped session.
    assert dropped.logins == 0 and dropped.closed and not dropped.logged_out
    assert fake_imap.instances[3].logins == 1 and "fetch" in fake_imap.instances[3].calls
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [
        "IMAP login for recent dropped: abort: socket error: EOF; logging in again in 1 s"
    ]


@pytest.mark.parametrize(
    "error",
    [
        imaplib.IMAP4.abort("socket error: EOF"),
        ConnectionResetError(104, "Connection reset by peer"),
        ssl.SSLEOFError(8, "EOF occurred in violation of protocol"),
    ],
)
def test_dropped_login_logs_in_again(fake_imap, error):
    fake_imap.failures = {0: ("login", error)}
    assert mailbox().recent(limit=5)[0]["uid"] == 7
    assert [imap.logins for imap in fake_imap.instances] == [0, 1]


def test_login_dropped_twice_names_the_reason(fake_imap, caplog):
    fake_imap.failures = {
        0: ("login", imaplib.IMAP4.abort("socket error: EOF")),
        1: ("login", imaplib.IMAP4.abort("Too many simultaneous connections")),
        2: ("login", imaplib.IMAP4.abort("never reached")),
    }
    with (
        caplog.at_level(logging.WARNING, logger="yandex_mail_mcp.mail"),
        pytest.raises(LoginInterrupted) as raised,
    ):
        mailbox().recent(limit=5)
    assert str(raised.value) == ("IMAP login was dropped twice (Too many simultaneous connections)")
    assert isinstance(raised.value, imaplib.IMAP4.abort)
    assert len(fake_imap.instances) == 2
    assert all(imap.closed for imap in fake_imap.instances)
    assert len(caplog.records) == 2
    assert caplog.records[-1].getMessage() == (
        "IMAP login for recent dropped again: abort: Too many simultaneous connections"
    )


@pytest.mark.parametrize(
    "error",
    [imaplib.IMAP4.error("AUTHENTICATIONFAILED"), TimeoutError("timed out")],
)
def test_login_failures_other_than_drops_are_not_retried(fake_imap, error):
    fake_imap.failures = {0: ("login", error)}
    with pytest.raises(type(error)):
        mailbox().recent(limit=5)
    assert len(fake_imap.instances) == 1


def test_connection_returned_during_the_login_pause_is_closed_untried(fake_imap, monkeypatch):
    box = mailbox(imap_pool_size=2)
    returned = []

    def sleep(seconds):
        # Another call opens and returns a connection while this one waits.
        if not returned:
            with box.connect() as imap:
                returned.append(imap)

    monkeypatch.setattr(mail, "time", SimpleNamespace(sleep=sleep, monotonic=time.monotonic))
    fake_imap.failures = {0: ("login", imaplib.IMAP4.abort("socket error: EOF"))}
    assert box.recent(limit=5)[0]["uid"] == 7
    dropped, other, fresh = fake_imap.instances
    assert dropped.closed and not dropped.logged_out
    assert returned == [other]
    assert other.calls == [] and other.closed
    assert "fetch" in fresh.calls and fresh.logins == 1


def test_change_logs_in_again_and_runs_once(fake_imap, monkeypatch):
    # Nothing but the login was sent, so a change may log in again.
    monkeypatch.setattr(Mailbox, "_destination", staticmethod(lambda imap, folder, *_: folder))
    fake_imap.failures = {0: ("login", imaplib.IMAP4.abort("socket error: EOF"))}
    assert mailbox(allow_write=True).move(7, "Archive", uidvalidity=None)["folder"] == "Archive"
    first, second = fake_imap.instances
    assert first.calls == []
    assert second.calls.count("move") == 1


def test_dropped_login_on_the_retry_logs_in_again(fake_imap):
    fake_imap.failures = {
        0: ("fetch", imaplib.IMAP4.abort("socket error: EOF")),
        1: ("login", ConnectionResetError(104, "Connection reset by peer")),
    }
    assert mailbox().recent(limit=5)[0]["uid"] == 7
    assert [imap.logins for imap in fake_imap.instances] == [1, 0, 1]


def test_dropped_session_is_logged_without_mail_content(fake_imap, caplog):
    box = mailbox()
    box.folders()
    fake_imap.failures = {
        0: ("fetch", imaplib.IMAP4.abort("command: FETCH => socket error: EOF")),
    }
    with caplog.at_level(logging.INFO, logger="yandex_mail_mcp.mail"):
        box.recent(limit=5)
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "during recent" in text
    assert "abort: command: FETCH => socket error: EOF" in text
    assert "reused connection" in text
    assert "2 operation(s)" in text
    assert "state SELECTED" in text
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "Retrying recent" in logged
    assert "alice@example.com" not in logged
    assert "Secret subject" not in logged
    assert "x" not in logged.split()  # The token.


def test_noop_replacement_is_logged(fake_imap, caplog):
    box = mailbox()
    box.folders()
    fake_imap.failures = {0: ("noop", imaplib.IMAP4.abort("Autologout; idle for too long"))}
    with caplog.at_level(logging.INFO, logger="yandex_mail_mcp.mail"):
        box.folders()
    assert any(
        "failed NOOP: abort: Autologout; idle for too long" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("unexpected response: b'Subject: Secret subject'", "unexpected response"),
        (
            "command: FETCH => unexpected tagged response: b'A1 Secret subject'",
            "command: FETCH => unexpected tagged response",
        ),
        ("socket error: unterminated line: b'Secret subject'", "socket error: unterminated line"),
        (
            "unexpected response while waiting for continuation response: (b'NO', [b'Secret'])",
            "unexpected response",
        ),
        ("Autologout; idle for too long", "Autologout; idle for too long"),
    ],
)
def test_server_lines_are_not_logged(text, reason):
    assert mail._failure_reason(imaplib.IMAP4.abort(text)) == reason


def test_failure_reason_of_socket_errors_and_long_texts():
    assert mail._failure_reason(ConnectionResetError(104, "Connection reset by peer")) == (
        "Connection reset by peer"
    )
    assert len(mail._failure_reason(imaplib.IMAP4.abort("x" * 500))) == 200


class ExclusiveIMAP(FakeIMAP):
    """Fails the test if two threads ever run commands on one session at once."""

    lock = threading.Lock()
    open_now = 0
    open_max = 0
    overlaps: ClassVar[list[int]] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.busy = False

    def oauth2_login(self, user, token):
        super().oauth2_login(user, token)
        with ExclusiveIMAP.lock:
            ExclusiveIMAP.open_now += 1
            ExclusiveIMAP.open_max = max(ExclusiveIMAP.open_max, ExclusiveIMAP.open_now)

    def _close(self):
        with ExclusiveIMAP.lock:
            if not self.closed:
                ExclusiveIMAP.open_now -= 1
            self.closed = True

    def logout(self):
        self._close()

    def shutdown(self):
        self._close()

    def _command(self, name):
        if self.busy:
            ExclusiveIMAP.overlaps.append(self.number)
        self.busy = True
        try:
            time.sleep(0.002)
            super()._command(name)
        finally:
            self.busy = False


def test_parallel_reads_never_share_a_session(monkeypatch):
    FakeIMAP.instances = []
    # Some sessions drop, so retries run alongside the other requests.
    FakeIMAP.failures = {1: ("fetch", imaplib.IMAP4.abort("socket error: EOF"))}
    ExclusiveIMAP.open_now = ExclusiveIMAP.open_max = 0
    ExclusiveIMAP.overlaps = []
    monkeypatch.setattr(mail, "IMAPClient", ExclusiveIMAP)
    box = mailbox(imap_pool_size=2)
    results, errors = [], []

    def run(index):
        try:
            if index % 2:
                results.append(box.recent(limit=5))
            else:
                results.append(box.search(sender="example.com"))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    box.close()
    assert errors == []
    assert len(results) == 20
    assert all(result[0]["uid"] == 7 for result in results)
    assert ExclusiveIMAP.overlaps == []
    assert ExclusiveIMAP.open_max <= 2
    assert ExclusiveIMAP.open_now == 0
