"""Read-only-first Yandex IMAP interface.

Always use BODY.PEEK[] so inspection does not mark mail as read.
No delete, expunge, SMTP, or arbitrary folder mutation is exposed.
"""

from __future__ import annotations

import base64
import binascii
import imaplib
import logging
import quopri
import re
from collections.abc import Iterator
from contextlib import contextmanager
from email import policy
from email.message import MIMEPart
from email.parser import BytesParser
from html.parser import HTMLParser
from typing import Any

from imapclient import IMAPClient
from typing_extensions import TypedDict

from .config import RESERVED_FOLDER_NAMES, Settings

LOG = logging.getLogger(__name__)

MailSummary = TypedDict(
    "MailSummary",
    {
        "uid": int,
        "message_id": str,
        "subject": str,
        "from": str,
        "to": str,
        "date": str,
        "preview": str,
        "has_attachments": bool,
    },
)


class MailError(Exception):
    """An expected error that can be reported to a tool caller."""


class MailValueError(MailError, ValueError):
    pass


class MailLookupError(MailError, LookupError):
    pass


class MailPermissionError(MailError, PermissionError):
    pass


class _TextFromHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.fragments: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style"}:
            self.hidden += 1
        elif tag.lower() in {"p", "br", "div", "td", "th", "tr", "li", "hr"} and not self.hidden:
            self.fragments.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self.hidden:
            self.hidden -= 1
        elif tag.lower() in {"p", "div", "td", "th", "tr", "li"} and not self.hidden:
            self.fragments.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.fragments.append(data)


def _is_attachment(kind: str, disposition: str, has_filename: bool) -> bool:
    return disposition == "attachment" or has_filename or kind not in {"text", "multipart"}


def parse_message(raw: bytes, uid: int, *, max_body_chars: int = 4000) -> MailSummary:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    text_parts: list[str] = []
    html_parts: list[str] = []
    has_attachments = False

    def collect(part: MIMEPart, root: bool = False) -> None:
        nonlocal has_attachments
        # A name or attachment disposition on the message itself does not make its body
        # an attachment; only a non-text root type does.
        if _is_attachment(
            part.get_content_maintype(),
            "" if root else part.get_content_disposition() or "",
            not root and part.get_filename() is not None,
        ):
            has_attachments = True
            return
        if part.is_multipart():
            for child in part.iter_parts():
                collect(child)
            return
        if part.get_content_type() not in {"text/plain", "text/html"}:
            return
        try:
            content = part.get_content()
            if not isinstance(content, str):
                return
        except (UnicodeError, LookupError, ValueError, TypeError):
            return
        if part.get_content_type() == "text/plain":
            text_parts.append(content)
        else:
            html_parts.append(content)

    collect(msg, root=True)
    if text_parts:
        body = "\n".join(text_parts)
    else:
        parser = _TextFromHTML()
        parser.feed("\n".join(html_parts))
        body = " ".join(parser.fragments)
    body = re.sub(r"\s+", " ", body).strip()
    date_header = msg.get("Date")
    parsed_date = getattr(date_header, "datetime", None)
    date = parsed_date.isoformat() if parsed_date is not None else str(date_header or "")
    return {
        "uid": uid,
        "message_id": str(msg.get("Message-ID", "")),
        "subject": str(msg.get("Subject", "")),
        "from": str(msg.get("From", "")),
        "to": str(msg.get("To", "")),
        "date": date,
        "preview": body[: min(max_body_chars, 10000)],
        "has_attachments": has_attachments,
    }


def valid_uid(uid: int) -> int:
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        raise MailValueError("uid must be a positive integer")
    return uid


