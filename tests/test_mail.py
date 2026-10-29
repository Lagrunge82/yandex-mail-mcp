import base64
import imaplib
import quopri
from contextlib import contextmanager
from email.message import EmailMessage

import pytest

from yandex_mail_mcp.config import Settings
from yandex_mail_mcp.mail import (
    Mailbox,
    MailError,
    MailLookupError,
    MailValueError,
    parse_message,
    valid_uid,
)


def test_parse_message_decodes_unicode_and_does_not_include_attachment():
    msg = EmailMessage()
    msg["From"] = "Test Sender <test@example.com>"
    msg["To"] = "me@example.com"
    msg["Subject"] = "Важный документ"
    msg.set_content("Здравствуйте! Оплатите счёт до пятницы.")
    msg.add_attachment(
        b"SECRET-ATTACHMENT", maintype="application", subtype="octet-stream", filename="test.bin"
    )
    parsed = parse_message(msg.as_bytes(), 123)
    assert parsed["subject"] == "Важный документ"
    assert "Оплатите" in parsed["preview"]
    assert "SECRET-ATTACHMENT" not in parsed["preview"]
    assert parsed["has_attachments"] is True
    assert parsed["uid"] == 123


def test_html_ignores_script_content():
    msg = EmailMessage()
    msg["Subject"] = "HTML"
    msg.set_content(
        "<html><script>do something bad</script><body>Hello world</body></html>", subtype="html"
    )
    parsed = parse_message(msg.as_bytes(), 1)
    assert "Hello world" in parsed["preview"]
    assert "something bad" not in parsed["preview"]


def test_inline_attachment_is_detected_and_excluded_from_preview():
    msg = EmailMessage()
    msg.set_content("Actual body")
    msg.add_attachment(
        "PRIVATE ATTACHMENT", subtype="plain", filename="document.txt", disposition="inline"
    )
    parsed = parse_message(msg.as_bytes(), 1)
    assert parsed["has_attachments"] is True
    assert parsed["preview"] == "Actual body"


def test_forwarded_message_is_not_in_preview():
    outer = EmailMessage()
    outer.set_content("Outer body")
    inner = EmailMessage()
    inner.set_content("INNER SECRET BODY")
    outer.add_attachment(inner, filename="forwarded.eml")
    result = parse_message(outer.as_bytes(), 1)
    assert result["has_attachments"] is True
    assert result["preview"] == "Outer body"


def test_inline_image_without_filename_counts_as_attachment_in_both_paths():
    from yandex_mail_mcp.mail import _text_sections

    msg = EmailMessage()
    msg.set_content("Body")
    msg.add_attachment(b"png", maintype="image", subtype="png", disposition="inline", cid="<x>")
    assert parse_message(msg.as_bytes(), 1)["has_attachments"] is True
    structure = (
        [
            (b"TEXT", b"PLAIN", None, None, None, b"7BIT", 10, 1, None, None),
            (b"IMAGE", b"PNG", None, b"<x>", None, b"BASE64", 4, None, (b"INLINE", None)),
        ],
        b"RELATED",
    )
    assert _text_sections(structure)[1] is True


def test_root_message_with_name_or_attachment_disposition_is_body_in_both_paths():
    from yandex_mail_mcp.mail import _text_sections

    raw = (
        b"Subject: Report\r\n"
        b'Content-Type: text/plain; charset="utf-8"; name="body.txt"\r\n'
        b'Content-Disposition: attachment; filename="body.txt"\r\n'
        b"\r\n"
        b"Daily report body\r\n"
    )
    parsed = parse_message(raw, 1)
    assert parsed["has_attachments"] is False
    assert parsed["preview"] == "Daily report body"
    structure = (
        b"TEXT",
        b"PLAIN",
        (b"CHARSET", b"utf-8", b"NAME", b"body.txt"),
        None,
        None,
        b"7BIT",
        19,
        1,
        None,
        (b"ATTACHMENT", (b"FILENAME", b"body.txt")),
        None,
    )
    assert _text_sections(structure) == ([("1", "text/plain", "utf-8", "7BIT")], False)


