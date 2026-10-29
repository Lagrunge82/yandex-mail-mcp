"""move_emails: one confirmation that lists every message, one MOVE for the batch."""

import asyncio
import json
import logging
from contextlib import contextmanager
from types import SimpleNamespace

import anyio.to_thread
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_permissions import signed_in

from yandex_mail_mcp import server
from yandex_mail_mcp.config import Settings
from yandex_mail_mcp.confirmations import PendingActions
from yandex_mail_mcp.mail import (
    MAX_BATCH_MOVE,
    Mailbox,
    MailLookupError,
    MailPermissionError,
    MailValueError,
    batch_uids,
)

FULL = "mail:imap_full login:info login:email"


@pytest.fixture(autouse=True)
def run_sync_inline(monkeypatch):
    async def run_sync(fn, *args, **kwargs):
        return fn(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", run_sync)


def header(subject, sender):
    return f"Subject: {subject}\r\nFrom: {sender}\r\n\r\n".encode()


class BatchIMAP:
    """A folder of distinct messages; records fetches, selections and moves."""

    def __init__(self):
        self.inbox = {
            3: header("Run failed: CI (0bac95a)", "Lagrunge <notifications@github.com>"),
            4: header("Run failed: Build image", "Lagrunge <notifications@github.com>"),
            5: header("Re: PR #70 review", "emirnonstop <notifications@github.com>"),
        }
        self.folders = [
            ((b"\\HasNoChildren",), b"|", "INBOX"),
            ((b"\\HasChildren",), b"|", "github"),
            ((b"\\HasNoChildren",), b"|", "github|ci/cd"),
            ((b"\\HasNoChildren", b"\\Trash"), b"|", "Удалённые"),
        ]
        self.changes = []
        self.selected = []
        self.fetches = []

    def list_folders(self):
        return self.folders

    def has_capability(self, name):
        return True

    def select_folder(self, name, readonly):
        self.selected.append((name, readonly))

    def fetch(self, uids, fields):
        assert fields == ["BODY.PEEK[HEADER]"]
        self.fetches.append(list(uids))
        return {uid: {b"BODY[HEADER]": self.inbox[uid]} for uid in uids if uid in self.inbox}

    def search(self, criteria):
        assert criteria[0] == "UID"
        wanted = {int(part) for part in criteria[1].split(",")}
        return [uid for uid in self.inbox if uid in wanted]

    def move(self, uids, folder):
        self.changes.append(("move", list(uids), folder))


def box_with(imap, **settings):
    box = Mailbox(Settings(email="test@yandex.com", app_password="x", **settings))

    @contextmanager
    def connect():
        yield imap

    box.connect = connect
    return box


class Client:
    def __init__(self, elicitation=True, approve=True):
        self.session = SimpleNamespace(check_client_capability=lambda capability: elicitation)
        self.approve = approve
        self.messages = []

    async def elicit(self, message, schema):
        self.messages.append(message)
        return SimpleNamespace(action="accept", data=SimpleNamespace(approve=self.approve))


def test_batch_uids_are_validated_deduplicated_and_capped():
    assert batch_uids([4, 3, 4, 5]) == [4, 3, 5]
    with pytest.raises(MailValueError, match="at least one"):
        batch_uids([])
    with pytest.raises(MailValueError, match="positive integer"):
        batch_uids([3, 0])
    with pytest.raises(MailValueError, match="positive integer"):
        batch_uids([True])
    with pytest.raises(MailValueError, match=f"at most {MAX_BATCH_MOVE}"):
        batch_uids(list(range(1, MAX_BATCH_MOVE + 2)))
    assert len(batch_uids(list(range(1, MAX_BATCH_MOVE + 1)))) == MAX_BATCH_MOVE


def test_preview_reads_every_header_at_once_and_names_missing_messages():
    imap = BatchIMAP()
    box = box_with(imap, allow_write=True)
    messages, destination = box.preview_moves([4, 3], "github|ci/cd")
    assert [m["uid"] for m in messages] == [4, 3]
    assert messages[0]["subject"] == "Run failed: Build image"
    assert destination == "github|ci/cd"
    assert imap.fetches == [[4, 3]]
    assert imap.selected == [("INBOX", True)]
    with pytest.raises(MailLookupError, match="UID 9, 10"):
        box.preview_moves([3, 9, 10], "github|ci/cd")
    with pytest.raises(MailValueError, match="cannot be moved"):
        box.preview_moves([3], "Удалённые")
    with pytest.raises(MailValueError, match="already in that folder"):
        box.preview_moves([3], "inbox")
    assert imap.changes == []


def test_move_many_issues_one_move_for_the_whole_batch():
    imap = BatchIMAP()
    box = box_with(imap, allow_write=True)
    result = box.move_many([3, 4], "github|ci/cd")
    assert result == {
        "uids": [3, 4],
        "folder": "github|ci/cd",
        "action": "move",
        "reversible": True,
    }
    assert imap.changes == [("move", [3, 4], "github|ci/cd")]
    assert imap.selected[-1] == ("INBOX", False)


def test_move_many_moves_nothing_when_a_message_is_missing_or_writes_are_off():
    imap = BatchIMAP()
    with pytest.raises(MailLookupError, match="UID 9"):
        box_with(imap, allow_write=True).move_many([3, 9], "github|ci/cd")
    with pytest.raises(MailPermissionError):
        box_with(imap).move_many([3], "github|ci/cd")
    assert imap.changes == []


def test_tool_lists_every_message_in_one_confirmation(monkeypatch, caplog):
    imap = BatchIMAP()
    monkeypatch.setattr(server, "_mailbox", lambda: box_with(imap, allow_write=True))
    client = Client()
    with caplog.at_level(logging.INFO, logger="yandex_mail_mcp.audit"):
        result = asyncio.run(server.move_emails([3, 4, 3], "github|ci/cd", client))
    assert result["uids"] == [3, 4]
    assert imap.changes == [("move", [3, 4], "github|ci/cd")]
    [text] = client.messages
    lines = text.splitlines()
    assert lines[0].startswith("Move 2 messages to another folder?")
    assert lines[1] == 'From folder (requested by the assistant): "INBOX"'
    assert lines[2] == 'To folder (requested by the assistant): "github|ci/cd"'
    assert "The 2 messages below are copied from the emails: untrusted content" in lines[3]
    assert lines[4] == (
        '1. UID 3: sender "Lagrunge <notifications@github.com>", subject "Run failed: CI (0bac95a)"'
    )
    assert lines[5].startswith('2. UID 4: sender "Lagrunge <notifications@github.com>"')
    record = json.loads(caplog.records[-1].getMessage())
    assert record["tool"] == "move_emails"
    assert record["uids"] == [3, 4, 3]
    assert record["folder"] == "github|ci/cd"
    assert record["result"] == "ok"


def test_tool_moves_nothing_without_approval_or_past_the_cap(monkeypatch):
    imap = BatchIMAP()
    monkeypatch.setattr(server, "_mailbox", lambda: box_with(imap, allow_write=True))
    with pytest.raises(ToolError, match="not approved"):
        asyncio.run(server.move_emails([3, 4], "github|ci/cd", Client(approve=False)))
    client = Client()
    with pytest.raises(ToolError, match=f"at most {MAX_BATCH_MOVE}"):
        asyncio.run(server.move_emails(list(range(1, 27)), "github|ci/cd", client))
    assert client.messages == []
    assert imap.changes == []


def test_tool_requires_write_access_before_asking(monkeypatch):
    imap = BatchIMAP()
    monkeypatch.setattr(server, "_mailbox", lambda: box_with(imap))
    client = Client()
    with pytest.raises(ToolError, match="YANDEX_ALLOW_WRITE"):
        asyncio.run(server.move_emails([3], "github|ci/cd", client))
    assert client.messages == [] and imap.changes == []


def test_batch_from_a_client_without_elicitation_waits_for_the_browser(monkeypatch):
    imap = BatchIMAP()

    @contextmanager
    def connect(self):
        yield imap

    monkeypatch.setattr(Mailbox, "connect", connect)
    monkeypatch.setattr(server, "_mailboxes", {})
    monkeypatch.setattr(server, "_settings", lambda: Settings(allow_write=True))
    pending = PendingActions()
    monkeypatch.setattr(server, "_confirmations", pending)
    monkeypatch.setattr(
        server,
        "_auth_provider",
        SimpleNamespace(confirm_url=lambda action_id: f"https://mail.example/confirm/{action_id}"),
    )
    with signed_in(FULL):
        parked = asyncio.run(server.move_emails([3, 5], "github", Client(elicitation=False)))
    assert parked["status"] == "confirmation_required"
    assert imap.changes == []
    action_id = parked["confirm_url"].rsplit("/", 1)[1]
    waiting = pending.get(action_id)
    assert "UID 3" in waiting.question and "UID 5" in waiting.question
    assert 'subject "Re: PR #70 review"' in waiting.question
    assert pending.claim(action_id, "ivan", "approve") is waiting
    assert waiting.action() == {
        "uids": [3, 5],
        "folder": "github",
        "action": "move",
        "reversible": True,
    }
    assert imap.changes == [("move", [3, 5], "github")]


def test_move_emails_is_listed_as_a_mutation():
    tools = {tool.name: tool.annotations for tool in asyncio.run(server.mcp.list_tools())}
    annotations = tools["move_emails"]
    assert annotations.read_only_hint is False
    assert annotations.destructive_hint is False