def _text_sections(structure: Any) -> tuple[list[tuple[str, str, str, str]], bool]:
    """Return safe text section metadata and whether MIME attachments exist."""
    sections: list[tuple[str, str, str, str]] = []
    has_attachments = False

    def string(value: Any, default: str = "") -> str:
        if isinstance(value, bytes):
            return value.decode("ascii", errors="replace")
        return value if isinstance(value, str) else default

    def walk(part: Any, path: str) -> None:
        nonlocal has_attachments
        if not isinstance(part, (tuple, list)) or not part:
            return
        if isinstance(part[0], list):
            disposition = part[3] if len(part) > 3 else None
            name = (
                string(disposition[0]).lower()
                if isinstance(disposition, (tuple, list)) and disposition
                else ""
            )
            disp_params = (
                disposition[1]
                if isinstance(disposition, (tuple, list)) and len(disposition) > 1
                else None
            )
            has_filename = isinstance(disp_params, (tuple, list)) and any(
                string(value).lower() == "filename" for value in disp_params[::2]
            )
            if path and _is_attachment("multipart", name, has_filename):
                has_attachments = True
                return
            for index, child in enumerate(part[0], 1):
                walk(child, f"{path}.{index}" if path else str(index))
            return
        if len(part) < 7:
            return
        kind = string(part[0]).lower()
        subtype = string(part[1]).lower()
        raw_params = part[2] if isinstance(part[2], (tuple, list)) else ()
        params = {
            string(raw_params[i]).lower(): string(raw_params[i + 1])
            for i in range(0, len(raw_params) - 1, 2)
        }
        disposition_index = 9 if kind == "text" else 8
        disposition = part[disposition_index] if len(part) > disposition_index else None
        disposition_name = (
            string(disposition[0]).lower()
            if isinstance(disposition, (tuple, list)) and disposition
            else ""
        )
        disposition_params = (
            disposition[1]
            if isinstance(disposition, (tuple, list)) and len(disposition) > 1
            else None
        )
        has_filename = bool(params.get("name"))
        if isinstance(disposition_params, (tuple, list)):
            has_filename |= any(
                string(value).lower() == "filename" for value in disposition_params[::2]
            )
        # As in parse_message, the root part is judged by its type only.
        root = not path
        is_attachment = _is_attachment(
            kind, "" if root else disposition_name, has_filename and not root
        )
        if is_attachment:
            has_attachments = True
        if kind == "text" and subtype in {"plain", "html"} and not is_attachment:
            sections.append(
                (
                    path or "1",
                    f"text/{subtype}",
                    params.get("charset", "utf-8"),
                    string(part[5], "7bit"),
                )
            )

    walk(structure, "")
    sections.sort(key=lambda item: item[1] != "text/plain")
    return sections, has_attachments


