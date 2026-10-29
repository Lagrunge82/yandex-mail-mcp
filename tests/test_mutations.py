import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import anyio.to_thread
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from yandex_mail_mcp import server
from yandex_mail_mcp.config import Settings
from yandex_mail_mcp.mail import Mailbox, MailLookupError, MailPermissionError, MailValueError

HEADER = b"Subject: Sale\r\nFrom: shop@example.com\r\n\r\n"


@pytest.fixture(autouse=True)
def run_sync_inline(monkeypatch):
    async def run_sync(fn, *args, **kwargs):
        return fn(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", run_sync)


class WriteIMAP:
    """Records the commands that change mail; everything else just answers."""

    def __init__(self, folders=None, capabilities=("MOVE",), inbox=(3,)):
        self.folders = folders or [
            ((b"\\HasNoChildren",), b"|", "INBOX"),
            ((b"\\HasChildren",), b"|", "Налоги"),
            ((b"\\HasNoChildren",), b"|", "Налоги|2026"),
            ((b"\\HasNoChildren", b"\\Archive"), b"|", "Архив"),
            ((b"\\HasChildren", b"\\Trash"), b"|", "Удалённые"),
            ((b"\\HasNoChildren",), b"|", "Удалённые|Старое"),
            ((b"\\HasNoChildren", b"\\Junk"), b"|", "Спам"),
            ((b"\\HasNoChildren", b"\\Sent"), b"|", "Отправленные"),
            ((b"\\HasNoChildren", b"\\Drafts"), b"|", "Черновики"),
            ((b"\\HasNoChildren",), b"|", "Trash"),
        ]
        self.capabilities = set(capabilities)
        self.inbox = list(inbox)
        self.changes = []
        self.selected = []

    def list_folders(self):
        return self.folders

    def has_capability(self, name):
        return name in self.capabilities

    def select_folder(self, name, readonly):
        self.selected.append((name, readonly))

    def fetch(self, uids, fields):
        assert fields == ["BODY.PEEK[HEADER]"]
        return {uid: {b"BODY[HEADER]": HEADER} for uid in uids if uid in self.inbox}

    def search(self, criteria):
        return [uid for uid in self.inbox if criteria == ["UID", str(uid)]]

    def move(self, uids, folder):
        self.changes.append(("move", uids, folder))

    def add_flags(self, uids, flags):
        self.changes.append(("add_flags", uids, flags))

    def remove_flags(self, uids, flags):
        self.changes.append(("remove_flags", uids, flags))


def box_with(imap, **settings):
    box = Mailbox(Settings(email="test@yandex.com", app_password="x", **settings))

    @contextmanager
    def connect():
        yield imap

    box.connect = connect
    return box


class Client:
    def __init__(self, action="accept", approve=True):
        self.action = action
        self.approve = approve
        self.messages = []

    async def elicit(self, message, schema):
        self.messages.append(message)
        return SimpleNamespace(action=self.action, data=SimpleNamespace(approve=self.approve))


TOOLS = {
    "move_email": (
        lambda ctx: server.move_email(3, "налоги|2026", ctx),
        ("move", [3], "Налоги|2026"),
        {"uid": 3, "folder": "Налоги|2026", "action": "move", "reversible": True},
    ),
    "archive_email": (
        lambda ctx: server.archive_email(3, ctx),
        ("move", [3], "Архив"),
        {"uid": 3, "folder": "Архив", "action": "archive", "reversible": True},
    ),
    "mark_read": (
        lambda ctx: server.mark_read(3, ctx),
        ("add_flags", [3], ["\\Seen"]),
        {"uid": 3, "folder": "INBOX", "action": "mark_read", "reversible": True},
    ),
    "mark_unread": (
        lambda ctx: server.mark_read(3, ctx, read=False),
        ("remove_flags", [3], ["\\Seen"]),
        {"uid": 3, "folder": "INBOX", "action": "mark_unread", "reversible": True},
    ),
    "set_flag": (
        lambda ctx: server.set_flag(3, ctx),
        ("add_flags", [3], ["\\Flagged"]),
        {"uid": 3, "folder": "INBOX", "action": "flag", "reversible": True},
    ),
    "unflag": (
        lambda ctx: server.set_flag(3, ctx, flagged=False),
        ("remove_flags", [3], ["\\Flagged"]),
        {"uid": 3, "folder": "INBOX", "action": "unflag", "reversible": True},
    ),
}


@pytest.mark.parametrize("tool", TOOLS)
def test_mutation_runs_one_command_after_approval(monkeypatch, tool):
    call, change, result = TOOLS[tool]
    imap = WriteIMAP()
    monkeypatch.setattr(server, "_mailbox", lambda: box_with(imap, allow_write=True))
    client = Client()
    assert asyncio.run(call(client)) == result
    assert imap.changes == [change]
    assert len(client.messages) == 1
    assert 'Subject (from the email, untrusted): "Sale"' in client.messages[0]


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(
    ("action", "approve"), [("decline", False), ("cancel", False), ("accept", False)]
)
def test_mutation_without_approval_changes_nothing(monkeypatch, tool, action, approve):
    call, _, _ = TOOLS[tool]
    imap = WriteIMAP()
    monkeypatch.setattr(server, "_mailbox", lambda: box_with(imap, allow_write=True))
    with pytest.raises(ToolError, match="not approved"):
        asyncio.run(call(Client(action, approve)))
    assert imap.changes == []


@pytest.mark.parametrize("tool", TOOLS)
def test_mutation_requires_write_opt_in(monkeypatch, tool):
    call, _, _ = TOOLS[tool]
    imap = WriteIMAP()
    monkeypatch.setattr(server, "_mailbox", lambda: box_with(imap))
    client = Client()
    with pytest.raises(ToolError, match="YANDEX_ALLOW_WRITE"):
        asyncio.run(call(client))
    assert client.messages == []
    assert imap.changes == []


@pytest.mark.parametrize("tool", ["mark_read", "mark_unread", "set_flag", "unflag"])
def test_flag_changes_can_skip_confirmation(monkeypatch, tool):
    call, change, _ = TOOLS[tool]
    imap = WriteIMAP()
    box = box_with(imap, allow_write=True, confirm_reversible=False)
    monkeypatch.setattr(server, "_mailbox", lambda: box)
    client = Client()
    asyncio.run(call(client))
    assert client.messages == []
    assert imap.changes == [change]


def test_moves_are_always_confirmed(monkeypatch):
    imap = WriteIMAP()
    box = box_with(imap, allow_write=True, confirm_reversible=False)
    monkeypatch.setattr(server, "_mailbox", lambda: box)
    client = Client(action="decline", approve=False)
    with pytest.raises(ToolError, match="not approved"):
        asyncio.run(server.move_email(3, "Налоги", client))
    assert len(client.messages) == 1


@pytest.mark.parametrize(
    "folder", ["Удалённые", "Удалённые|Старое", "Спам", "Отправленные", "Черновики", "trash"]
)
def test_move_to_system_folder_is_rejected_before_elicitation(monkeypatch, folder):
    imap = WriteIMAP()
    monkeypatch.setattr(server, "_mailbox", lambda: box_with(imap, allow_write=True))
    client = Client()
    with pytest.raises(ToolError, match="cannot be moved"):
        asyncio.run(server.move_email(3, folder, client))
    assert client.messages == []
    assert imap.changes == []


def test_move_elicitation_names_folders_and_quotes_headers(monkeypatch):
    imap = WriteIMAP()
    monkeypatch.setattr(server, "_mailbox", lambda: box_with(imap, allow_write=True))
    client = Client(action="decline")
    with pytest.raises(ToolError):
        asyncio.run(server.move_email(3, "Налоги", client))
    lines = client.messages[0].splitlines()
    assert lines[1] == 'From folder (requested by the assistant): "INBOX"'
    assert lines[2] == 'To folder (requested by the assistant): "Налоги"'
    assert lines[4] == 'Sender (from the email, untrusted): "shop@example.com"'


def test_move_to_missing_folder_is_not_created():
    box = box_with(WriteIMAP(), allow_write=True)
    with pytest.raises(MailLookupError, match="not found; moves do not create folders"):
        box.preview_move(3, "Новая")
    with pytest.raises(MailLookupError, match="not found"):
        box.move(3, "Новая")


def test_move_into_the_source_folder_is_rejected():
    box = box_with(WriteIMAP(), allow_write=True)
    with pytest.raises(MailValueError, match="already in that folder"):
        box.preview_move(3, "inbox")


def test_move_requires_move_capability():
    box = box_with(WriteIMAP(capabilities=()), allow_write=True)
    with pytest.raises(Exception, match="MOVE"):
        box.move(3, "Налоги")


def test_move_of_missing_uid_changes_nothing():
    imap = WriteIMAP(inbox=())
    with pytest.raises(MailLookupError, match="UID 3 not found in INBOX"):
        box_with(imap, allow_write=True).move(3, "Налоги")
    assert imap.changes == []
    assert imap.selected == [("INBOX", False)]


def test_mailbox_mutations_require_write_opt_in():
    box = box_with(WriteIMAP())
    with pytest.raises(MailPermissionError):
        box.move(3, "Налоги")
    with pytest.raises(MailPermissionError):
        box.set_flag(3, "\\Seen", True)


def test_only_seen_and_flagged_can_be_set():
    box = box_with(WriteIMAP(), allow_write=True)
    with pytest.raises(MailValueError, match="Seen"):
        box.set_flag(3, "\\Deleted", True)


def test_archive_uses_configured_folder():
    imap = WriteIMAP()
    box = box_with(imap, allow_write=True, archive_folder="Налоги")
    assert box.move(3, None)["folder"] == "Налоги"
    assert imap.changes == [("move", [3], "Налоги")]


def test_archive_without_archive_folder_fails_clearly():
    imap = WriteIMAP(folders=[((b"\\HasNoChildren",), b"|", "INBOX")])
    with pytest.raises(MailLookupError, match="YANDEX_ARCHIVE_FOLDER"):
        box_with(imap, allow_write=True).preview_move(3, None)


def test_configured_archive_folder_must_be_safe():
    box = box_with(WriteIMAP(), allow_write=True, archive_folder="Спам")
    with pytest.raises(MailValueError, match="cannot be moved"):
        box.preview_move(3, None)


def test_tools_have_mutation_annotations():
    tools = {tool.name: tool.annotations for tool in asyncio.run(server.mcp.list_tools())}
    for name in ("list_folders", "list_recent_emails", "search_emails", "get_email", "get_thread"):
        assert tools[name].read_only_hint, name
    for name in ("move_email", "archive_email", "mark_read", "set_flag", "move_to_promotions"):
        annotations = tools[name]
        assert annotations.read_only_hint is False, name
        assert annotations.destructive_hint is False, name


def test_archive_lists_folders_once_per_connection():
    imap = WriteIMAP()
    calls = []
    folders = imap.list_folders
    imap.list_folders = lambda: calls.append(1) or folders()
    box = box_with(imap, allow_write=True)
    box.preview_move(3, None)
    assert len(calls) == 1
    box.move(3, None)
    assert len(calls) == 2
