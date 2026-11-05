"""The newest UIDs of a folder are read in windows, never by UID SEARCH ALL (#119)."""

import imaplib
from contextlib import contextmanager
from itertools import islice

import pytest

from yandex_mail_mcp import mail
from yandex_mail_mcp.config import Settings
from yandex_mail_mcp.mail import Mailbox


def _in(number, sequence, top):
    """Whether number is in an IMAP sequence set such as "3", "1:5" or "7:*"."""
    for part in str(sequence).split(","):
        first, _, last = part.partition(":")
        start = top if first == "*" else int(first)
        end = start if not last else (top if last == "*" else int(last))
        if min(start, end) <= number <= max(start, end):
            return True
    return False


# A message that is one PDF: an attachment, per _text_sections.
PDF = (b"APPLICATION", b"PDF", None, None, None, b"BASE64", 10, None, None, None)


class Folder:
    """A selected folder: UIDs ascending with sequence numbers, some unread."""

    def __init__(self, uids, unread=(), subjects=None, report_exists=True, report_uidnext=True):
        self.uids = sorted(uids)
        self.unread = set(unread)
        self.subjects = subjects or {}
        self.report_exists = report_exists
        self.report_uidnext = report_uidnext
        self.use_uid = True
        self.searches = []
        self.fetches = []

    def select_folder(self, name, readonly=False):
        if not self.report_exists:
            return {b"UIDVALIDITY": 1}
        selected = {b"EXISTS": len(self.uids), b"UIDVALIDITY": 1}
        if self.report_uidnext:
            selected[b"UIDNEXT"] = (self.uids[-1] if self.uids else 0) + 1
        return selected

    def _matches(self, uid, criteria):
        items = list(criteria)
        while items:
            key = items.pop(0)
            if key == "ALL":
                continue
            if key == "UNSEEN" and uid not in self.unread:
                return False
            if key == "SEEN" and uid in self.unread:
                return False
            if key == "UID":
                top = self.uids[-1] if self.uids else 0
                if not _in(uid, items.pop(0), top):
                    return False
            if key == "TEXT" and items.pop(0) not in self.subjects.get(uid, ""):
                return False
        return True

    def search(self, criteria, charset=None):
        self.searches.append(list(criteria))
        return [uid for uid in self.uids if self._matches(uid, criteria)]

    def fetch(self, messages, fields):
        if isinstance(messages, list):
            messages = ",".join(map(str, messages))
        self.fetches.append((self.use_uid, messages, list(fields)))
        if fields == ["BODYSTRUCTURE"]:
            return {int(uid): {b"BODYSTRUCTURE": PDF} for uid in messages.split(",")}
        if fields != ["UID"]:
            return {int(uid): {} for uid in messages.split(",")}
        if self.use_uid:
            # Like IMAPClient: keyed by UID, which it drops from the item, with SEQ kept.
            top = self.uids[-1] if self.uids else 0
            return {
                uid: {b"SEQ": seq}
                for seq, uid in enumerate(self.uids, start=1)
                if _in(uid, messages, top)
            }
        return {
            seq: {b"UID": uid, b"SEQ": seq}
            for seq, uid in enumerate(self.uids, start=1)
            if _in(seq, messages, len(self.uids))
        }


def mailbox(folder, monkeypatch):
    box = Mailbox(Settings(email="test@yandex.com", oauth_token="x"))

    @contextmanager
    def connect(*_args, **_kwargs):
        yield folder

    box.connect = connect
    monkeypatch.setattr(
        Mailbox, "_fetch", staticmethod(lambda imap, uids, **kwargs: [{"uid": uid} for uid in uids])
    )
    return box


def reference(folder, limit, before_uid=None, criteria=()):
    """What the former UID SEARCH over the whole folder returned."""
    return [
        uid
        for uid in reversed(folder.uids)
        if (before_uid is None or uid < before_uid) and folder._matches(uid, criteria)
    ][:limit]


def uids(result):
    return [item["uid"] for item in result]


# UIDs with gaps, as in a folder mail was moved out of.
SPARSE = [*range(1, 40), *range(5000, 5600, 3), *range(90000, 90050), 1_000_000]


def test_recent_reads_the_last_uids_by_sequence_number_without_search(monkeypatch):
    folder = Folder(SPARSE)
    box = mailbox(folder, monkeypatch)
    assert uids(box.recent(limit=5)) == reference(folder, 5)
    assert folder.searches == []
    exists = len(SPARSE)
    assert folder.fetches == [(False, f"{exists - 4}:{exists}", ["UID"])]
    assert folder.use_uid is True


@pytest.mark.parametrize("count", [0, 1, 3])
def test_recent_handles_an_empty_or_small_folder(monkeypatch, count):
    folder = Folder(range(10, 10 + count))
    box = mailbox(folder, monkeypatch)
    assert uids(box.recent(limit=20)) == reference(folder, 20)
    assert folder.searches == []