class Mailbox:
    def __init__(self, config: Settings):
        self.config = config

    @contextmanager
    def connect(self) -> Iterator[IMAPClient]:
        with IMAPClient(
            self.config.imap_host,
            port=self.config.imap_port,
            ssl=True,
            timeout=30,
        ) as imap:
            imap.login(self.config.email, self.config.app_password.get_secret_value())
            yield imap

    @staticmethod
    def _fetch(imap: IMAPClient, uids: list[int], max_body_chars: int = 4000) -> list[MailSummary]:
        if not uids:
            return []
        fetched = imap.fetch(uids, ["BODY.PEEK[HEADER]", "BODYSTRUCTURE"])
        summaries: dict[int, MailSummary] = {}
        sections: dict[str, list[tuple[int, str, str, str]]] = {}

        def failure(uid: int) -> None:
            LOG.warning("Incomplete IMAP response for UID %s", uid)
            summaries.pop(uid, None)

        for uid in uids:
            if uid not in fetched:
                continue  # Message disappeared between SEARCH and FETCH.
            item = fetched[uid]
            header, structure = item.get(b"BODY[HEADER]"), item.get(b"BODYSTRUCTURE")
            if not isinstance(header, bytes) or not structure:
                failure(uid)
                continue
            summary = parse_message(header, uid, max_body_chars=max_body_chars)
            parts, summary["has_attachments"] = _text_sections(structure)
            summaries[uid] = summary
            if parts:
                section, content_type, charset, encoding = parts[0]
                sections.setdefault(section, []).append((uid, content_type, charset, encoding))

        for section, entries in sections.items():
            responses = imap.fetch(
                [entry[0] for entry in entries], [f"BODY.PEEK[{section}]<0.16384>"]
            )
            for uid, content_type, charset, encoding in entries:
                raw = responses.get(uid, {}).get(f"BODY[{section}]<0>".encode())
                if not isinstance(raw, bytes):
                    failure(uid)
                    continue
                try:
                    if encoding.lower() == "base64":
                        encoded = b"".join(raw.split())
                        if len(encoded) % 4 == 1:
                            encoded = encoded[:-1]
                        encoded += b"=" * (-len(encoded) % 4)
                        payload = base64.b64decode(encoded, validate=True)
                    elif encoding.lower() == "quoted-printable":
                        payload = quopri.decodestring(raw)
                    else:
                        payload = raw
                    try:
                        content = payload.decode(charset, errors="replace")
                    except (LookupError, ValueError):
                        content = payload.decode("utf-8", errors="replace")
                    if content_type == "text/html":
                        parser = _TextFromHTML()
                        parser.feed(content)
                        content = " ".join(parser.fragments)
                    summaries[uid]["preview"] = re.sub(r"\s+", " ", content).strip()[
                        : min(max_body_chars, 10000)
                    ]
                except (binascii.Error, ValueError):
                    failure(uid)
        return [summaries[uid] for uid in uids if uid in summaries]

    def folders(self) -> list[str]:
        with self.connect() as imap:
            return [str(name) for _, _, name in imap.list_folders()]

    def recent(
        self, limit: int = 20, unread_only: bool = False, folder: str = "INBOX"
    ) -> list[MailSummary]:
        limit = max(1, min(limit, 100))
        with self.connect() as imap:
            imap.select_folder(folder, readonly=True)
            uids = imap.search(["UNSEEN"] if unread_only else ["ALL"])
            return self._fetch(imap, sorted(uids)[-limit:][::-1])

    def search(self, query: str, limit: int = 20, folder: str = "INBOX") -> list[MailSummary]:
        """Search the server, falling back to the latest 300 previews if unsupported."""
        if not query.strip():
            raise MailValueError("query cannot be empty")
        limit = max(1, min(limit, 100))
        needle = query.casefold()
        with self.connect() as imap:
            imap.select_folder(folder, readonly=True)
            try:
                uids = sorted(imap.search(["TEXT", query], charset="UTF-8"))[::-1]
            except imaplib.IMAP4.abort:
                raise
            except imaplib.IMAP4.error:
                uids = sorted(imap.search(["ALL"]))[-300:][::-1]
            else:
                return self._fetch(imap, uids[:limit], max_body_chars=2000)
            found: list[MailSummary] = []
            for start in range(0, len(uids), 30):
                for message in self._fetch(imap, uids[start : start + 30], max_body_chars=2000):
                    if (
                        needle
                        in " ".join(
                            [message["from"], message["subject"], message["preview"]]
                        ).casefold()
                    ):
                        found.append(message)
                        if len(found) >= limit:
                            return found
            return found

    def message(self, uid: int, folder: str = "INBOX") -> MailSummary:
        valid_uid(uid)
        with self.connect() as imap:
            imap.select_folder(folder, readonly=True)
            item = imap.fetch([uid], ["RFC822.SIZE"]).get(uid, {})
            if not item:
                raise MailLookupError(f"Message UID {uid} not found in {folder}")
            size = item.get(b"RFC822.SIZE")
            if not isinstance(size, int) or size > 20 * 1024 * 1024:
                raise MailValueError(
                    "Message size is unavailable or exceeds the 20 MiB download limit"
                )
            raw = imap.fetch([uid], ["BODY.PEEK[]"]).get(uid, {}).get(b"BODY[]")
            if not isinstance(raw, bytes):
                raise MailLookupError(f"Message UID {uid} not found in {folder}")
            return parse_message(raw, uid, max_body_chars=10000)

    def headers(self, uid: int, folder: str = "INBOX") -> MailSummary:
        valid_uid(uid)
        with self.connect() as imap:
            return self._headers(imap, uid, folder)

    @staticmethod
    def _headers(imap: IMAPClient, uid: int, folder: str) -> MailSummary:
        imap.select_folder(folder, readonly=True)
        item = imap.fetch([uid], ["BODY.PEEK[HEADER]"]).get(uid, {})
        raw = item.get(b"BODY[HEADER]")
        if not isinstance(raw, bytes):
            raise MailLookupError(f"Message UID {uid} not found or header unavailable in {folder}")
        return parse_message(raw, uid, max_body_chars=0)

    def preview_promotions_move(self, uid: int) -> tuple[MailSummary, str]:
        """Validate the promotions folder and read the INBOX message headers.

        Returns the headers and the folder name as the server spells it, so the
        caller can show both to a human before calling move_to_promotions.
        """
        valid_uid(uid)
        self._promotions_folder_name()
        with self.connect() as imap:
            folder, _ = self.validate_promotions_folder(imap)
            return self._headers(imap, uid, "INBOX"), folder

    def _promotions_folder_name(self) -> str:
        # Checks that need no connection; Settings.from_env runs the same ones.
        folder = self.config.promotions_folder.strip()
        if not folder or folder.casefold() in RESERVED_FOLDER_NAMES:
            raise MailValueError("Choose a safe dedicated promotions folder")
        return folder

    def validate_promotions_folder(self, imap: IMAPClient) -> tuple[str, bool]:
        """Check that the configured promotions folder is safe to move mail into.

        Returns the canonical folder name and whether the folder already exists.
        """
        folder = self._promotions_folder_name()
        folders = imap.list_folders()
        delimiters = {
            delimiter.decode() if isinstance(delimiter, bytes) else delimiter
            for _, delimiter, _ in folders
            if delimiter
        }
        if any(delimiter in folder for delimiter in delimiters):
            raise MailValueError(
                "Choose a safe dedicated top-level promotions folder; nested folders are not allowed"
            )
        matching = [
            (flags, delimiter, name)
            for flags, delimiter, name in folders
            if name.casefold() == folder.casefold()
        ]
        if len(matching) > 1:
            raise MailValueError("Promotions folder name is ambiguous")
        if matching:
            flags, _, folder = matching[0]
            special = {
                str(flag.decode() if isinstance(flag, bytes) else flag).casefold() for flag in flags
            }
            if special & {
                "\\trash",
                "\\junk",
                "\\drafts",
                "\\sent",
                "\\all",
                "\\flagged",
                "\\inbox",
                "\\noselect",
            }:
                raise MailValueError("Choose a safe dedicated promotions folder")
        if not imap.has_capability("MOVE"):
            raise MailError("IMAP server does not support MOVE")
        return folder, bool(matching)

    def move_to_promotions(self, uid: int, confirm: bool = False) -> dict[str, Any]:
        valid_uid(uid)
        if not self.config.allow_write:
            raise MailPermissionError("Write operations are disabled. Set YANDEX_ALLOW_WRITE=true")
        if confirm is not True:
            raise MailPermissionError("Explicit confirm=true is required to move a message")
        self._promotions_folder_name()
        with self.connect() as imap:
            folder, exists = self.validate_promotions_folder(imap)
            if not exists:
                imap.create_folder(folder)
            imap.select_folder("INBOX", readonly=False)
            if uid not in imap.search(["UID", str(uid)]):
                raise MailLookupError(f"Message UID {uid} not found in INBOX")
            imap.move([uid], folder)
        return {"uid": uid, "moved_to": folder, "deleted_permanently": False}
