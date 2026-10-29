"""Read-only-first Yandex IMAP interface.

Always use BODY.PEEK[] so inspection does not mark mail as read.
No delete, expunge, SMTP, or arbitrary folder mutation is exposed.
"""

from __future__ import annotations

import base64
import binascii
import imaplib
import logging
import queue
import quopri
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email import policy
from email.errors import HeaderParseError
from email.header import decode_header, make_header
from email.message import EmailMessage, Message, MIMEPart
from email.parser import BytesParser
from email.utils import (
    collapse_rfc2231_value,
    decode_params,
    formataddr,
    formatdate,
    getaddresses,
    make_msgid,
    unquote,
)
from html.parser import HTMLParser
from typing import Any

from imapclient import IMAPClient
from typing_extensions import TypedDict

from .config import RESERVED_FOLDER_NAMES, Settings

LOG = logging.getLogger(__name__)


class AttachmentInfo(TypedDict):
    """An attachment's metadata; its content is never returned.

    size is the encoded size in bytes as stored in the message, so base64
    content is about a third larger than the file. section is the IMAP part number.
    """

    filename: str
    content_type: str
    size: int
    section: str


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
        "attachments": list[AttachmentInfo],
        "list_unsubscribe": str,
        "in_reply_to": str,
        "references": list[str],
    },
)


class ThreadMessage(MailSummary):
    """A message found by get_thread, which can come from several folders."""

    folder: str


_MESSAGE_ID = re.compile(r"<[^<>\s]+>")
# Printable ASCII without IMAP atom-specials, so a SEARCH can send the id as an atom.
_SEARCHABLE_ID = re.compile(r"<[!#$&'+,\-./0-9:;=?@A-Z\[^_`a-z|}~]+>")


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


def _header_text(value: str) -> str:
    """Decode RFC 2047 encoded words, keeping the raw value if they are malformed."""
    try:
        return str(make_header(decode_header(value)))
    except (HeaderParseError, LookupError, UnicodeError, ValueError):
        return value


def _filename(params: list[tuple[str, str]]) -> str:
    """The file name from Content-Disposition or Content-Type parameters.

    Handles RFC 2231 continuations and charsets, then RFC 2047 encoded words,
    which many mail clients still use for non-ASCII names.
    """
    try:
        decoded = decode_params([("", ""), *params])[1:]
    except (LookupError, UnicodeError, ValueError):
        return ""
    for key in ("filename", "name"):
        for name, value in decoded:
            if name.lower() != key:
                continue
            try:
                text = collapse_rfc2231_value(value)
            except (LookupError, UnicodeError):
                text = value[2] if isinstance(value, tuple) else value
            return re.sub(r"\s+", " ", _header_text(unquote(text))).strip()
    return ""


def parse_message(raw: bytes, uid: int, *, max_body_chars: int = 4000) -> MailSummary:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[AttachmentInfo] = []

    def collect(part: MIMEPart, path: str = "") -> None:
        root = not path
        params = [
            (str(key), str(value))
            for header in ("Content-Type", "Content-Disposition")
            for key, value in (part.get_params(header=header) or [])[1:]
        ]
        filename = _filename(params)
        # A name or attachment disposition on the message itself does not make its body
        # an attachment; only a non-text root type does.
        if _is_attachment(
            part.get_content_maintype(),
            "" if root else part.get_content_disposition() or "",
            not root and (bool(filename) or part.get_filename() is not None),
        ):
            payload = part.get_payload()
            raw = (
                payload.encode("utf-8", "surrogateescape")
                if isinstance(payload, str)
                else part.as_bytes().partition(b"\n\n")[2]
            )
            attachments.append(
                {
                    "filename": filename,
                    "content_type": part.get_content_type(),
                    "size": len(raw),
                    "section": path or "1",
                }
            )
            return
        if part.is_multipart():
            for index, child in enumerate(part.iter_parts(), 1):
                collect(child, f"{path}.{index}" if path else str(index))
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

    collect(msg)
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
        "has_attachments": bool(attachments),
        "attachments": attachments,
        "list_unsubscribe": re.sub(r"\s+", " ", str(msg.get("List-Unsubscribe", ""))).strip(),
        "in_reply_to": str(msg.get("In-Reply-To", "")).strip(),
        "references": _MESSAGE_ID.findall(str(msg.get("References", ""))),
    }