@pytest.mark.parametrize(
    "before_uid", [2, 3, 39, 40, 41, 4999, 5001, 5300, 90000, 90049, 999_999, 1_000_001, 10**9]
)
@pytest.mark.parametrize("limit", [1, 5, 100])
@pytest.mark.parametrize("report_uidnext", [True, False])
def test_before_uid_matches_the_former_search(monkeypatch, before_uid, limit, report_uidnext):
    folder = Folder(SPARSE, report_uidnext=report_uidnext)
    box = mailbox(folder, monkeypatch)
    assert uids(box.recent(limit=limit, before_uid=before_uid)) == reference(
        folder, limit, before_uid
    )
    assert folder.searches == []
    assert folder.use_uid is True


def test_before_uid_pages_through_the_whole_folder(monkeypatch):
    folder = Folder(SPARSE)
    box = mailbox(folder, monkeypatch)
    seen, before = [], None
    while page := uids(box.recent(limit=7, before_uid=before)):
        seen += page
        before = page[-1]
    assert seen == sorted(SPARSE, reverse=True)


# Dense old mail, a wide gap, then a few new messages.
GAPPED = [*range(1, 20_001), *range(900_000, 900_005)]


def fetched_sizes(folder):
    """How many messages each FETCH (UID) asked for."""
    top = len(folder.uids) if folder.uids else 0
    sizes = []
    for use_uid, messages, _ in folder.fetches:
        numbers = folder.uids if use_uid else range(1, top + 1)
        sizes.append(sum(_in(number, messages, numbers[-1]) for number in numbers))
    return sizes


def test_before_uid_of_a_message_is_placed_by_one_uid_fetch(monkeypatch):
    folder = Folder(GAPPED)
    box = mailbox(folder, monkeypatch)
    assert uids(box.recent(limit=5, before_uid=900_000)) == [20_000, 19_999, 19_998, 19_997, 19_996]
    # Its sequence number, then the five messages below it: no UID range over the gap.
    assert folder.fetches == [(True, "900000", ["UID"]), (False, "19996:20000", ["UID"])]


@pytest.mark.parametrize("report_uidnext", [True, False])
@pytest.mark.parametrize("before_uid", [500_000, 10**9])
def test_before_uid_no_message_has_is_placed_by_binary_search(
    monkeypatch, report_uidnext, before_uid
):
    folder = Folder(GAPPED, report_uidnext=report_uidnext)
    box = mailbox(folder, monkeypatch)
    assert uids(box.recent(limit=5, before_uid=before_uid)) == reference(folder, 5, before_uid)
    # Every FETCH names at most `limit` messages, however wide the gap.
    assert max(fetched_sizes(folder)) <= 5
    assert len(folder.fetches) <= 18
    if report_uidnext and before_uid > GAPPED[-1]:
        assert folder.fetches == [(False, "20001:20005", ["UID"])]


@pytest.mark.parametrize("before_uid", [None, 5300, 90010])
def test_unread_only_searches_unseen_within_widening_windows(monkeypatch, before_uid):
    unread = {2, 17, 5003, 5450, 90001}
    folder = Folder(SPARSE, unread=unread)
    box = mailbox(folder, monkeypatch)
    result = uids(box.recent(limit=10, unread_only=True, before_uid=before_uid))
    assert result == reference(folder, 10, before_uid, ["UNSEEN"])
    assert set(result) <= unread
    assert folder.searches
    for criteria in folder.searches:
        assert criteria[0] == "UID" and "UNSEEN" in criteria and "ALL" not in criteria


def test_unread_only_stops_once_the_limit_is_met(monkeypatch):
    folder = Folder(range(1, 50_001), unread=range(49_990, 50_001))
    box = mailbox(folder, monkeypatch)
    assert uids(box.recent(limit=5, unread_only=True)) == [50_000, 49_999, 49_998, 49_997, 49_996]
    # The first window's lowest UID by sequence number, then one SEARCH up to the end.
    assert folder.searches == [["UID", "49801:*", "UNSEEN"]]
    assert folder.fetches == [(False, "49801", ["UID"])]


def test_window_at_the_folder_start_needs_no_fetch(monkeypatch):
    folder = Folder(range(1, 101), unread={50})
    box = mailbox(folder, monkeypatch)
    assert uids(box.recent(limit=5, unread_only=True)) == [50]
    assert folder.fetches == []
    assert folder.searches == [["UID", "1:*", "UNSEEN"]]


def test_unread_only_with_no_unread_mail_reaches_the_folder_start(monkeypatch):
    folder = Folder(range(1, 50_001))
    box = mailbox(folder, monkeypatch)
    assert box.recent(limit=5, unread_only=True) == []
    spans = [criteria[1] for criteria in folder.searches]
    assert spans[0] == "49801:*"
    assert spans[-1].startswith("1:")
    assert len(spans) <= 6


