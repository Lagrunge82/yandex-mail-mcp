"""move_emails: one confirmation that lists every message, one MOVE for the batch."""

import asyncio
import imaplib
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
    MAX_BATCH_INPUT,
    MAX_BATCH_MOVE,
    Mailbox,
    MailLookupError,
    MailPermissionError,
    MailValueError,
    batch_uids,
    copyuid_sources,
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

    def __init__(self, copyuid=True, moves=None, refuse=False, vanish=()):
        # Mirrors imaplib: move() returns the (absent) untagged FETCH data, and the
        # COPYUID response code of the tagged OK or NO lands in untagged_responses.
        # copyuid: the server has UIDPLUS; moves: UIDs it actually moves (None: all);
        # refuse: answer NO after moving those; vanish: UIDs another client removes
        # while the MOVE runs.
        self.copyuid = copyuid
        self.moves = moves
        self.refuse = refuse
        self.vanish = set(vanish)
        self._imap = SimpleNamespace(untagged_responses={})
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
        for uid in self.vanish:
            self.inbox.pop(uid, None)
        moved = [
            uid for uid in uids if uid in self.inbox and (self.moves is None or uid in self.moves)
        ]
        self.changes.append(("move", list(uids), folder))
        for uid in moved:
            del self.inbox[uid]
        if self.copyuid and moved:
            sources = ",".join(str(uid) for uid in moved)
            self._imap.untagged_responses["COPYUID"] = [
                f"1587 {sources} 100:{99 + len(moved)}".encode()
            ]
        if self.refuse:
            raise imaplib.IMAP4.error("MOVE failed: [SERVERBUG] Partial move")
        return [None]


def box_with(imap, **settings):
    box = Mailbox(Settings(email="test@yandex.com", oauth_token="x", **settings))

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
    # Duplicates do not count against the cap; too many entries are refused up front.
    assert batch_uids([7] * MAX_BATCH_INPUT) == [7]
    with pytest.raises(MailValueError, match=f"at most {MAX_BATCH_INPUT} entries"):
        batch_uids([7] * (MAX_BATCH_INPUT + 1))


def test_batch_validation_stops_at_the_first_excess_uid():
    class Counting(list):
        def __iter__(self):
            yield from (yielded.append(n) or n for n in super().__iter__())

    yielded = []
    with pytest.raises(MailValueError, match="at most"):
        batch_uids(Counting(range(1, MAX_BATCH_INPUT + 1)))
    assert len(yielded) == MAX_BATCH_MOVE + 1


def test_copyuid_sources_are_parsed_or_absent():
    # As imaplib stores the response code: the data after the code name.
    assert copyuid_sources([b"1587 3,5:7 100:103"]) == {3, 5, 6, 7}
    assert copyuid_sources(b"2 4 9") == {4}
    assert copyuid_sources(b"COPYUID 2 4 9") == {4}
    # RFC 4315: a range may name its high end first.
    assert copyuid_sources(b"1 5:3 10:12") == {3, 4, 5}
    assert copyuid_sources([b"Move completed."]) is None
    assert copyuid_sources(None) is None
    assert copyuid_sources([b"1 1:99999999 1:99999999"]) is None


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
        "moved": [3, 4],
        "not_moved": [],
        "folder": "github|ci/cd",
        "action": "move",
        "reversible": True,
    }
    assert imap.changes == [("move", [3, 4], "github|ci/cd")]
    assert imap.selected[-1] == ("INBOX", False)


def test_copyuid_is_read_from_imaplibs_response_codes_and_cleared_first():
    imap = BatchIMAP()
    imap._imap.untagged_responses["COPYUID"] = [b"1 999 1"]  # left by an earlier command
    result = box_with(imap, allow_write=True).move_many([3, 4], "github|ci/cd")
    assert (result["moved"], result["not_moved"]) == ([3, 4], [])
    assert "warning" not in result
    assert "COPYUID" not in imap._imap.untagged_responses


def test_partial_move_is_reported_from_copyuid_even_when_the_server_says_no():
    imap = BatchIMAP(moves=[3])
    result = box_with(imap, allow_write=True).move_many([3, 4, 5], "github|ci/cd")
    assert (result["moved"], result["not_moved"]) == ([3], [4, 5])
    assert "moved only part of the batch; 2 messages are still in INBOX" in result["warning"]
    refused = BatchIMAP(moves=[4], refuse=True)
    result = box_with(refused, allow_write=True).move_many([3, 4], "github|ci/cd")
    assert (result["moved"], result["not_moved"]) == ([4], [3])
    assert "Partial move" in result["warning"]
    # Nothing moved before the refusal: the error is the caller's to see.
    with pytest.raises(imaplib.IMAP4.error, match="Partial move"):
        box_with(BatchIMAP(moves=[], refuse=True), allow_write=True).move_many([3], "github")


def test_message_removed_by_another_client_is_not_counted_as_moved():
    imap = BatchIMAP(vanish=[4])
    result = box_with(imap, allow_write=True).move_many([3, 4], "github|ci/cd")
    assert (result["moved"], result["not_moved"]) == ([3], [4])
    assert "1 messages are still in INBOX" in result["warning"]


def test_without_copyuid_the_outcome_is_undetermined_but_the_source_is_reported():
    imap = BatchIMAP(copyuid=False, moves=[3], vanish=[4])
    result = box_with(imap, allow_write=True).move_many([3, 4, 5], "github|ci/cd")
    assert result["moved"] is None
    assert result["left_source"] == [3, 4]  # moved, or removed by another client
    assert result["not_moved"] == [5]
    assert "no COPYUID" in result["warning"] and "check the destination" in result["warning"]
    # A NO without COPYUID and nothing gone from the source is a plain error.
    with pytest.raises(imaplib.IMAP4.error):
        box_with(BatchIMAP(copyuid=False, moves=[], refuse=True), allow_write=True).move_many(
            [3], "github"
        )

    class Unreadable(BatchIMAP):
        def search(self, criteria):
            if self.changes:
                raise imaplib.IMAP4.error("SEARCH failed")
            return super().search(criteria)

    imap = Unreadable(copyuid=False)
    result = box_with(imap, allow_write=True).move_many([3, 4], "github|ci/cd")
    assert (result["moved"], result["not_moved"], result["left_source"]) == (None, None, None)
    assert "check both folders" in result["warning"]


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
    assert record["uids"] == [3, 4]
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
        "moved": [3, 5],
        "not_moved": [],
        "folder": "github",
        "action": "move",
        "reversible": True,
    }
    assert imap.changes == [("move", [3, 5], "github")]


def test_audit_records_the_batch_as_the_operation_used_it(monkeypatch, caplog):
    imap = BatchIMAP()
    monkeypatch.setattr(server, "_mailbox", lambda: box_with(imap, allow_write=True))
    with caplog.at_level(logging.INFO, logger="yandex_mail_mcp.audit"):
        asyncio.run(server.move_emails([3] * 25 + [4, 5], "github|ci/cd", Client()))
    assert imap.changes == [("move", [3, 4, 5], "github|ci/cd")]
    assert json.loads(caplog.records[-1].getMessage())["uids"] == [3, 4, 5]


def test_move_emails_is_listed_as_a_mutation_with_a_bounded_input():
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    tool = tools["move_emails"]
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is False
    uids = tool.input_schema["properties"]["uids"]
    assert (uids["minItems"], uids["maxItems"]) == (1, MAX_BATCH_INPUT)
