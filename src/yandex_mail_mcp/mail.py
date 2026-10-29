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
from datetime import UTC, date, datetime
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
        "in_reply_to": str,
        "references": list[str],
    },
)


class ThreadMessage(MailSummary):
    """A message found by get_thread, which can come from several folders."""

    folder: str


_MESSAGE_ID = re.compile(r"<[^<>\s]+>")


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
        if self._closed:
            _close(imap)
            return
        if _selected(imap):
            # CLOSE would expunge messages marked deleted, so only UNSELECT is safe.
            if not imap.has_capability("UNSELECT"):
                _close(imap)
                return
            try:
                imap.unselect_folder()
            except (imaplib.IMAP4.error, OSError):
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
                and _text_sections(structures[uid][b"BODYSTRUCTURE"])[1] == has_attachments
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
                special = {
                    str(flag.decode() if isinstance(flag, bytes) else flag) for flag in flags
                }
                if (name.upper() == "INBOX" or "\\Sent" in special) and all(
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
                for name in folders:
                    imap.select_folder(name, readonly=True)
                    uids: set[int] = set()
                    for start in range(0, len(pending), 10):
                        uids.update(imap.search(_thread_criteria(pending[start : start + 10])))
                    new = sorted(u for u in uids if (name, u) not in found)
                    for message in self._fetch(imap, new, max_body_chars=2000):
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