def test_root_message_of_non_text_type_is_still_an_attachment():
    from yandex_mail_mcp.mail import _text_sections

    raw = b"Subject: Scan\r\nContent-Type: application/pdf\r\n\r\n%PDF-1.7\r\n"
    assert parse_message(raw, 1)["has_attachments"] is True
    structure = (b"APPLICATION", b"PDF", None, None, None, b"BASE64", 10, None, None, None)
    assert _text_sections(structure) == ([], True)


def test_html_blocks_and_entities_are_separated():
    msg = EmailMessage()
    msg.set_content(
        "<table><tr><td>Итого:</td><td>100 &amp; 20</td></tr></table><p>A<br>B</p>", subtype="html"
    )
    assert parse_message(msg.as_bytes(), 1)["preview"] == "Итого: 100 & 20 A B"


def test_date_uses_iso_format_and_preserves_invalid_input():
    raw = b"Date: Tue, 28 Oct 2026 10:00:00 +0300\r\n\r\n"
    assert parse_message(raw, 1)["date"] == "2026-10-28T10:00:00+03:00"
    assert parse_message(b"Date: nonsense\r\n\r\n", 2)["date"] == "nonsense"


def test_invalid_uid_rejected():
    for uid in (0, -1, True, "1"):
        with pytest.raises(ValueError):
            valid_uid(uid)


def test_no_mutation_without_explicit_opt_in():
    box = Mailbox(Settings(email="test@yandex.com", app_password="not-a-real-password"))
    with pytest.raises(PermissionError, match="disabled"):
        box.move_to_promotions(3, confirm=True)


def test_no_mutation_without_confirmation():
    box = Mailbox(
        Settings(email="test@yandex.com", app_password="not-a-real-password", allow_write=True)
    )
    with pytest.raises(PermissionError, match="confirm"):
        box.move_to_promotions(3)


def test_system_folder_flag_is_rejected():
    box = Mailbox(
        Settings(
            email="test@yandex.com", app_password="x", allow_write=True, promotions_folder="Archive"
        )
    )

    class IMAP:
        def list_folders(self):
            return [((b"\\Trash",), b"/", "Archive")]

    @contextmanager
    def connect():
        yield IMAP()

    box.connect = connect
    with pytest.raises(ValueError, match="safe"):
        box.move_to_promotions(3, confirm=True)


def test_promotions_folder_with_whitespace_uses_existing_folder():
    box = Mailbox(
        Settings(
            email="test@yandex.com",
            app_password="x",
            allow_write=True,
            promotions_folder=" AI-Promotions ",
        )
    )
    calls = []

    class IMAP:
        def list_folders(self):
            return [((b"\\HasNoChildren",), b"|", "AI-Promotions")]

        def has_capability(self, name):
            return True

        def create_folder(self, name):
            calls.append(("create", name))

        def select_folder(self, name, readonly):
            pass

        def search(self, criteria):
            return [3]

        def move(self, uids, folder):
            calls.append(("move", folder))

    @contextmanager
    def connect():
        yield IMAP()

    box.connect = connect
    assert box.move_to_promotions(3, confirm=True)["moved_to"] == "AI-Promotions"
    assert calls == [("move", "AI-Promotions")]


@pytest.mark.parametrize("name", ["Черновики", "Удалённые", "Sent", "Spam"])
def test_system_folder_name_is_rejected(name):
    box = Mailbox(
        Settings(
            email="test@yandex.com", app_password="x", allow_write=True, promotions_folder=name
        )
    )
    with pytest.raises(ValueError, match="safe"):
        box.move_to_promotions(3, confirm=True)


