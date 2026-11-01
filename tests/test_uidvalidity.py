"""A confirmed change acts on the confirmed messages, or not at all.

A UID names a message only together with its folder's UIDVALIDITY. When a folder
is deleted and created again between the confirmation and the change, the same
UID may belong to another message: the change must stop instead of moving or
flagging it.
"""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_permissions import signed_in

from yandex_mail_mcp import server
from yandex_mail_mcp.config import Settings
from yandex_mail_mcp.confirmations import ConfirmationFailed, PendingActions
from yandex_mail_mcp.credentials import OAuthCredentials, StaticTokenSource
from yandex_mail_mcp.mail import Mailbox, MailChangedError

FULL = "mail:imap_full login:info login:email"


def header(subject):
    return f"From: sender@example.com\r\nSubject: {subject}\r\n\r\n".encode()


class FolderIMAP:
    """An IMAP server whose folders have a UIDVALIDITY and can be created again."""

    def __init__(self, report_uidvalidity=True):
        self.report_uidvalidity = report_uidvalidity
        self.folders = {
            "INBOX": [100, {3: header("Confirmed"), 5: header("Also confirmed")}],
            "Archive": [200, {}],
            "Receipts": [300, {}],
        }
        self.selected = None
        self.changes = []

    def recreate(self, folder, uidvalidity):
        """Delete the folder and create it again; new mail there reuses the old UIDs."""
        self.folders[folder] = [uidvalidity, {3: header("Another"), 5: header("Other")}]

    def list_folders(self):
        return [
            ((b"\\HasNoChildren",), b"|", "INBOX"),
            ((b"\\HasNoChildren", b"\\Archive"), b"|", "Archive"),
            ((b"\\HasNoChildren",), b"|", "Receipts"),
        ]

    def has_capability(self, name):
        return name in {"MOVE", "UNSELECT"}

    def select_folder(self, name, readonly=False):
        self.selected = name
        uidvalidity, messages = self.folders[name]
        response = {b"EXISTS": len(messages), b"READ-WRITE": not readonly}
        if self.report_uidvalidity:
            response[b"UIDVALIDITY"] = uidvalidity
        return response

    def fetch(self, uids, fields):
        messages = self.folders[self.selected][1]
        return {uid: {b"BODY[HEADER]": messages[uid]} for uid in uids if uid in messages}

    def search(self, criteria, charset=None):
        wanted = {int(uid) for uid in criteria[1].split(",")}
        return sorted(wanted & set(self.folders[self.selected][1]))

    def move(self, uids, folder):
        self.changes.append(("move", self.folders[self.selected][1][uids[0]], folder))

    def add_flags(self, uids, flags):
        self.changes.append(("add_flags", self.folders[self.selected][1][uids[0]], flags))

    def remove_flags(self, uids, flags):
        self.changes.append(("remove_flags", self.folders[self.selected][1][uids[0]], flags))


@pytest.fixture
def imap(monkeypatch):
    imap = FolderIMAP()

    @contextmanager
    def connect(self):
        yield imap

    monkeypatch.setattr(Mailbox, "connect", connect)
    monkeypatch.setattr(server, "_mailboxes", {})
    monkeypatch.setattr(server, "_mailbox_used", {})
    monkeypatch.setattr(server, "_settings", lambda: Settings(allow_write=True))
    pending = PendingActions()
    monkeypatch.setattr(server, "_confirmations", pending)
    monkeypatch.setattr(
        server,
        "_auth_provider",
        SimpleNamespace(
            confirm_url=lambda action_id: f"https://mail.example/confirm/{action_id}",
            authorization_revoked=lambda authorization: False,
        ),
    )
    imap.pending = pending
    return imap


class Asks:
    """A client that asks the user; the folder may be created again while they decide."""

    session = SimpleNamespace(
        check_client_capability=lambda capability: True, can_send_request=True
    )

    def __init__(self, imap, recreate=None):
        self.imap = imap
        self.recreate = recreate
        self.asked = []

    async def elicit(self, message, schema):
        self.asked.append(message)
        if self.recreate is not None:
            self.imap.recreate(self.recreate, 999)
        return SimpleNamespace(action="accept", data=SimpleNamespace(approve=True))


class CannotAsk:
    session = SimpleNamespace(
        check_client_capability=lambda capability: False, can_send_request=True
    )


# Each changing tool, called on UID 3 (and 5 for the batch) in INBOX.
CHANGES = {
    "move_email": lambda ctx: server.move_email(3, "Receipts", ctx),
    "move_emails": lambda ctx: server.move_emails([3, 5], "Receipts", ctx),
    "archive_email": lambda ctx: server.archive_email(3, ctx),
    "mark_read": lambda ctx: server.mark_read(3, ctx),
    "set_flag": lambda ctx: server.set_flag(3, ctx),
}


@pytest.mark.parametrize("tool", CHANGES)
def test_folder_created_again_during_elicitation_changes_nothing(imap, tool):
    client = Asks(imap, recreate="INBOX")
    with signed_in(FULL), pytest.raises(ToolError, match="changed after the confirmation"):
        asyncio.run(CHANGES[tool](client))
    assert client.asked, "the user was asked"
    assert imap.changes == []


@pytest.mark.parametrize("tool", CHANGES)
def test_folder_created_again_before_browser_approval_changes_nothing(imap, tool):
    with signed_in(FULL):
        parked = asyncio.run(CHANGES[tool](CannotAsk()))
    imap.recreate("INBOX", 999)
    waiting = imap.pending.claim(parked["confirm_url"].rsplit("/", 1)[1], "ivan", "approve")
    with pytest.raises(ConfirmationFailed, match="ask again"):
        waiting.action()
    assert imap.changes == []


@pytest.mark.parametrize("tool", CHANGES)
def test_unchanged_folder_is_changed_as_confirmed(imap, tool):
    with signed_in(FULL):
        asyncio.run(CHANGES[tool](Asks(imap)))
        parked = asyncio.run(CHANGES[tool](CannotAsk()))
    waiting = imap.pending.claim(parked["confirm_url"].rsplit("/", 1)[1], "ivan", "approve")
    waiting.action()
    # Both times the confirmed message, never another one.
    assert len(imap.changes) == 2
    assert {change[1] for change in imap.changes} == {header("Confirmed")}


def test_another_folder_created_again_does_not_matter(imap):
    # Only the folder whose UIDs were confirmed is pinned; the destination is
    # checked by name again, as before.
    with signed_in(FULL):
        result = asyncio.run(server.move_email(3, "Receipts", Asks(imap, recreate="Receipts")))
    assert result["folder"] == "Receipts"
    assert imap.changes == [("move", header("Confirmed"), "Receipts")]


def test_server_that_never_reports_uidvalidity_works_as_before(imap):
    imap.report_uidvalidity = False
    with signed_in(FULL):
        asyncio.run(server.mark_read(3, Asks(imap)))
    assert imap.changes == [("add_flags", header("Confirmed"), ["\\Seen"])]


def test_uidvalidity_missing_at_the_change_is_refused():
    imap = FolderIMAP()
    box = Mailbox(
        Settings(allow_write=True),
        OAuthCredentials("ivan@yandex.ru", StaticTokenSource("ya-token", FULL)),
    )
    box.connect = contextmanager(lambda: (yield imap))
    _, target = box.preview_flag(3)
    assert target.uidvalidity == 100
    imap.report_uidvalidity = False
    with pytest.raises(MailChangedError, match="Nothing was changed"):
        box.set_flag(3, "\\Seen", True, uidvalidity=target.uidvalidity)
    assert imap.changes == []
