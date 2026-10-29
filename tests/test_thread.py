import imaplib
from contextlib import contextmanager

import pytest

from yandex_mail_mcp.config import Settings
from yandex_mail_mcp.mail import Mailbox, MailValueError, _base_subject, parse_message

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

    def __init__(self, folders, message_id_error=False):
        self.folders = folders
        self.selected = None
        self.searches = []
        # Like Yandex, which can fail HEADER Message-ID while other headers work.
        self.message_id_error = message_id_error
        self.header_field_fetches = []

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
        if self.message_id_error and "Message-ID" in criteria:
            raise imaplib.IMAP4.error("SEARCH failed: [UNAVAILABLE] UID SEARCH Backend error.")
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
        if token == "SUBJECT":
            value = tokens.pop(0).casefold()
            return lambda uid, raw: any(
                line.lower().startswith("subject:") and value in line.casefold()
                for line in raw.decode().splitlines()
            )
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
        if fields == ["BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"]:
            self.header_field_fetches.append(list(uids))
            return {
                uid: {
                    # Spelled as some servers echo it, with the field name quoted.
                    b'BODY[HEADER.FIELDS ("MESSAGE-ID")]': b"".join(
                        line + b"\r\n"
                        for line in folder[uid].splitlines()
                        if line.lower().startswith(b"message-id:")
                    )
                }
                for uid in uids
                if uid in folder
            }
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


def test_thread_searches_replies_and_parents_separately():
    imap = ThreadIMAP(conversation())
    mailbox(imap).thread(1)
    (folder, replies), (_, parents) = imap.searches[:2]
    assert folder == "INBOX"
    assert replies == [
        "OR",
        "HEADER",
        "In-Reply-To",
        "<a@x>",
        "HEADER",
        "References",
        "<a@x>",
    ]
    assert parents == ["HEADER", "Message-ID", "<a@x>"]
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


def test_ids_that_cannot_be_sent_as_atoms_are_not_searched():
    folders = conversation()
    folders["INBOX"][2] = header(
        "<c@x>", "Sat, 03 Oct 2026 09:00:00 +0000", "<b@x>", "<a@x> <b%c(d)@y> <тест@x> <b@x>"
    )
    imap = ThreadIMAP(folders)
    result = mailbox(imap).thread(2)
    assert [item["message_id"] for item in result] == ["<a@x>", "<b@x>", "<c@x>"]
    tokens = [token for _, criteria in imap.searches for token in criteria]
    assert "<b%c(d)@y>" not in tokens
    assert "<тест@x>" not in tokens


def test_lower_case_sent_flag_is_searched():
    imap = ThreadIMAP(conversation())
    imap.list_folders = lambda: [
        ((b"\\HasNoChildren",), b"|", "INBOX"),
        ((b"\\sent",), b"|", "Отправленные"),
    ]
    result = mailbox(imap).thread(1)
    assert ("Отправленные", 7) in [(item["folder"], item["uid"]) for item in result]


def test_thread_fetches_at_most_limit_messages():
    root = header("<root@x>", "Thu, 01 Oct 2026 10:00:00 +0300")
    replies = {
        uid: header(f"<{uid}@x>", "Fri, 02 Oct 2026 10:00:00 +0300", "<root@x>", "<root@x>")
        for uid in range(2, 802)
    }
    imap = ThreadIMAP({"INBOX": {1: root, **replies}})
    fetched = []
    fetch = imap.fetch

    def recording_fetch(uids, fields):
        if fields == ["BODY.PEEK[HEADER]", "BODYSTRUCTURE"]:
            fetched.append(len(uids))
        return fetch(uids, fields)

    imap.fetch = recording_fetch
    assert len(mailbox(imap).thread(1, limit=5)) == 5
    assert fetched == [4]


@pytest.mark.parametrize(("folder", "uid"), [("INBOX", 1), ("Отправленные", 7), ("INBOX", 2)])
def test_thread_survives_yandex_message_id_backend_error(folder, uid):
    imap = ThreadIMAP(conversation(), message_id_error=True)
    result = mailbox(imap).thread(uid, folder=folder)
    assert [(item["folder"], item["uid"]) for item in result] == [
        ("INBOX", 1),
        ("Отправленные", 7),
        ("INBOX", 2),
    ]
    # Message-ID is tried once per call; then parents are found by subject.
    assert sum("Message-ID" in criteria for _, criteria in imap.searches) == 1
    assert ["SUBJECT", "Договор"] in [criteria for _, criteria in imap.searches]


def test_subject_fallback_matches_message_ids_not_subjects():
    imap = ThreadIMAP(conversation(), message_id_error=True)
    result = mailbox(imap).thread(2)
    # <other@x> has the same subject but is not part of the thread.
    assert "<other@x>" not in [item["message_id"] for item in result]


def test_subject_fallback_reads_at_most_200_candidates():
    folders = {"INBOX": {uid: header(f"<{uid}@x>", "") for uid in range(1, 301)}}
    folders |= {"Отправленные": {}, "Other": {}}
    folders["INBOX"][301] = header("<child@x>", "", "<parent@x>", "<parent@x>")
    imap = ThreadIMAP(folders, message_id_error=True)
    mailbox(imap).thread(301)
    assert imap.header_field_fetches
    assert all(len(batch) <= 200 for batch in imap.header_field_fetches)
    assert max(max(batch) for batch in imap.header_field_fetches) == 301


@pytest.mark.parametrize(
    ("subject", "base"),
    [
        ("Re: Договор", "Договор"),
        ("RE: Fwd: re[2]: Счёт  за\r\n октябрь", "Счёт за октябрь"),
        ("Ответ: Отв: Встреча", "Встреча"),
        ("Договор", "Договор"),
        ("", ""),
    ],
)
def test_base_subject(subject, base):
    assert _base_subject(subject) == base
