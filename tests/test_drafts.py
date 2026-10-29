import asyncio
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace

import anyio.to_thread
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from yandex_mail_mcp import server
from yandex_mail_mcp.config import Settings
from yandex_mail_mcp.mail import (
    Mailbox,
    MailError,
    MailLookupError,
    MailPermissionError,
    MailValueError,
)

ORIGINAL = (
    b"Message-ID: <orig@bank.example>\r\n"
    b"In-Reply-To: <second@me.example>\r\n"
    b"References: <first@bank.example> <second@me.example>\r\n"
    b"From: Bank <noreply@bank.example>\r\n"
    b"Reply-To: =?utf-8?b?0J/QvtC00LTQtdGA0LbQutCw?= <support@bank.example>\r\n"
    b"Subject: =?utf-8?b?0JLRi9C/0LjRgdC60LAg0LfQsCDQvtC60YLRj9Cx0YDRjA==?=\r\n"
    b"\r\n"
)


@pytest.fixture(autouse=True)
def run_sync_inline(monkeypatch):
    async def run_sync(fn, *args, **kwargs):
        return fn(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", run_sync)


class DraftIMAP:
    def __init__(self, drafts=True, appenduid=True):
        self.appended = []
        self.drafts = drafts
        self.appenduid = appenduid

    def list_folders(self):
        folders = [((b"\\HasNoChildren",), b"|", "INBOX")]
        if self.drafts:
            folders.append(((b"\\HasNoChildren", b"\\Drafts"), b"|", "Черновики"))
        return folders

    def select_folder(self, name, readonly):
        assert (name, readonly) == ("INBOX", True)

    def fetch(self, uids, fields):
        assert fields == ["BODY.PEEK[HEADER]"]
        return {7: {b"BODY[HEADER]": ORIGINAL}} if uids == [7] else {}

    def append(self, folder, msg, flags=(), msg_time=None):
        self.appended.append((folder, msg, flags, msg_time))
        return b"[APPENDUID 1700000000 42] APPEND Completed." if self.appenduid else b"OK"


def box_with(imap, **settings):
    box = Mailbox(Settings(email="me@yandex.ru", oauth_token="x", **settings))

    @contextmanager
    def connect():
        yield imap

    box.connect = connect
    return box


def parsed(draft):
    return BytesParser(policy=policy.default).parsebytes(draft.message.as_bytes())


def test_draft_has_headers_and_cyrillic_subject_and_body():
    original, draft = box_with(DraftIMAP()).prepare_draft(
        ["Иван Петров <ivan@example.com>"],
        "Счёт за октябрь",
        "Здравствуйте!\nСчёт во вложении не нужен.",
        cc=["boss@example.com"],
    )
    assert original is None
    assert draft.folder == "Черновики"
    raw = draft.message.as_bytes()
    assert raw.isascii()
    assert b"\r\n" in raw
    message = parsed(draft)
    assert message["From"] == "me@yandex.ru"
    assert message["To"] == "Иван Петров <ivan@example.com>"
    assert message["Cc"] == "boss@example.com"
    assert message["Subject"] == "Счёт за октябрь"
    assert message["Message-ID"].endswith("@yandex.ru>")
    assert message["Date"].datetime is not None
    assert message.get_content_type() == "text/plain"
    assert message.get_content_charset() == "utf-8"
    assert message.get_content() == "Здравствуйте!\r\nСчёт во вложении не нужен.\r\n"


def test_reply_sets_threading_headers_and_defaults():
    original, draft = box_with(DraftIMAP()).prepare_draft([], "", "Спасибо", in_reply_to_uid=7)
    assert original["subject"] == "Выписка за октябрь"
    message = parsed(draft)
    assert message["In-Reply-To"] == "<orig@bank.example>"
    assert message["References"] == ("<first@bank.example> <second@me.example> <orig@bank.example>")
    assert message["To"] == "Поддержка <support@bank.example>"
    assert message["Subject"] == "Re: Выписка за октябрь"


def test_reply_keeps_explicit_recipient_and_does_not_repeat_re(monkeypatch):
    box = box_with(DraftIMAP())
    monkeypatch.setattr(
        "yandex_mail_mcp.mail.parse_message",
        lambda raw, uid, max_body_chars: {
            "subject": "RE: Договор",
            "message_id": "<x@y>",
            "references": [],
            "in_reply_to": "",
        },
    )
    _, draft = box.prepare_draft(["a@example.com"], " ", "ok", in_reply_to_uid=7)
    message = parsed(draft)
    assert message["To"] == "a@example.com"
    assert message["Subject"] == "RE: Договор"
    assert message["References"] == "<x@y>"


def test_reply_to_missing_message_fails():
    with pytest.raises(MailLookupError, match="UID 8 not found in INBOX"):
        box_with(DraftIMAP()).prepare_draft([], "", "x", in_reply_to_uid=8)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"to": []}, "at least one address"),
        ({"to": ["not-an-address"]}, "valid email addresses"),
        ({"to": ["a@example.com, b@example.com"]}, "one per item"),
        ({"to": ["a@example.com\r\nBcc: x@example.com"]}, "single lines"),
        ({"cc": ["broken@"]}, "cc must contain"),
        ({"subject": "Hi\r\nBcc: x@example.com"}, "single line"),
        ({"body": "я" * 60000}, "100 KiB"),
    ],
)
def test_invalid_drafts_are_rejected(kwargs, message):
    arguments = {"to": ["a@example.com"], "subject": "s", "body": "b", **kwargs}
    with pytest.raises(MailValueError, match=message):
        box_with(DraftIMAP()).prepare_draft(**arguments)