@pytest.mark.parametrize(
    ("name", "delimiter"),
    [("Trash/Promo", b"/"), ("Spam|x", b"|"), ("Удалённые|Промо", "|")],
)
def test_nested_promotions_folder_is_rejected(name, delimiter):
    box = Mailbox(
        Settings(
            email="test@yandex.com", app_password="x", allow_write=True, promotions_folder=name
        )
    )

    class IMAP:
        def list_folders(self):
            return [((b"\\HasNoChildren",), delimiter, "INBOX"), ((), None, "Shared")]

    @contextmanager
    def connect():
        yield IMAP()

    box.connect = connect
    with pytest.raises(MailValueError, match="safe"):
        box.move_to_promotions(3, confirm=True)


def test_preview_fetches_only_bounded_text_section():
    calls = []

    class IMAP:
        def fetch(self, uids, fields):
            calls.append(fields)
            if fields == ["BODY.PEEK[HEADER]", "BODYSTRUCTURE"]:
                structure = (
                    [
                        (
                            b"TEXT",
                            b"PLAIN",
                            (b"CHARSET", b"UTF-8"),
                            None,
                            None,
                            b"BASE64",
                            12,
                            1,
                            None,
                            None,
                        ),
                        (
                            b"APPLICATION",
                            b"PDF",
                            (b"NAME", b"large.pdf"),
                            None,
                            None,
                            b"BASE64",
                            5000000,
                            None,
                            (b"ATTACHMENT", (b"FILENAME", b"large.pdf")),
                        ),
                    ],
                    b"MIXED",
                )
                return {
                    1: {
                        b"BODY[HEADER]": b"Subject: Hello\r\nFrom: a@example.com\r\n\r\n",
                        b"BODYSTRUCTURE": structure,
                    }
                }
            return {1: {b"BODY[1]<0>": b"SGVsbG8gd29ybGQ="}}

    result = Mailbox._fetch(IMAP(), [1])
    assert result[0]["preview"] == "Hello world"
    assert result[0]["has_attachments"]
    assert calls == [["BODY.PEEK[HEADER]", "BODYSTRUCTURE"], ["BODY.PEEK[1]<0.16384>"]]


def test_get_email_rejects_large_message_before_body_fetch():
    box = Mailbox(Settings(email="test@yandex.com", app_password="x"))
    calls = []

    class IMAP:
        def select_folder(self, *args, **kwargs):
            pass

        def fetch(self, uids, fields):
            calls.append(fields)
            return {1: {b"RFC822.SIZE": 21 * 1024 * 1024}}

    @contextmanager
    def connect():
        yield IMAP()

    box.connect = connect
    with pytest.raises(ValueError, match="20 MiB"):
        box.message(1)
    assert calls == [["RFC822.SIZE"]]


def test_search_uses_server_results_without_300_message_cap(monkeypatch):
    box = Mailbox(Settings(email="test@yandex.com", app_password="x"))

    class IMAP:
        def select_folder(self, *args, **kwargs):
            pass

        def search(self, criteria, charset=None):
            assert criteria == ["TEXT", "счёт"]
            assert charset == "UTF-8"
            return [1, 500]

    @contextmanager
    def connect():
        yield IMAP()

    box.connect = connect
    monkeypatch.setattr(
        Mailbox, "_fetch", staticmethod(lambda imap, uids, **kwargs: [{"uid": uid} for uid in uids])
    )
    assert box.search("счёт", limit=1) == [{"uid": 500}]


def test_fetch_batches_sections_and_handles_incomplete_base64_and_missing_structure():
    data = base64.b64encode("Привет мир!".encode())[:-3]
    assert len(data) % 4 == 1
    calls = []
    text_part = (
        b"TEXT",
        b"PLAIN",
        (b"CHARSET", b"UTF-8"),
        None,
        None,
        b"BASE64",
        40,
        1,
        None,
        None,
    )

    class IMAP:
        def fetch(self, uids, fields):
            calls.append((uids, fields))
            if fields == ["BODY.PEEK[HEADER]", "BODYSTRUCTURE"]:
                return {
                    1: {b"BODY[HEADER]": b"Subject: one\r\n\r\n", b"BODYSTRUCTURE": text_part},
                    2: {b"BODY[HEADER]": b"Subject: two\r\n\r\n", b"BODYSTRUCTURE": text_part},
                    3: {b"BODY[HEADER]": b"Subject: bad\r\n\r\n"},
                }
            return {1: {b"BODY[1]<0>": data}, 2: {b"BODY[1]<0>": base64.b64encode(b"second")}}

    result = Mailbox._fetch(IMAP(), [1, 2, 3])
    assert [item["uid"] for item in result] == [1, 2]
    assert result[0]["preview"].startswith("Привет")
    assert result[1]["preview"] == "second"
    assert calls[1] == ([1, 2], ["BODY.PEEK[1]<0.16384>"])


