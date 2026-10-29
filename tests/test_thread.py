from contextlib import contextmanager

import pytest

from yandex_mail_mcp.config import Settings
from yandex_mail_mcp.mail import Mailbox, MailValueError, parse_message

TEXT = (b"TEXT", b"PLAIN", (b"CHARSET", b"UTF-8"), None, None, b"7BIT", 4, 1, None, None)


def header(message_id, date, in_reply_to="", references=""):
    lines = [f"Message-ID: {message_id}", f"Date: {date}", "Subject: Re: Договор"]
    if in_reply_to:
        lines.append(f"In-Reply-To: {in_reply_to}")
    if references:
        lines.append(f"References: {references}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


class ThreadIMAP:
    """Evaluates the SEARCH keys get_thread and pagination use over in-memory folders."""

    def __init__(self, folders):
        self.folders = folders
        self.selected = None
        self.searches = []

    def list_folders(self):
        return [
            ((b"\\HasNoChildren",), b"|", "INBOX"),
            ((b"\\HasNoChildren", b"\\Sent"), b"|", "Отправленные"),
            ((b"\\HasNoChildren",), b"|", "Other"),
        ]

    def select_folder(self, name, readonly):
        assert readonly
        self.selected = name

    def search(self, criteria, charset=None):
        self.searches.append((self.selected, criteria))
        tokens = list(criteria)
        predicates = []
        while tokens:
            predicates.append(self._parse(tokens))
        return [
            uid
            for uid, raw in self.folders[self.selected].items()
            if all(predicate(uid, raw) for predicate in predicates)
        ]

    def _parse(self, tokens):
        token = tokens.pop(0)
        if token == "OR":
            left, right = self._parse(tokens), self._parse(tokens)
            return lambda uid, raw: left(uid, raw) or right(uid, raw)
        if token == "HEADER":
            name, value = tokens.pop(0), tokens.pop(0)
            return lambda uid, raw: any(
                line.lower().startswith(name.lower() + ":") and value in line
                for line in raw.decode().splitlines()
            )
        if token == "UID":
            low, high = map(int, tokens.pop(0).split(":"))
            return lambda uid, raw: low <= uid <= high
        if token == "TEXT":
            value = tokens.pop(0)
            return lambda uid, raw: value in raw.decode()
        if token == "ALL":
            return lambda uid, raw: True
        if token == "UNSEEN":
            return lambda uid, raw: uid % 2 == 1
        raise AssertionError(f"unexpected search key {token}")

    def fetch(self, uids, fields):
        folder = self.folders[self.selected]
        if fields == ["BODY.PEEK[HEADER]"]:
            return {uid: {b"BODY[HEADER]": folder[uid]} for uid in uids if uid in folder}
        if fields == ["BODY.PEEK[HEADER]", "BODYSTRUCTURE"]:
            return {
                uid: {b"BODY[HEADER]": folder[uid], b"BODYSTRUCTURE": TEXT}
                for uid in uids
                if uid in folder
            }
        return {uid: {b"BODY[1]<0>": b"text"} for uid in uids}


def mailbox(imap):
    box = Mailbox(Settings(email="test@yandex.com", app_password="x"))

    @contextmanager
    def connect():
        yield imap

    box.connect = connect
    return box


def conversation():
    return {
        "INBOX": {
            1: header("<a@x>", "Thu, 01 Oct 2026 10:00:00 +0300"),
            2: header("<c@x>", "Sat, 03 Oct 2026 09:00:00 +0000", "<b@x>", "<a@x>\r\n <b@x>"),
            3: header("<other@x>", "Sun, 04 Oct 2026 10:00:00 +0300"),
        },
        "Отправленные": {
            7: header("<b@x>", "Fri, 02 Oct 2026 10:00:00 +0300", "<a@x>", "<a@x>"),
        },
        "Other": {9: header("<d@x>", "Mon, 05 Oct 2026 10:00:00 +0300", "<c@x>", "<c@x>")},
    }


def test_parse_message_returns_reply_headers():
    parsed = parse_message(conversation()["INBOX"][2], 2)
    assert parsed["in_reply_to"] == "<b@x>"
    assert parsed["references"] == ["<a@x>", "<b@x>"]
    plain = parse_message(b"Subject: x\r\n\r\n", 1)
    assert (plain["in_reply_to"], plain["references"]) == ("", [])


@pytest.mark.parametrize(("folder", "uid"), [("INBOX", 1), ("Отправленные", 7), ("INBOX", 2)])
def test_thread_is_collected_from_any_message(folder, uid):
    result = mailbox(ThreadIMAP(conversation())).thread(uid, folder=folder)
    assert [(item["folder"], item["uid"]) for item in result] == [
        ("INBOX", 1),
        ("Отправленные", 7),
        ("INBOX", 2),
    ]
    assert result[2]["references"] == ["<a@x>", "<b@x>"]


def test_thread_searches_identifiers_with_one_or_chain_per_folder():
    imap = ThreadIMAP(conversation())
    mailbox(imap).thread(1)
    folder, criteria = imap.searches[0]
    assert folder == "INBOX"
    assert criteria == [
        "OR",
        "OR",
        "HEADER",
        "Message-ID",
        "<a@x>",
        "HEADER",
        "In-Reply-To",
        "<a@x>",
        "HEADER",
        "References",
        "<a@x>",
    ]
    assert {folder for folder, _ in imap.searches} == {"INBOX", "Отправленные"}


def test_thread_of_message_without_identifiers_is_the_message_itself():
    imap = ThreadIMAP({"INBOX": {4: b"Subject: lone\r\n\r\n"}})
    result = mailbox(imap).thread(4)
    assert [item["uid"] for item in result] == [4]
    assert imap.searches == []


def test_thread_stops_at_limit():
    result = mailbox(ThreadIMAP(conversation())).thread(1, limit=2)
    assert len(result) == 2


def test_message_in_two_folders_is_listed_once():
    folders = conversation()
    folders["INBOX"][5] = folders["Отправленные"][7]
    result = mailbox(ThreadIMAP(folders)).thread(1)
    assert [item["message_id"] for item in result] == ["<a@x>", "<b@x>", "<c@x>"]


def test_before_uid_limits_recent_to_older_messages():
    imap = ThreadIMAP({"INBOX": {uid: header(f"<{uid}@x>", "") for uid in range(1, 11)}})
    box = mailbox(imap)
    result = box.recent(limit=3, before_uid=8)
    assert [item["uid"] for item in result] == [7, 6, 5]
    assert imap.searches[-1] == ("INBOX", ["UID", "1:7"])
    result = box.recent(limit=3, unread_only=True, before_uid=6)
    assert [item["uid"] for item in result] == [5, 3, 1]
    assert imap.searches[-1] == ("INBOX", ["UNSEEN", "UID", "1:5"])


def test_before_uid_scopes_search():
    imap = ThreadIMAP({"INBOX": {uid: header(f"<{uid}@x>", "") for uid in range(1, 11)}})
    result = mailbox(imap).search(query="x", before_uid=4)
    assert [item["uid"] for item in result] == [3, 2, 1]
    assert imap.searches == [("INBOX", ["TEXT", "x", "UID", "1:3"])]


def test_before_uid_of_one_returns_nothing_without_connecting():
    box = Mailbox(Settings(email="test@yandex.com", app_password="x"))
    box.connect = None  # Any connection attempt would fail.
    assert box.recent(before_uid=1) == []
    assert box.search(query="x", before_uid=1) == []


@pytest.mark.parametrize("value", [0, -5, True])
def test_invalid_before_uid_is_rejected(value):
    box = Mailbox(Settings(email="test@yandex.com", app_password="x"))
    with pytest.raises(MailValueError, match="before_uid must be a positive integer"):
        box.recent(before_uid=value)