def test_without_exists_the_folder_is_searched_at_once(monkeypatch):
    folder = Folder(SPARSE, report_exists=False)
    box = mailbox(folder, monkeypatch)
    assert uids(box.recent(limit=5, before_uid=5300)) == reference(folder, 5, 5300)
    assert folder.searches == [["UID", "1:5299"]]


def test_search_without_dates_is_windowed(monkeypatch):
    folder = Folder(SPARSE, unread={3, 90002})
    box = mailbox(folder, monkeypatch)
    assert uids(box.search(unread=True)) == [90002, 3]
    assert all(criteria[0] == "UID" for criteria in folder.searches)


def test_search_with_dates_is_sent_whole(monkeypatch):
    folder = Folder(SPARSE)
    box = mailbox(folder, monkeypatch)
    box.search(since="2026-01-01", limit=3)
    assert len(folder.searches) == 1
    assert "UID" not in folder.searches[0]


def test_has_attachments_alone_takes_the_last_candidates_without_search(monkeypatch):
    folder = Folder(range(1, 2001))
    box = mailbox(folder, monkeypatch)
    candidates = []
    monkeypatch.setattr(
        Mailbox,
        "_filter_attachments",
        lambda self, imap, uids, has_attachments, limit: (
            candidates.append(list(islice(uids, 500))) or []
        ),
    )
    box.search(has_attachments=True)
    assert folder.searches == []
    assert candidates == [list(range(2000, 1500, -1))]
    assert folder.fetches == [(False, "1501:2000", ["UID"])]


def test_has_attachments_with_criteria_stops_searching_once_the_limit_matches(monkeypatch):
    folder = Folder(range(1, 50_001), unread=range(1, 50_001))
    box = mailbox(folder, monkeypatch)
    assert uids(box.search(unread=True, has_attachments=True, limit=5)) == list(
        range(50_000, 49_995, -1)
    )
    # One window answers it; the 500 candidates are never searched for.
    assert folder.searches == [["UID", "49801:*", "UNSEEN"]]


def test_has_attachments_checks_at_most_500_candidates(monkeypatch):
    folder = Folder(range(1, 50_001), unread=range(1, 50_001))
    box = mailbox(folder, monkeypatch)
    checked = []
    real = Mailbox._filter_attachments

    def spy(self, imap, uids, has_attachments, limit):
        def counted():
            for uid in uids:
                checked.append(uid)
                yield uid

        return real(self, imap, counted(), has_attachments, limit)

    monkeypatch.setattr(Mailbox, "_filter_attachments", spy)
    assert box.search(unread=True, has_attachments=False, limit=5) == []
    assert checked == list(range(50_000, 49_500, -1))
    assert len(folder.searches) == 2


def test_rejected_query_falls_back_to_the_last_300_without_search(monkeypatch):
    folder = Folder(range(1, 1001))
    box = mailbox(folder, monkeypatch)

    def search(criteria, charset=None):
        folder.searches.append(list(criteria))
        raise imaplib.IMAP4.error("SEARCH backend rejected the query")

    folder.search = search
    seen = []
    monkeypatch.setattr(
        Mailbox,
        "_fetch",
        staticmethod(lambda imap, uids, **kwargs: seen.extend(uids) or []),
    )
    assert box.search("invoice") == []
    assert seen == list(range(1000, 700, -1))
    assert len(folder.searches) == 1


def test_rejected_fetch_does_not_fall_back_to_previews(monkeypatch):
    folder = Folder(range(1, 1001))
    box = mailbox(folder, monkeypatch)

    def fetch(messages, fields):
        raise imaplib.IMAP4.error("FETCH command error: BAD")

    folder.fetch = fetch
    with pytest.raises(imaplib.IMAP4.error, match="FETCH") as raised:
        box.search("invoice")
    assert not isinstance(raised.value, mail.SearchRejected)
    assert folder.searches == []


def test_rejected_search_is_still_an_imap_error(monkeypatch):
    folder = Folder(range(1, 1001))
    box = mailbox(folder, monkeypatch)

    def search(criteria, charset=None):
        raise imaplib.IMAP4.error("SEARCH backend error")

    folder.search = search
    with pytest.raises(imaplib.IMAP4.error, match="backend error"):
        box.recent(limit=5, unread_only=True)


def test_sequence_fetch_restores_use_uid_after_an_error(monkeypatch):
    folder = Folder(SPARSE)

    def fetch(messages, fields):
        raise imaplib.IMAP4.abort("socket error: EOF")

    folder.fetch = fetch
    with pytest.raises(imaplib.IMAP4.abort):
        mail.newest_uids(folder, folder.select_folder("INBOX"), 5)
    assert folder.use_uid is True