def test_search_does_not_fallback_on_aborted_connection():
    box = Mailbox(Settings(email="test@yandex.com", app_password="x"))

    class IMAP:
        def select_folder(self, *args, **kwargs):
            pass

        def search(self, *args, **kwargs):
            raise imaplib.IMAP4.abort("disconnected")

    @contextmanager
    def connect():
        yield IMAP()

    box.connect = connect
    with pytest.raises(imaplib.IMAP4.abort, match="disconnected"):
        box.search("example")


def test_headers_does_not_fetch_full_message():
    box = Mailbox(Settings(email="test@yandex.com", app_password="x"))

    class IMAP:
        def select_folder(self, *args, **kwargs):
            pass

        def fetch(self, uids, fields):
            assert fields == ["BODY.PEEK[HEADER]"]
            return {1: {b"BODY[HEADER]": b"Subject: Large\r\nFrom: a@example.com\r\n\r\n"}}

    @contextmanager
    def connect():
        yield IMAP()

    box.connect = connect
    assert box.headers(1)["subject"] == "Large"


def test_preview_validates_folder_and_reads_headers_in_one_connection():
    box = Mailbox(
        Settings(
            email="test@yandex.com",
            app_password="x",
            allow_write=True,
            promotions_folder="ai-promotions",
        )
    )
    connections = []

    class IMAP:
        def list_folders(self):
            return [((b"\\HasNoChildren",), b"|", "AI-Promotions")]

        def has_capability(self, name):
            return True

        def select_folder(self, name, readonly):
            assert (name, readonly) == ("INBOX", True)

        def fetch(self, uids, fields):
            return {3: {b"BODY[HEADER]": b"Subject: Sale\r\nFrom: shop@example.com\r\n\r\n"}}

    @contextmanager
    def connect():
        connections.append(1)
        yield IMAP()

    box.connect = connect
    headers, folder = box.preview_promotions_move(3)
    assert headers["subject"] == "Sale"
    assert folder == "AI-Promotions"
    assert connections == [1]


def test_preview_rejects_server_without_move():
    box = Mailbox(Settings(email="test@yandex.com", app_password="x", allow_write=True))

    class IMAP:
        def list_folders(self):
            return []

        def has_capability(self, name):
            return False

    @contextmanager
    def connect():
        yield IMAP()

    box.connect = connect
    with pytest.raises(MailError, match="MOVE"):
        box.preview_promotions_move(3)


class MoveIMAP:
    """Records the IMAP calls move_to_promotions makes."""

    def __init__(self, folders=(), move=True, inbox=(3,)):
        self.calls = []
        self._folders = list(folders)
        self._move = move
        self._inbox = list(inbox)

    def list_folders(self):
        self.calls.append(("list_folders",))
        return self._folders

    def has_capability(self, name):
        self.calls.append(("has_capability", name))
        return self._move

    def create_folder(self, name):
        self.calls.append(("create_folder", name))

    def select_folder(self, name, readonly):
        self.calls.append(("select_folder", name, readonly))

    def search(self, criteria):
        self.calls.append(("search", criteria))
        return self._inbox

    def move(self, uids, folder):
        self.calls.append(("move", uids, folder))


def writable_box(imap):
    box = Mailbox(Settings(email="test@yandex.com", app_password="x", allow_write=True))

    @contextmanager
    def connect():
        yield imap

    box.connect = connect
    return box


