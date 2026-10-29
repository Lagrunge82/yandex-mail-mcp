from email.message import EmailMessage

from yandex_mail_mcp.mail import Mailbox, _text_sections, parse_message

CYRILLIC_NAME = "Счёт №5.pdf"
RFC2231_NAME = "UTF-8''%D0%A1%D1%87%D1%91%D1%82%20%E2%84%965.pdf"


def message_with_pdf_and_inline_image():
    msg = EmailMessage()
    msg["Subject"] = "Договор"
    msg.set_content("См. вложение")
    msg.add_attachment(
        b"%PDF-1.7 contract", maintype="application", subtype="pdf", filename="Договор.pdf"
    )
    msg.add_attachment(
        b"\x89PNG",
        maintype="image",
        subtype="png",
        filename="logo.png",
        disposition="inline",
        cid="<logo>",
    )
    return msg


def test_parse_message_lists_attachments_without_content():
    msg = message_with_pdf_and_inline_image()
    parsed = parse_message(msg.as_bytes(), 1)
    pdf, png = (part.get_payload() for part in list(msg.iter_parts())[1:])
    assert parsed["attachments"] == [
        {
            "filename": "Договор.pdf",
            "content_type": "application/pdf",
            "size": len(pdf),
            "section": "2",
        },
        {"filename": "logo.png", "content_type": "image/png", "size": len(png), "section": "3"},
    ]
    assert parsed["has_attachments"] is True
    assert "contract" not in str(parsed)


def test_bodystructure_lists_the_same_attachments():
    structure = (
        [
            (b"TEXT", b"PLAIN", (b"CHARSET", b"utf-8"), None, None, b"8BIT", 22, 1, None, None),
            (
                b"APPLICATION",
                b"PDF",
                None,
                None,
                None,
                b"BASE64",
                24,
                None,
                (
                    b"ATTACHMENT",
                    (b"FILENAME*", b"utf-8''%D0%94%D0%BE%D0%B3%D0%BE%D0%B2%D0%BE%D1%80.pdf"),
                ),
                None,
            ),
            (
                b"IMAGE",
                b"PNG",
                None,
                b"<logo>",
                None,
                b"BASE64",
                8,
                None,
                (b"INLINE", (b"FILENAME", b"logo.png")),
                None,
            ),
        ],
        b"MIXED",
    )
    sections, attachments = _text_sections(structure)
    assert sections == [("1", "text/plain", "utf-8", "8BIT")]
    assert attachments == [
        {"filename": "Договор.pdf", "content_type": "application/pdf", "size": 24, "section": "2"},
        {"filename": "logo.png", "content_type": "image/png", "size": 8, "section": "3"},
    ]


def test_rfc2231_and_rfc2047_names_are_decoded_in_both_paths():
    raw = (
        "Subject: x\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
        "--b\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        "--b\r\nContent-Type: application/pdf\r\n"
        f"Content-Disposition: attachment; filename*={RFC2231_NAME}\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\nJVBERg==\r\n"
        '--b\r\nContent-Type: application/pdf; name="=?UTF-8?B?0KHRh9GR0YIucGRm?="\r\n'
        "Content-Transfer-Encoding: base64\r\n\r\nJVBERg==\r\n"
        "--b--\r\n"
    ).encode()
    names = [item["filename"] for item in parse_message(raw, 1)["attachments"]]
    assert names == [CYRILLIC_NAME, "Счёт.pdf"]
    pdf = (b"APPLICATION", b"PDF")
    structure = (
        [
            (b"TEXT", b"PLAIN", None, None, None, b"7BIT", 4, 1, None, None),
            (
                *pdf,
                None,
                None,
                None,
                b"BASE64",
                8,
                None,
                (b"ATTACHMENT", (b"FILENAME*", RFC2231_NAME.encode())),
                None,
            ),
            (
                *pdf,
                (b"NAME", b"=?UTF-8?B?0KHRh9GR0YIucGRm?="),
                None,
                None,
                b"BASE64",
                8,
                None,
                None,
                None,
            ),
            (
                *pdf,
                None,
                None,
                None,
                b"BASE64",
                8,
                None,
                (b"ATTACHMENT", (b"FILENAME*0*", b"UTF-8''%D0%A1", b"FILENAME*1*", b"%D1%8F.pdf")),
                None,
            ),
        ],
        b"MIXED",
    )
    names = [item["filename"] for item in _text_sections(structure)[1]]
    assert names == [CYRILLIC_NAME, "Счёт.pdf", "Ся.pdf"]


def test_forwarded_message_is_listed_with_its_disposition():
    envelope = (None,) * 10
    inner = (b"TEXT", b"PLAIN", None, None, None, b"7BIT", 5, 1, None, None)
    structure = (
        [
            (b"TEXT", b"PLAIN", None, None, None, b"7BIT", 4, 1, None, None),
            (
                b"MESSAGE",
                b"RFC822",
                None,
                None,
                None,
                b"7BIT",
                300,
                envelope,
                inner,
                9,
                None,
                (b"ATTACHMENT", (b"FILENAME", b"forwarded.eml")),
            ),
        ],
        b"MIXED",
    )
    assert _text_sections(structure)[1] == [
        {"filename": "forwarded.eml", "content_type": "message/rfc822", "size": 300, "section": "2"}
    ]


def test_list_unsubscribe_is_returned_on_one_line():
    raw = (
        b"Subject: News\r\n"
        b"List-Unsubscribe: <https://example.com/unsubscribe?id=1>,\r\n <mailto:u@example.com>\r\n"
        b"\r\nbody"
    )
    parsed = parse_message(raw, 1)
    assert parsed["list_unsubscribe"] == (
        "<https://example.com/unsubscribe?id=1>, <mailto:u@example.com>"
    )
    assert parse_message(b"Subject: x\r\n\r\n", 1)["list_unsubscribe"] == ""


def test_fetch_returns_attachments_and_list_unsubscribe():
    structure = (
        [
            (b"TEXT", b"PLAIN", None, None, None, b"7BIT", 4, 1, None, None),
            (b"APPLICATION", b"PDF", (b"NAME", b"a.pdf"), None, None, b"BASE64", 9, None, None),
        ],
        b"MIXED",
    )

    class IMAP:
        def fetch(self, uids, fields):
            if fields == ["BODY.PEEK[HEADER]", "BODYSTRUCTURE"]:
                header = b"Subject: News\r\nList-Unsubscribe: <mailto:off@example.com>\r\n\r\n"
                return {1: {b"BODY[HEADER]": header, b"BODYSTRUCTURE": structure}}
            return {1: {b"BODY[1]<0>": b"body"}}

    [summary] = Mailbox._fetch(IMAP(), [1])
    assert summary["attachments"] == [
        {"filename": "a.pdf", "content_type": "application/pdf", "size": 9, "section": "2"}
    ]
    assert summary["list_unsubscribe"] == "<mailto:off@example.com>"