def test_missing_drafts_folder_is_an_error():
    with pytest.raises(MailError, match="Drafts"):
        box_with(DraftIMAP(drafts=False)).prepare_draft(["a@example.com"], "s", "b")


@pytest.mark.parametrize(("appenduid", "uid"), [(True, 42), (False, None)])
def test_save_appends_with_draft_flag(appenduid, uid):
    imap = DraftIMAP(appenduid=appenduid)
    box = box_with(imap, allow_write=True)
    _, draft = box.prepare_draft(["a@example.com"], "s", "b")
    result = box.save_draft(draft)
    assert result == {"folder": "Черновики", "uid": uid, "message_id": draft.message["Message-ID"]}
    [(folder, raw, flags, msg_time)] = imap.appended
    assert (folder, flags) == ("Черновики", [b"\\Draft"])
    assert raw == draft.message.as_bytes()
    assert msg_time.tzinfo is not None


def test_save_requires_write_opt_in():
    imap = DraftIMAP()
    box = box_with(imap)
    _, draft = box.prepare_draft(["a@example.com"], "s", "b")
    with pytest.raises(MailPermissionError, match="YANDEX_ALLOW_WRITE"):
        box.save_draft(draft)
    assert imap.appended == []


class Client:
    session = SimpleNamespace(check_client_capability=lambda capability: True)

    def __init__(self, action="accept", approve=True):
        self.action, self.approve, self.messages = action, approve, []

    async def elicit(self, message, schema):
        self.messages.append(message)
        return SimpleNamespace(action=self.action, data=SimpleNamespace(approve=self.approve))


def test_tool_saves_the_draft_the_user_approved(monkeypatch):
    imap = DraftIMAP()
    monkeypatch.setattr(server, "_mailbox", lambda: box_with(imap, allow_write=True))
    client = Client()
    result = asyncio.run(server.create_draft([], "", "Спасибо, получил", client, in_reply_to_uid=7))
    assert result["uid"] == 42
    [text] = client.messages
    assert 'To: "Поддержка <support@bank.example>"' in text
    assert 'Subject: "Re: Выписка за октябрь"' in text
    assert 'Text begins: "Спасибо, получил"' in text
    assert 'Sender (from the email, untrusted): "Bank <noreply@bank.example>"' in text
    assert len(imap.appended) == 1


def test_tool_without_approval_saves_nothing(monkeypatch):
    imap = DraftIMAP()
    monkeypatch.setattr(server, "_mailbox", lambda: box_with(imap, allow_write=True))
    with pytest.raises(ToolError, match="Draft was not approved"):
        asyncio.run(server.create_draft(["a@example.com"], "s", "b", Client("decline", False)))
    assert imap.appended == []


def test_tool_can_skip_confirmation(monkeypatch):
    imap = DraftIMAP()
    box = box_with(imap, allow_write=True, confirm_reversible=False)
    monkeypatch.setattr(server, "_mailbox", lambda: box)
    client = Client()
    asyncio.run(server.create_draft(["a@example.com"], "s", "b", client))
    assert client.messages == []
    assert len(imap.appended) == 1


def test_tool_requires_write_opt_in(monkeypatch):
    imap = DraftIMAP()
    monkeypatch.setattr(server, "_mailbox", lambda: box_with(imap))
    with pytest.raises(ToolError, match="YANDEX_ALLOW_WRITE"):
        asyncio.run(server.create_draft(["a@example.com"], "s", "b", Client()))
    assert imap.appended == []


def test_create_draft_annotations():
    tools = {tool.name: tool.annotations for tool in asyncio.run(server.mcp.list_tools())}
    annotations = tools["create_draft"]
    assert annotations.read_only_hint is False
    assert annotations.destructive_hint is False
    assert annotations.idempotent_hint is False


def test_draft_is_from_the_signed_in_user_not_the_server_address():
    from yandex_mail_mcp.credentials import OAuthCredentials, StaticTokenSource

    box = Mailbox(Settings(), OAuthCredentials("anna@company.ru", StaticTokenSource("t")))

    @contextmanager
    def connect():
        yield DraftIMAP()

    box.connect = connect
    _, draft = box.prepare_draft(["a@example.com"], "s", "b")
    assert parsed(draft)["From"] == "anna@company.ru"
    assert draft.message["Message-ID"].endswith("@company.ru>")