def test_move_creates_missing_folder_then_moves():
    imap = MoveIMAP(folders=[((b"\\HasNoChildren",), b"|", "INBOX")])
    result = writable_box(imap).move_to_promotions(3, confirm=True)
    assert result == {"uid": 3, "moved_to": "AI-Promotions", "deleted_permanently": False}
    assert imap.calls == [
        ("list_folders",),
        ("has_capability", "MOVE"),
        ("create_folder", "AI-Promotions"),
        ("select_folder", "INBOX", False),
        ("search", ["UID", "3"]),
        ("move", [3], "AI-Promotions"),
    ]


def test_move_requires_move_capability():
    imap = MoveIMAP(move=False)
    with pytest.raises(MailError, match="MOVE"):
        writable_box(imap).move_to_promotions(3, confirm=True)
    assert [call[0] for call in imap.calls] == ["list_folders", "has_capability"]


def test_move_of_uid_missing_from_inbox_does_not_move():
    imap = MoveIMAP(folders=[((b"\\HasNoChildren",), b"|", "AI-Promotions")], inbox=[])
    with pytest.raises(MailLookupError, match="UID 3 not found in INBOX"):
        writable_box(imap).move_to_promotions(3, confirm=True)
    assert not any(call[0] in {"move", "create_folder"} for call in imap.calls)


def single_part_imap(subtype, charset, encoding, body):
    structure = (b"TEXT", subtype, (b"CHARSET", charset), None, None, encoding, len(body), 1)
    structure += (None, None)

    class IMAP:
        def fetch(self, uids, fields):
            if fields == ["BODY.PEEK[HEADER]", "BODYSTRUCTURE"]:
                header = b"Subject: Test\r\nFrom: a@example.com\r\n\r\n"
                return {1: {b"BODY[HEADER]": header, b"BODYSTRUCTURE": structure}}
            assert fields == ["BODY.PEEK[1]<0.16384>"]
            return {1: {b"BODY[1]<0>": body}}

    return IMAP()


def test_fetch_decodes_quoted_printable_cyrillic():
    body = "Оплатите счёт".encode()
    imap = single_part_imap(b"PLAIN", b"UTF-8", b"QUOTED-PRINTABLE", quopri.encodestring(body))
    assert Mailbox._fetch(imap, [1])[0]["preview"] == "Оплатите счёт"


def test_fetch_falls_back_to_utf8_for_unknown_charset():
    imap = single_part_imap(b"PLAIN", b"x-unknown", b"8BIT", "Привет".encode())
    assert Mailbox._fetch(imap, [1])[0]["preview"] == "Привет"


def test_fetch_extracts_text_from_html_section():
    html = b"<html><style>p {}</style><body><p>Hello</p><p>world</p></body></html>"
    imap = single_part_imap(b"HTML", b"UTF-8", b"7BIT", html)
    assert Mailbox._fetch(imap, [1])[0]["preview"] == "Hello world"


def test_search_falls_back_to_previews_when_server_search_fails():
    box = Mailbox(Settings(email="test@yandex.com", app_password="x"))
    fetched = []

    class IMAP:
        def select_folder(self, *args, **kwargs):
            pass

        def search(self, criteria, charset=None):
            if criteria != ["ALL"]:
                raise imaplib.IMAP4.error("SEARCH charset not supported")
            return list(range(1, 401))

    @contextmanager
    def connect():
        yield IMAP()

    def fetch(imap, uids, max_body_chars):
        fetched.extend(uids)
        return [
            {"uid": uid, "from": "", "subject": "Счёт" if uid % 100 == 0 else "", "preview": ""}
            for uid in uids
        ]

    box.connect = connect
    box._fetch = fetch
    found = box.search("счёт", limit=2)
    assert [message["uid"] for message in found] == [400, 300]
    assert max(fetched) == 400
    assert min(fetched) > 100  # Only the 300 newest messages are scanned.