def _search_date(name: str, value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise MailValueError(f"{name} must be a date in YYYY-MM-DD format") from exc


def search_criteria(
    *,
    query: str | None = None,
    sender: str | None = None,
    to: str | None = None,
    subject: str | None = None,
    since: str | None = None,
    before: str | None = None,
    unread: bool | None = None,
    flagged: bool | None = None,
) -> list[Any]:
    """Build IMAP SEARCH criteria; IMAPClient quotes strings and formats dates."""
    criteria: list[Any] = []
    for name, key, value in (
        ("query", "TEXT", query),
        ("sender", "FROM", sender),
        ("to", "TO", to),
        ("subject", "SUBJECT", subject),
    ):
        if value is None:
            continue
        if not value.strip():
            raise MailValueError(f"{name} cannot be empty")
        criteria += [key, value]
    since_date = None if since is None else _search_date("since", since)
    before_date = None if before is None else _search_date("before", before)
    if since_date and before_date and since_date >= before_date:
        raise MailValueError("since must be earlier than before")
    if since_date:
        criteria += ["SINCE", since_date]
    if before_date:
        criteria += ["BEFORE", before_date]
    if unread is not None:
        criteria.append("UNSEEN" if unread else "SEEN")
    if flagged is not None:
        criteria.append("FLAGGED" if flagged else "UNFLAGGED")
    return criteria


def valid_uid(uid: int, name: str = "uid") -> int:
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        raise MailValueError(f"{name} must be a positive integer")
    return uid


# Folders mail is never moved into, or (for the first four) under.
SYSTEM_FOLDER_FLAGS = frozenset({"\\trash", "\\junk", "\\drafts", "\\sent"})
FORBIDDEN_DESTINATION_FLAGS = SYSTEM_FOLDER_FLAGS | {
    "\\all",
    "\\flagged",
    "\\noselect",
    "\\nonexistent",
}


def _flag_names(flags: Any) -> set[str]:
    return {str(flag.decode() if isinstance(flag, bytes) else flag).casefold() for flag in flags}


def _thread_ids(message: MailSummary) -> set[str]:
    ids = _MESSAGE_ID.findall(" ".join([message["message_id"], message["in_reply_to"]]))
    return set(ids) | set(message["references"])


def _thread_criteria(ids: list[str]) -> list[str]:
    """SEARCH criteria matching any message that has or refers to one of the ids."""
    keys = [
        ["HEADER", header, message_id]
        for message_id in ids
        for header in ("Message-ID", "In-Reply-To", "References")
    ]
    # Prefix notation: n keys need n - 1 leading ORs.
    return ["OR"] * (len(keys) - 1) + [token for key in keys for token in key]


def _date_key(message: MailSummary) -> tuple[int, datetime]:
    try:
        parsed = datetime.fromisoformat(message["date"])
    except ValueError:
        return (1, datetime.min.replace(tzinfo=UTC))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (0, parsed)


def _uid_scope(before_uid: int | None) -> list[str] | None:
    """SEARCH criteria for UIDs below before_uid, or None when no UID can match."""
    if before_uid is None:
        return []
    valid_uid(before_uid, "before_uid")
    return ["UID", f"1:{before_uid - 1}"] if before_uid > 1 else None


def _text_sections(structure: Any) -> tuple[list[tuple[str, str, str, str]], list[AttachmentInfo]]:
    """Return safe text section metadata and the MIME attachments' metadata."""
    sections: list[tuple[str, str, str, str]] = []
    attachments: list[AttachmentInfo] = []

    def string(value: Any, default: str = "") -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value if isinstance(value, str) else default

    def pairs(values: Any) -> list[tuple[str, str]]:
        if not isinstance(values, (tuple, list)):
            return []
        return [
            (string(values[i]).lower(), string(values[i + 1])) for i in range(0, len(values) - 1, 2)
        ]

    def disposition_of(value: Any) -> tuple[str, list[tuple[str, str]]]:
        if not isinstance(value, (tuple, list)) or not value:
            return "", []
        return string(value[0]).lower(), pairs(value[1]) if len(value) > 1 else []

    def size_of(part: Any) -> int:
        if isinstance(part, (tuple, list)) and part and isinstance(part[0], list):
            return sum(size_of(child) for child in part[0])
        if isinstance(part, (tuple, list)) and len(part) > 6 and isinstance(part[6], int):
            return part[6]
        return 0

    def named(params: list[tuple[str, str]]) -> bool:
        # RFC 2231 spells the parameter filename* or filename*0*.
        return any(key.split("*")[0] in {"filename", "name"} for key, _ in params)

    def attach(part: Any, path: str, content_type: str, params: list[tuple[str, str]]) -> None:
        attachments.append(
            {
                "filename": _filename(params),
                "content_type": content_type,
                "size": size_of(part),
                "section": path or "1",
            }
        )

    def walk(part: Any, path: str) -> None:
        if not isinstance(part, (tuple, list)) or not part:
            return
        if isinstance(part[0], list):
            name, disp_params = disposition_of(part[3] if len(part) > 3 else None)
            if path and _is_attachment("multipart", name, named(disp_params)):
                subtype = string(part[1]).lower() if len(part) > 1 else "mixed"
                attach(part, path, f"multipart/{subtype}", disp_params)
                return
            for index, child in enumerate(part[0], 1):
                walk(child, f"{path}.{index}" if path else str(index))
            return
        if len(part) < 7:
            return
        kind = string(part[0]).lower()
        subtype = string(part[1]).lower()
        params = pairs(part[2])
        # Extension data follows the type-specific fields: text has a line count,
        # message/rfc822 an envelope, body structure and line count.
        if kind == "text":
            disposition_index = 9
        elif (kind, subtype) == ("message", "rfc822"):
            disposition_index = 11
        else:
            disposition_index = 8
        disposition_name, disposition_params = disposition_of(
            part[disposition_index] if len(part) > disposition_index else None
        )
        # As in parse_message, the root part is judged by its type only.
        root = not path
        if _is_attachment(
            kind,
            "" if root else disposition_name,
            not root and named(params + disposition_params),
        ):
            attach(part, path, f"{kind}/{subtype}", disposition_params + params)
        elif kind == "text" and subtype in {"plain", "html"}:
            sections.append(
                (
                    path or "1",
                    f"text/{subtype}",
                    dict(params).get("charset", "utf-8"),
                    string(part[5], "7bit"),
                )
            )

    walk(structure, "")
    sections.sort(key=lambda item: item[1] != "text/plain")
    return sections, attachments


def _close(imap: IMAPClient) -> None:
    # The same cleanup as IMAPClient.__exit__: errors here mean the session is gone.
    try:
        imap.logout()
    except Exception:
        try:
            imap.shutdown()
        except Exception as exc:
            LOG.info("Could not close the IMAP connection cleanly: %s", exc)


def _selected(imap: IMAPClient) -> bool:
    # IMAPClient keeps no public record of the selected folder; imaplib's state does.
    return getattr(getattr(imap, "_imap", None), "state", None) == "SELECTED"


class ConnectionPool:
    """Reuse logged-in IMAP connections across tool calls.

    At most ``size`` connections are open at once, since Yandex limits concurrent
    IMAP sessions per account. A connection that has been idle longer than
    ``idle_timeout`` seconds or fails NOOP is replaced. A connection is returned
    with no folder selected, and is closed instead if the caller raised.
    """

    def __init__(
        self,
        factory: Callable[[], IMAPClient],
        size: int = 2,
        idle_timeout: float = 300,
        *,
        wait_timeout: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = factory
        self._idle: queue.LifoQueue[tuple[IMAPClient, float]] = queue.LifoQueue()
        self._slots = threading.BoundedSemaphore(size)
        self._idle_timeout = idle_timeout
        self._wait_timeout = wait_timeout
        self._clock = clock
        self._closed = False

    @contextmanager
    def connection(self) -> Iterator[IMAPClient]:
        if not self._slots.acquire(timeout=self._wait_timeout):
            raise MailError("All IMAP connections are busy; please retry")
        try:
            imap = self._checkout()
            try:
                yield imap
            except MailError:
                # Raised by this package between complete commands: the session is fine.
                self._checkin(imap)
                raise
            except BaseException:
                _close(imap)
                raise
            self._checkin(imap)
        finally:
            self._slots.release()

    def _checkout(self) -> IMAPClient:
        while True:
            try:
                imap, last_used = self._idle.get_nowait()
            except queue.Empty:
                return self._factory()
            if self._clock() - last_used > self._idle_timeout:
                _close(imap)
                continue
            try:
                imap.noop()
            except (imaplib.IMAP4.error, OSError):
                # IMAP4.abort is an IMAP4.error.
                _close(imap)
                continue
            return imap

    def _checkin(self, imap: IMAPClient) -> None:
        """Return a connection to the pool, or close it; never raise.

        The caller's command has already completed, so a failure here must not
        replace its result.
        """
        if self._closed:
            _close(imap)
            return
        try:
            if _selected(imap):
                # CLOSE would expunge messages marked deleted, so only UNSELECT is safe.
                if not imap.has_capability("UNSELECT"):
                    _close(imap)
                    return
                imap.unselect_folder()
                # IMAPClient sends UNSELECT without updating imaplib's state.
                imap._imap.state = "AUTH"
        except Exception as exc:
            LOG.info("Closing an IMAP connection that could not be reset: %s", exc)
            _close(imap)
            return
        self._idle.put((imap, self._clock()))

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                imap, _ = self._idle.get_nowait()
            except queue.Empty:
                return
            _close(imap)


MAX_DRAFT_BODY = 100 * 1024


@dataclass(frozen=True)
class Draft:
    """A draft message ready to APPEND to the drafts folder."""

    folder: str
    message: EmailMessage


def _address(field: str, value: str) -> str:
    """Validate one address such as "Name <user@example.com>" and return it formatted."""
    if "\r" in value or "\n" in value:
        raise MailValueError(f"{field} addresses must be single lines")
    pairs = getaddresses([value])
    if len(pairs) != 1 or not re.fullmatch(r"[^@\s<>,;]+@[^@\s<>,;]+", pairs[0][1]):
        raise MailValueError(f"{field} must contain valid email addresses, one per item")
    return formataddr(pairs[0])


class Mailbox:
    def __init__(self, config: Settings):
        self.config = config
        self.pool = ConnectionPool(
            self._open, size=config.imap_pool_size, idle_timeout=config.imap_idle_timeout
        )

    def _open(self) -> IMAPClient:
        imap = IMAPClient(
            self.config.imap_host,
            port=self.config.imap_port,
            ssl=True,
            timeout=30,
        )
        try:
            imap.login(self.config.email, self.config.app_password.get_secret_value())
        except BaseException:
            _close(imap)
            raise
        return imap

    @contextmanager
    def connect(self) -> Iterator[IMAPClient]:
        with self.pool.connection() as imap:
            yield imap

    def close(self) -> None:
        self.pool.close()

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
            parts, attachments = _text_sections(structure)
            summary["attachments"] = attachments
            summary["has_attachments"] = bool(attachments)
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
        self,
        limit: int = 20,
        unread_only: bool = False,
        folder: str = "INBOX",
        before_uid: int | None = None,
    ) -> list[MailSummary]:
        limit = max(1, min(limit, 100))
        scope = _uid_scope(before_uid)
        if scope is None:
            return []
        with self.connect() as imap:
            imap.select_folder(folder, readonly=True)
            uids = imap.search((["UNSEEN"] if unread_only else []) + scope or ["ALL"])
            return self._fetch(imap, sorted(uids)[-limit:][::-1])

    def search(
        self,
        query: str | None = None,
        *,
        sender: str | None = None,
        to: str | None = None,
        subject: str | None = None,
        since: str | None = None,
        before: str | None = None,
        unread: bool | None = None,
        flagged: bool | None = None,
        has_attachments: bool | None = None,
        limit: int = 20,
        folder: str = "INBOX",
        before_uid: int | None = None,
    ) -> list[MailSummary]:
        """Search the server with the given criteria, newest first.

        A query alone falls back to the latest 300 previews if the server rejects it.
        """
        criteria = search_criteria(
            query=query,
            sender=sender,
            to=to,
            subject=subject,
            since=since,
            before=before,
            unread=unread,
            flagged=flagged,
        )
        if not criteria and has_attachments is None:
            raise MailValueError("Give at least one search criterion")
        limit = max(1, min(limit, 100))
        scope = _uid_scope(before_uid)
        if scope is None:
            return []
        with self.connect() as imap:
            imap.select_folder(folder, readonly=True)
            try:
                uids = sorted(imap.search(criteria + scope or ["ALL"], charset="UTF-8"))[::-1]
            except imaplib.IMAP4.abort:
                raise
            except imaplib.IMAP4.error:
                if query is None or criteria != ["TEXT", query] or has_attachments is not None:
                    raise
                return self._search_previews(imap, query, limit, scope)
            if has_attachments is None:
                return self._fetch(imap, uids[:limit], max_body_chars=2000)
            return self._filter_attachments(imap, uids, has_attachments, limit)

    def _search_previews(
        self, imap: IMAPClient, query: str, limit: int, scope: list[str]
    ) -> list[MailSummary]:
        needle = query.casefold()
        uids = sorted(imap.search(scope or ["ALL"]))[-300:][::-1]
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

    def _filter_attachments(
        self, imap: IMAPClient, uids: list[int], has_attachments: bool, limit: int
    ) -> list[MailSummary]:
        """Keep messages whose MIME structure matches, scanning at most 500 candidates."""
        found: list[MailSummary] = []
        candidates = uids[:500]
        for start in range(0, len(candidates), 50):
            batch = candidates[start : start + 50]
            structures = imap.fetch(batch, ["BODYSTRUCTURE"])
            matching = [
                uid
                for uid in batch
                if structures.get(uid, {}).get(b"BODYSTRUCTURE")
                and bool(_text_sections(structures[uid][b"BODYSTRUCTURE"])[1]) == has_attachments
            ]
            found.extend(self._fetch(imap, matching[: limit - len(found)], max_body_chars=2000))
            if len(found) >= limit:
                break
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

    def thread(self, uid: int, folder: str = "INBOX", limit: int = 50) -> list[ThreadMessage]:
        """Collect the conversation of a message by Message-ID, In-Reply-To and References.

        Searches the given folder, INBOX and the folder flagged \\Sent, repeating with
        newly found identifiers until none are new or limit messages are found.
        Returns the messages oldest first.
        """
        valid_uid(uid)
        limit = max(1, min(limit, 100))
        with self.connect() as imap:
            origin = self._headers(imap, uid, folder)
            folders = [folder]
            for flags, _, name in imap.list_folders():
                if (name.upper() == "INBOX" or "\\sent" in _flag_names(flags)) and all(
                    name.casefold() != known.casefold() for known in folders
                ):
                    folders.append(name)
            found: dict[tuple[str, int], ThreadMessage] = {
                (folder, uid): {**origin, "folder": folder}
            }
            known = _thread_ids(origin)
            searched: set[str] = set()
            while len(found) < limit and (pending := sorted(known - searched)):
                searched.update(pending)
                # IMAPClient sends other ids unquoted or as us-ascii; skipping them
                # loses only links that no other id in the thread provides.
                pending = [value for value in pending if _SEARCHABLE_ID.fullmatch(value)]
                for name in folders:
                    if not pending or len(found) >= limit:
                        break
                    imap.select_folder(name, readonly=True)
                    uids: set[int] = set()
                    for start in range(0, len(pending), 10):
                        uids.update(imap.search(_thread_criteria(pending[start : start + 10])))
                    new = sorted(u for u in uids if (name, u) not in found)
                    for message in self._fetch(
                        imap, new[: limit - len(found)], max_body_chars=2000
                    ):
                        found[(name, message["uid"])] = {**message, "folder": name}
                        known |= _thread_ids(message)
        unique: dict[str, ThreadMessage] = {}
        for key, message in found.items():
            # The same message can be in two folders, such as mail sent to oneself.
            unique.setdefault(message["message_id"] or f"{key}", message)
        return sorted(unique.values(), key=_date_key)[:limit]

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
            if _flag_names(flags) & {
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

    @staticmethod
    def _destination(imap: IMAPClient, folder: str, listing: Any = None) -> str:
        """Check that mail may be moved into an existing folder; return its server name.

        Moves never create folders and never target Trash, Spam, Drafts, Sent,
        virtual folders, or folders nested inside Trash, Spam, Drafts or Sent.
        IMAP folder names are case-sensitive (except INBOX), so the folder is
        matched exactly first, then case-insensitively when that is unambiguous.
        listing is a LIST result the caller already has, to avoid a second LIST.
        """
        folder = folder.strip()
        if not folder:
            raise MailValueError("folder cannot be empty")
        folders = {
            str(name): (
                _flag_names(flags),
                delimiter.decode() if isinstance(delimiter, bytes) else delimiter,
            )
            for flags, delimiter, name in (imap.list_folders() if listing is None else listing)
        }
        matching = [folder] if folder in folders else []
        if not matching:
            matching = [name for name in folders if name.casefold() == folder.casefold()]
        if not matching:
            raise MailLookupError(f"Folder {folder!r} not found; moves do not create folders")
        if len(matching) > 1:
            raise MailValueError(
                f"Folder {folder!r} is ambiguous: several folders differ only by case. "
                "Give the name exactly as list_folders shows it"
            )
        name = matching[0]
        flags, delimiter = folders[name]
        unsafe = MailValueError(
            "Messages cannot be moved to Trash, Spam, Drafts, Sent, virtual folders "
            "or folders inside them"
        )
        if flags & FORBIDDEN_DESTINATION_FLAGS:
            raise unsafe
        components = name.split(delimiter) if delimiter else [name]
        for depth in range(1, len(components) + 1):
            ancestor = (delimiter or "").join(components[:depth])
            ancestor_flags = folders.get(ancestor, (set(), None))[0]
            if ancestor_flags & SYSTEM_FOLDER_FLAGS or (
                ancestor.casefold() in RESERVED_FOLDER_NAMES - {"inbox"}
            ):
                raise unsafe
        if not imap.has_capability("MOVE"):
            raise MailError("IMAP server does not support MOVE")
        return name

    def _archive_destination(self, imap: IMAPClient) -> str:
        listing = imap.list_folders()
        if self.config.archive_folder:
            return self._destination(imap, self.config.archive_folder, listing)
        for flags, _, name in listing:
            if "\\archive" in _flag_names(flags):
                return self._destination(imap, str(name), listing)
        raise MailLookupError(
            "No archive folder found: the server marks no folder \\Archive. "
            "Set YANDEX_ARCHIVE_FOLDER to an existing folder"
        )

    def _require_write(self) -> None:
        if not self.config.allow_write:
            raise MailPermissionError("Write operations are disabled. Set YANDEX_ALLOW_WRITE=true")

    @staticmethod
    def _select_message(imap: IMAPClient, uid: int, folder: str) -> None:
        imap.select_folder(folder, readonly=False)
        if uid not in imap.search(["UID", str(uid)]):
            raise MailLookupError(f"Message UID {uid} not found in {folder}")

    def preview_move(
        self, uid: int, folder: str | None = None, source_folder: str = "INBOX"
    ) -> tuple[MailSummary, str]:
        """Validate a move and read the message headers for the confirmation request.

        folder=None means the archive folder. Returns the headers and the
        destination as the server spells it.
        """
        valid_uid(uid)
        with self.connect() as imap:
            destination = (
                self._archive_destination(imap)
                if folder is None
                else self._destination(imap, folder)
            )
            if destination.casefold() == source_folder.strip().casefold():
                raise MailValueError("The message is already in that folder")
            return self._headers(imap, uid, source_folder), destination

    def move(
        self, uid: int, folder: str | None = None, source_folder: str = "INBOX"
    ) -> dict[str, Any]:
        """Move one message; folder=None means the archive folder."""
        valid_uid(uid)
        self._require_write()
        with self.connect() as imap:
            destination = (
                self._archive_destination(imap)
                if folder is None
                else self._destination(imap, folder)
            )
            if destination.casefold() == source_folder.strip().casefold():
                raise MailValueError("The message is already in that folder")
            self._select_message(imap, uid, source_folder)
            imap.move([uid], destination)
        return {
            "uid": uid,
            "folder": destination,
            "action": "move" if folder is not None else "archive",
            "reversible": True,
        }

    def preview_flag(self, uid: int, folder: str = "INBOX") -> MailSummary:
        valid_uid(uid)
        with self.connect() as imap:
            return self._headers(imap, uid, folder)

    def set_flag(self, uid: int, flag: str, value: bool, folder: str = "INBOX") -> dict[str, Any]:
        """Add or remove \\Seen or \\Flagged on one message."""
        valid_uid(uid)
        if flag not in {"\\Seen", "\\Flagged"}:
            raise MailValueError("Only the \\Seen and \\Flagged flags can be changed")
        self._require_write()
        with self.connect() as imap:
            self._select_message(imap, uid, folder)
            if value:
                imap.add_flags([uid], [flag])
            else:
                imap.remove_flags([uid], [flag])
        names = {"\\Seen": ("mark_read", "mark_unread"), "\\Flagged": ("flag", "unflag")}
        return {
            "uid": uid,
            "folder": folder,
            "action": names[flag][0 if value else 1],
            "reversible": True,
        }

    @staticmethod
    def _drafts_folder(imap: IMAPClient) -> str:
        for flags, _, name in imap.list_folders():
            if "\\drafts" in _flag_names(flags):
                return str(name)
        raise MailError("No drafts folder found: the server marks no folder \\Drafts")

    def prepare_draft(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        in_reply_to_uid: int | None = None,
        reply_folder: str = "INBOX",
    ) -> tuple[MailSummary | None, Draft]:
        """Build a plain-text draft and find the drafts folder, without saving.

        For a reply, also returns the original message's headers, and fills in
        In-Reply-To, References and, when empty, the recipient and "Re:" subject.
        """
        recipients = [_address("to", value) for value in to]
        copies = [_address("cc", value) for value in cc or []]
        if len(body.encode()) > MAX_DRAFT_BODY:
            raise MailValueError("body exceeds the 100 KiB draft limit")
        if "\r" in subject or "\n" in subject:
            raise MailValueError("subject must be a single line")
        original: MailSummary | None = None
        reply: Message | None = None
        with self.connect() as imap:
            folder = self._drafts_folder(imap)
            if in_reply_to_uid is not None:
                valid_uid(in_reply_to_uid, "in_reply_to_uid")
                imap.select_folder(reply_folder, readonly=True)
                item = imap.fetch([in_reply_to_uid], ["BODY.PEEK[HEADER]"]).get(in_reply_to_uid, {})
                raw = item.get(b"BODY[HEADER]")
                if not isinstance(raw, bytes):
                    raise MailLookupError(
                        f"Message UID {in_reply_to_uid} not found in {reply_folder}"
                    )
                original = parse_message(raw, in_reply_to_uid, max_body_chars=0)
                reply = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
        message = EmailMessage(policy=policy.SMTP)
        message["From"] = self.config.email
        if reply is not None and original is not None:
            if not recipients:
                answer_to = str(reply.get("Reply-To") or reply.get("From") or "")
                recipients = [
                    formataddr(pair) for pair in getaddresses([answer_to]) if "@" in pair[1]
                ]
            if not subject.strip():
                subject = re.sub(r"\s+", " ", original["subject"]).strip()
                if not re.match(r"(?i)re\s*:", subject):
                    subject = f"Re: {subject}"
            if original["message_id"]:
                message["In-Reply-To"] = original["message_id"]
                references = original["references"] or _MESSAGE_ID.findall(original["in_reply_to"])
                message["References"] = " ".join([*references, original["message_id"]])
        if not recipients:
            raise MailValueError("to must list at least one address")
        message["To"] = ", ".join(recipients)
        if copies:
            message["Cc"] = ", ".join(copies)
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain=self.config.email.rpartition("@")[2] or None)
        message.set_content(body, charset="utf-8", cte="quoted-printable")
        return original, Draft(folder=folder, message=message)

    def save_draft(self, draft: Draft) -> dict[str, Any]:
        """APPEND a prepared draft with the \\Draft flag; nothing is sent."""
        self._require_write()
        with self.connect() as imap:
            response = imap.append(
                draft.folder,
                draft.message.as_bytes(),
                flags=[b"\\Draft"],
                msg_time=datetime.now(UTC),
            )
        text = response.decode(errors="replace") if isinstance(response, bytes) else str(response)
        match = re.search(r"APPENDUID \d+ (\d+)", text)
        return {
            "folder": draft.folder,
            # Only servers with UIDPLUS report the new message's UID.
            "uid": int(match.group(1)) if match else None,
            "message_id": str(draft.message["Message-ID"]),
        }

    def move_to_promotions(self, uid: int, confirm: bool = False) -> dict[str, Any]:
        valid_uid(uid)
        self._require_write()
        if confirm is not True:
            raise MailPermissionError("Explicit confirm=true is required to move a message")
        self._promotions_folder_name()
        with self.connect() as imap:
            folder, exists = self.validate_promotions_folder(imap)
            if not exists:
                imap.create_folder(folder)
            self._select_message(imap, uid, "INBOX")
            imap.move([uid], folder)
        return {"uid": uid, "moved_to": folder, "deleted_permanently": False}
