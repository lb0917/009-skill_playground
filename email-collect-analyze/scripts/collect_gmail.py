#!/usr/bin/env python3
"""Read-only, incremental Gmail IMAP archiver.

The archive keeps one raw EML file per Gmail message and a SQLite index for
incremental collection and downstream analysis. Passwords are accepted only
through a hidden prompt or an environment variable.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import getpass
import hashlib
import imaplib
import json
import os
import re
import socket
import sqlite3
import ssl
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Iterable, Sequence


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    archive_id TEXT PRIMARY KEY,
    account TEXT NOT NULL,
    gmail_msgid TEXT,
    thread_key TEXT NOT NULL,
    message_id TEXT,
    sent_at TEXT,
    internal_date TEXT,
    subject TEXT NOT NULL,
    from_name TEXT,
    from_email TEXT,
    to_json TEXT NOT NULL,
    cc_json TEXT NOT NULL,
    bcc_json TEXT NOT NULL,
    flags_json TEXT NOT NULL,
    source_mailboxes_json TEXT NOT NULL,
    raw_path TEXT NOT NULL,
    text_body TEXT NOT NULL,
    html_body TEXT NOT NULL,
    attachments_json TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_account_date
    ON messages(account, sent_at);
CREATE INDEX IF NOT EXISTS idx_messages_thread
    ON messages(account, thread_key);
CREATE INDEX IF NOT EXISTS idx_messages_from
    ON messages(account, from_email);

CREATE TABLE IF NOT EXISTS mailbox_membership (
    account TEXT NOT NULL,
    mailbox TEXT NOT NULL,
    uid INTEGER NOT NULL,
    archive_id TEXT NOT NULL,
    flags_json TEXT NOT NULL,
    internal_date TEXT,
    is_current INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT,
    last_seen_at TEXT,
    removed_at TEXT,
    last_seen_run_id TEXT,
    PRIMARY KEY (account, mailbox, uid)
);

CREATE TABLE IF NOT EXISTS mailbox_state (
    account TEXT NOT NULL,
    mailbox TEXT NOT NULL,
    uidvalidity TEXT,
    last_uid INTEGER NOT NULL DEFAULT 0,
    special_use_flags_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account, mailbox)
);
"""

RETRYABLE_ERRORS = (
    imaplib.IMAP4.abort,
    imaplib.IMAP4.error,
    OSError,
    socket.timeout,
    ssl.SSLError,
)

DEFAULT_ARCHIVE_ROOT = Path("/Volumes/Lenovo/develper_mirror/email-archives")


@dataclass(frozen=True)
class Mailbox:
    raw_name: str
    display_name: str
    flags: tuple[str, ...]


@dataclass(frozen=True)
class FetchItem:
    uid: int
    gmail_msgid: str | None
    gmail_thrid: str | None
    flags: tuple[str, ...]
    internal_date: str | None
    raw_message: bytes


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(str(value)))).strip()
    except (LookupError, TypeError, UnicodeError, ValueError):
        return str(value).strip()


def decode_mailbox_name(raw_name: str) -> str:
    name = raw_name.strip()
    if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
        name = name[1:-1].replace(r'\"', '"').replace("\\\\", "\\")

    def decode_segment(match: re.Match[str]) -> str:
        encoded = match.group(1)
        if not encoded:
            return "&"
        value = encoded.replace(",", "/")
        value += "=" * ((4 - len(value) % 4) % 4)
        try:
            return base64.b64decode(value).decode("utf-16-be")
        except (UnicodeDecodeError, ValueError):
            return match.group(0)

    return re.sub(r"&([^-]*)-", decode_segment, name)


def parse_mailbox_list_line(line: bytes) -> Mailbox | None:
    text = line.decode("utf-8", errors="replace")
    match = re.match(r"^\((?P<flags>[^)]*)\)\s+(?:NIL|\"(?:\\.|[^\"])*\")\s+(?P<name>.+)$", text)
    if not match:
        return None
    flags = tuple(match.group("flags").split())
    raw_name = match.group("name").strip()
    return Mailbox(raw_name, decode_mailbox_name(raw_name), flags)


def normalize_email(value: str) -> str:
    return value.strip().lower()


def account_directory_name(account: str) -> str:
    """Return a stable, filesystem-safe directory name for one mailbox account."""
    return re.sub(r"[^a-z0-9]+", "_", normalize_email(account)).strip("_")


def parse_addresses(values: Sequence[str | None]) -> list[dict[str, str]]:
    decoded = [decode_header_value(value) for value in values if value]
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for name, address in getaddresses(decoded):
        clean_address = normalize_email(address)
        if not clean_address:
            continue
        clean_name = decode_header_value(name)
        key = (clean_name, clean_address)
        if key not in seen:
            result.append({"name": clean_name, "email": clean_address})
            seen.add(key)
    return result


def decode_part(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        value = part.get_payload()
        return value if isinstance(value, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def extract_bodies_and_attachments(message) -> tuple[str, str, list[dict[str, object]]]:
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict[str, object]] = []

    parts: Iterable = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        disposition = (part.get_content_disposition() or "").lower()
        filename = decode_header_value(part.get_filename())
        payload = part.get_payload(decode=True)
        size = len(payload) if payload is not None else 0
        is_attachment = disposition == "attachment" or bool(filename)

        if is_attachment:
            attachments.append(
                {
                    "filename": filename,
                    "content_type": content_type,
                    "content_id": (part.get("Content-ID") or "").strip("<>"),
                    "disposition": disposition or "inline",
                    "size_bytes": size,
                }
            )
            continue
        if content_type == "text/plain":
            text_parts.append(decode_part(part))
        elif content_type == "text/html":
            html_parts.append(decode_part(part))

    return "\n\n".join(text_parts), "\n\n".join(html_parts), attachments


def parse_sent_at(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def parse_internal_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d-%b-%Y %H:%M:%S %z")
    except ValueError:
        return None


def normalized_subject(subject: str) -> str:
    value = re.sub(r"^(?:(?:re|fw|fwd)\s*:\s*)+", "", subject, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip().lower()


def build_thread_key(message, gmail_thrid: str | None, subject: str) -> str:
    if gmail_thrid:
        return f"gmail:{gmail_thrid}"
    references = (message.get("References") or "").split()
    if references:
        return f"reference:{references[0].strip().lower()}"
    fallback = normalized_subject(subject) or "(no subject)"
    return "subject:" + hashlib.sha256(fallback.encode("utf-8")).hexdigest()[:24]


def parse_fetch_items(data: list[object]) -> list[FetchItem]:
    items: list[FetchItem] = []
    for entry in data:
        if not isinstance(entry, tuple) or len(entry) < 2:
            continue
        metadata, raw_message = entry[0], entry[1]
        if not isinstance(metadata, bytes) or not isinstance(raw_message, bytes):
            continue
        text = metadata.decode("ascii", errors="replace")
        uid_match = re.search(r"\bUID\s+(\d+)", text)
        if not uid_match:
            continue
        msgid_match = re.search(r"\bX-GM-MSGID\s+(\d+)", text)
        thrid_match = re.search(r"\bX-GM-THRID\s+(\d+)", text)
        flags_match = re.search(r"\bFLAGS\s+\(([^)]*)\)", text)
        date_match = re.search(r'\bINTERNALDATE\s+"([^"]+)"', text)
        items.append(
            FetchItem(
                uid=int(uid_match.group(1)),
                gmail_msgid=msgid_match.group(1) if msgid_match else None,
                gmail_thrid=thrid_match.group(1) if thrid_match else None,
                flags=tuple(flags_match.group(1).split()) if flags_match else (),
                internal_date=date_match.group(1) if date_match else None,
                raw_message=raw_message,
            )
        )
    return items


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def init_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    migrations = {
        "mailbox_membership": {
            "is_current": "INTEGER NOT NULL DEFAULT 1",
            "first_seen_at": "TEXT",
            "last_seen_at": "TEXT",
            "removed_at": "TEXT",
            "last_seen_run_id": "TEXT",
        },
        "mailbox_state": {
            "special_use_flags_json": "TEXT NOT NULL DEFAULT '[]'",
        },
    }
    for table, columns in migrations.items():
        existing = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, definition in columns.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    now = utc_now()
    connection.execute(
        "UPDATE mailbox_membership SET first_seen_at = COALESCE(first_seen_at, ?), "
        "last_seen_at = COALESCE(last_seen_at, ?)",
        (now, now),
    )
    connection.commit()
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return connection


def batches(values: Sequence[int], size: int) -> Iterable[Sequence[int]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


class GmailConnection:
    def __init__(self, email_address: str, password: str, host: str, port: int, timeout: int, retries: int):
        self.email_address = email_address
        self.password = password
        self.host = host
        self.port = port
        self.timeout = timeout
        self.retries = retries
        self.client: imaplib.IMAP4_SSL | None = None
        self.current_mailbox: str | None = None

    def connect(self) -> None:
        self.close()
        context = ssl.create_default_context()
        self.client = imaplib.IMAP4_SSL(
            self.host,
            self.port,
            ssl_context=context,
            timeout=self.timeout,
        )
        status, _ = self.client.login(self.email_address, self.password)
        if status != "OK":
            raise RuntimeError("Gmail login failed")

    def close(self) -> None:
        if self.client is not None:
            try:
                self.client.logout()
            except Exception:
                pass
        self.client = None

    def reconnect(self) -> None:
        mailbox = self.current_mailbox
        self.connect()
        if mailbox:
            self.select(mailbox)

    def list_mailboxes(self) -> list[Mailbox]:
        assert self.client is not None
        status, data = self.client.list()
        if status != "OK":
            raise RuntimeError("Unable to list Gmail mailboxes")
        return [mailbox for item in data if item and (mailbox := parse_mailbox_list_line(item))]

    def select(self, raw_name: str) -> tuple[int, str | None]:
        assert self.client is not None
        status, data = self.client.select(raw_name, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Unable to open mailbox {decode_mailbox_name(raw_name)!r}")
        self.current_mailbox = raw_name
        count = int(data[0]) if data and data[0] else 0
        response = self.client.response("UIDVALIDITY")
        uidvalidity = None
        if response and len(response) > 1 and response[1] and response[1][0]:
            raw_value = response[1][0]
            uidvalidity = raw_value.decode("ascii", errors="replace") if isinstance(raw_value, bytes) else str(raw_value)
        return count, uidvalidity

    def search(self, criteria: list[str]) -> list[int]:
        for attempt in range(self.retries + 1):
            try:
                assert self.client is not None
                status, data = self.client.uid("SEARCH", None, *criteria)
                if status != "OK":
                    raise RuntimeError("Gmail UID search failed")
                if not data or not data[0]:
                    return []
                return [int(value) for value in data[0].split()]
            except RETRYABLE_ERRORS:
                if attempt >= self.retries:
                    raise
                time.sleep(min(2**attempt, 8))
                self.reconnect()
        return []

    def fetch(self, uids: Sequence[int]) -> list[FetchItem]:
        uid_set = ",".join(str(uid) for uid in uids)
        query = "(UID X-GM-MSGID X-GM-THRID FLAGS INTERNALDATE BODY.PEEK[])"
        for attempt in range(self.retries + 1):
            try:
                assert self.client is not None
                status, data = self.client.uid("FETCH", uid_set, query)
                if status != "OK":
                    raise RuntimeError(f"Gmail fetch failed for UIDs {uid_set}")
                return parse_fetch_items(data)
            except RETRYABLE_ERRORS:
                if attempt >= self.retries:
                    raise
                time.sleep(min(2**attempt, 8))
                self.reconnect()
        return []


def choose_mailboxes(available: Sequence[Mailbox], requested: Sequence[str]) -> list[Mailbox]:
    selectable = [box for box in available if not any(flag.lower() == r"\noselect" for flag in box.flags)]
    if not requested or requested == ["auto"]:
        all_mail = [box for box in selectable if any(flag.lower() == r"\all" for flag in box.flags)]
        if all_mail:
            return all_mail[:1]
        fallback = [
            box
            for box in selectable
            if box.display_name.upper() == "INBOX" or any(flag.lower() == r"\sent" for flag in box.flags)
        ]
        return fallback or selectable[:1]
    if any(item.lower() == "all" for item in requested):
        return selectable

    chosen: list[Mailbox] = []
    for wanted in requested:
        matches = [box for box in selectable if box.display_name.casefold() == wanted.casefold()]
        if not matches:
            raise ValueError(f"Mailbox not found: {wanted}")
        if matches[0] not in chosen:
            chosen.append(matches[0])
    return chosen


def imap_since(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d")
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return f"{parsed.day:02d}-{months[parsed.month - 1]}-{parsed.year:04d}"


def archive_fetch_item(
    connection: sqlite3.Connection,
    archive_root: Path,
    account: str,
    mailbox: Mailbox,
    item: FetchItem,
    run_id: str | None = None,
) -> bool:
    sha256 = hashlib.sha256(item.raw_message).hexdigest()
    message = BytesParser(policy=policy.default).parsebytes(item.raw_message)
    subject = decode_header_value(message.get("Subject"))
    message_id = decode_header_value(message.get("Message-ID")).strip().lower()
    archive_id = (
        f"gmail:{item.gmail_msgid}"
        if item.gmail_msgid
        else f"message-id:{message_id}"
        if message_id
        else f"sha256:{sha256}"
    )
    existing = connection.execute(
        "SELECT source_mailboxes_json, raw_path FROM messages WHERE archive_id = ?",
        (archive_id,),
    ).fetchone()

    sent_at = parse_sent_at(message.get("Date"))
    internal_datetime = parse_internal_date(item.internal_date)
    path_datetime = None
    if sent_at:
        try:
            path_datetime = datetime.fromisoformat(sent_at)
        except ValueError:
            path_datetime = None
    path_datetime = path_datetime or internal_datetime or datetime.now(timezone.utc)
    file_key = hashlib.sha256(archive_id.encode("utf-8")).hexdigest()[:32]
    raw_path = Path("raw") / f"{path_datetime.year:04d}" / f"{path_datetime.month:02d}" / f"{file_key}.eml"
    if not (archive_root / raw_path).exists():
        atomic_write_bytes(archive_root / raw_path, item.raw_message)

    from_addresses = parse_addresses(message.get_all("From", []))
    from_address = from_addresses[0] if from_addresses else {"name": "", "email": ""}
    to_addresses = parse_addresses(message.get_all("To", []))
    cc_addresses = parse_addresses(message.get_all("Cc", []))
    bcc_addresses = parse_addresses(message.get_all("Bcc", []))
    text_body, html_body, attachments = extract_bodies_and_attachments(message)
    thread_key = build_thread_key(message, item.gmail_thrid, subject)
    source_mailboxes = set(json.loads(existing["source_mailboxes_json"])) if existing else set()
    source_mailboxes.add(mailbox.display_name)
    now = utc_now()

    values = (
        archive_id,
        account,
        item.gmail_msgid,
        thread_key,
        message_id,
        sent_at,
        item.internal_date,
        subject,
        from_address["name"],
        from_address["email"],
        json.dumps(to_addresses, ensure_ascii=False),
        json.dumps(cc_addresses, ensure_ascii=False),
        json.dumps(bcc_addresses, ensure_ascii=False),
        json.dumps(item.flags, ensure_ascii=False),
        json.dumps(sorted(source_mailboxes), ensure_ascii=False),
        str(raw_path),
        text_body,
        html_body,
        json.dumps(attachments, ensure_ascii=False),
        len(item.raw_message),
        sha256,
        now,
        now,
    )
    connection.execute(
        """
        INSERT INTO messages (
            archive_id, account, gmail_msgid, thread_key, message_id, sent_at,
            internal_date, subject, from_name, from_email, to_json, cc_json,
            bcc_json, flags_json, source_mailboxes_json, raw_path, text_body,
            html_body, attachments_json, size_bytes, sha256, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(archive_id) DO UPDATE SET
            thread_key = excluded.thread_key,
            sent_at = COALESCE(excluded.sent_at, messages.sent_at),
            internal_date = COALESCE(excluded.internal_date, messages.internal_date),
            subject = excluded.subject,
            from_name = excluded.from_name,
            from_email = excluded.from_email,
            to_json = excluded.to_json,
            cc_json = excluded.cc_json,
            bcc_json = excluded.bcc_json,
            flags_json = excluded.flags_json,
            source_mailboxes_json = excluded.source_mailboxes_json,
            raw_path = excluded.raw_path,
            text_body = excluded.text_body,
            html_body = excluded.html_body,
            attachments_json = excluded.attachments_json,
            size_bytes = excluded.size_bytes,
            sha256 = excluded.sha256,
            updated_at = excluded.updated_at
        """,
        values,
    )
    connection.execute(
        """
        INSERT INTO mailbox_membership
            (account, mailbox, uid, archive_id, flags_json, internal_date,
             is_current, first_seen_at, last_seen_at, removed_at, last_seen_run_id)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, NULL, ?)
        ON CONFLICT(account, mailbox, uid) DO UPDATE SET
            archive_id = excluded.archive_id,
            flags_json = excluded.flags_json,
            internal_date = excluded.internal_date,
            is_current = 1,
            last_seen_at = excluded.last_seen_at,
            removed_at = NULL,
            last_seen_run_id = excluded.last_seen_run_id
        """,
        (
            account,
            mailbox.display_name,
            item.uid,
            archive_id,
            json.dumps(item.flags, ensure_ascii=False),
            item.internal_date,
            now,
            now,
            run_id,
        ),
    )
    return existing is None


def reconcile_mailbox_memberships(
    connection: sqlite3.Connection,
    account: str,
    mailbox: str,
    current_uids: set[int],
) -> int:
    """Mark memberships absent from a completed full scan as historical."""
    active_rows = connection.execute(
        "SELECT uid, archive_id FROM mailbox_membership "
        "WHERE account = ? AND mailbox = ? AND is_current = 1",
        (account, mailbox),
    ).fetchall()
    stale_rows = [row for row in active_rows if int(row["uid"]) not in current_uids]
    if not stale_rows:
        return 0
    now = utc_now()
    connection.executemany(
        "UPDATE mailbox_membership SET is_current = 0, removed_at = ? "
        "WHERE account = ? AND mailbox = ? AND uid = ?",
        ((now, account, mailbox, int(row["uid"])) for row in stale_rows),
    )
    affected_ids = {row["archive_id"] for row in stale_rows}
    for archive_id in affected_ids:
        current_mailboxes = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT mailbox FROM mailbox_membership "
                "WHERE archive_id = ? AND is_current = 1 ORDER BY mailbox",
                (archive_id,),
            ).fetchall()
        ]
        connection.execute(
            "UPDATE messages SET source_mailboxes_json = ?, updated_at = ? WHERE archive_id = ?",
            (json.dumps(current_mailboxes, ensure_ascii=False), now, archive_id),
        )
    return len(stale_rows)


def collect_mailbox(
    gmail: GmailConnection,
    database: sqlite3.Connection,
    archive_root: Path,
    account: str,
    mailbox: Mailbox,
    since: str | None,
    limit: int | None,
    batch_size: int,
    reset_state: bool,
    run_id: str | None = None,
) -> dict[str, object]:
    message_count, uidvalidity = gmail.select(mailbox.raw_name)
    state = database.execute(
        "SELECT uidvalidity, last_uid FROM mailbox_state WHERE account = ? AND mailbox = ?",
        (account, mailbox.display_name),
    ).fetchone()
    last_uid = int(state["last_uid"]) if state else 0
    if reset_state or (state and state["uidvalidity"] and state["uidvalidity"] != uidvalidity):
        last_uid = 0

    criteria: list[str]
    if since:
        criteria = ["SINCE", imap_since(since)]
        minimum_uid = 0
        scan_mode = "bounded_since"
    elif last_uid:
        criteria = ["UID", f"{last_uid + 1}:*"]
        minimum_uid = last_uid
        scan_mode = "incremental"
    else:
        criteria = ["ALL"]
        minimum_uid = 0
        scan_mode = "full"

    uids = sorted(uid for uid in gmail.search(criteria) if uid > minimum_uid)
    if limit is not None:
        uids = uids[:limit]
    print(
        json.dumps(
            {
                "event": "mailbox_started",
                "mailbox": mailbox.display_name,
                "server_message_count": message_count,
                "candidate_uids": len(uids),
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
        flush=True,
    )

    new_count = 0
    duplicate_count = 0
    processed = 0
    last_processed_uid = last_uid
    for uid_batch in batches(uids, batch_size):
        fetched = gmail.fetch(uid_batch)
        fetched_uids = {item.uid for item in fetched}
        missing = sorted(set(uid_batch) - fetched_uids)
        if missing:
            raise RuntimeError(f"Gmail returned no message data for UIDs: {missing[:10]}")
        for item in fetched:
            if archive_fetch_item(database, archive_root, account, mailbox, item, run_id):
                new_count += 1
            else:
                duplicate_count += 1
            processed += 1
            last_processed_uid = max(last_processed_uid, item.uid)
        state_uid = last_processed_uid if not since else last_uid
        database.execute(
            """
            INSERT INTO mailbox_state
                (account, mailbox, uidvalidity, last_uid, special_use_flags_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(account, mailbox) DO UPDATE SET
                uidvalidity = excluded.uidvalidity,
                last_uid = excluded.last_uid,
                special_use_flags_json = excluded.special_use_flags_json,
                updated_at = excluded.updated_at
            """,
            (
                account,
                mailbox.display_name,
                uidvalidity,
                state_uid,
                json.dumps(mailbox.flags, ensure_ascii=False),
                utc_now(),
            ),
        )
        database.commit()
        print(
            json.dumps(
                {
                    "event": "batch_completed",
                    "mailbox": mailbox.display_name,
                    "processed": processed,
                    "candidate_uids": len(uids),
                    "new_messages": new_count,
                    "duplicates": duplicate_count,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )

    if not uids:
        database.execute(
            """
            INSERT INTO mailbox_state
                (account, mailbox, uidvalidity, last_uid, special_use_flags_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(account, mailbox) DO UPDATE SET
                uidvalidity = excluded.uidvalidity,
                special_use_flags_json = excluded.special_use_flags_json,
                updated_at = excluded.updated_at
            """,
            (
                account,
                mailbox.display_name,
                uidvalidity,
                last_uid,
                json.dumps(mailbox.flags, ensure_ascii=False),
                utc_now(),
            ),
        )
        database.commit()

    removed_memberships = 0
    if scan_mode == "full" and limit is None:
        removed_memberships = reconcile_mailbox_memberships(
            database,
            account,
            mailbox.display_name,
            set(uids),
        )
        database.commit()

    current_membership_count = database.execute(
        "SELECT COUNT(*) FROM mailbox_membership "
        "WHERE account = ? AND mailbox = ? AND is_current = 1",
        (account, mailbox.display_name),
    ).fetchone()[0]

    return {
        "mailbox": mailbox.display_name,
        "scan_mode": scan_mode,
        "server_message_count": message_count,
        "candidate_uids": len(uids),
        "processed": processed,
        "new_messages": new_count,
        "duplicates": duplicate_count,
        "current_membership_count": current_membership_count,
        "removed_memberships": removed_memberships,
        "last_uid": last_processed_uid,
    }


def acquire_collection_lock(archive_root: Path):
    lock_path = archive_root / ".collection.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    os.chmod(lock_path, 0o600)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise RuntimeError(f"Another collection is already using {archive_root}") from error
    return handle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Archive Gmail through read-only IMAP")
    parser.add_argument("--email", required=True, help="Gmail or Google Workspace email address")
    parser.add_argument("--output-dir", type=Path, help="Local archive directory")
    parser.add_argument("--mailbox", action="append", help="Mailbox name, 'auto', or 'all'; repeat as needed")
    parser.add_argument("--since", help="Only collect messages on or after YYYY-MM-DD")
    parser.add_argument("--limit", type=int, help="Maximum messages per mailbox for this run")
    parser.add_argument("--batch-size", type=int, default=100, help="IMAP fetch batch size (default: 100)")
    parser.add_argument("--password-env", default="GMAIL_APP_PASSWORD", help="Environment variable containing the app password")
    parser.add_argument("--no-prompt", action="store_true", help="Fail instead of prompting when password env is unset")
    parser.add_argument("--host", default="imap.gmail.com")
    parser.add_argument("--port", type=int, default=993)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--reset-state", action="store_true", help="Rescan selected mailboxes while retaining deduplication")
    parser.add_argument("--list-mailboxes", action="store_true", help="List available mailboxes without collecting")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    started_at = utc_now()
    account = normalize_email(args.email)
    if not account:
        raise SystemExit("--email cannot be empty")
    if args.since:
        try:
            imap_since(args.since)
        except ValueError as error:
            raise SystemExit("--since must use YYYY-MM-DD") from error
    if args.batch_size < 1 or args.batch_size > 250:
        raise SystemExit("--batch-size must be between 1 and 250")
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be positive")

    requested_mailboxes = args.mailbox or ["auto"]
    expected_scope = (
        "all"
        if any(value.lower() == "all" for value in requested_mailboxes)
        else "normal"
        if requested_mailboxes == ["auto"]
        else "selected"
    )
    coverage_mode = "limited" if args.limit is not None else "bounded" if args.since else "unbounded"
    default_name = account_directory_name(account)
    configured_root = Path(
        os.environ.get("EMAIL_ARCHIVE_ROOT", str(DEFAULT_ARCHIVE_ROOT))
    ).expanduser()
    if args.output_dir is None and not configured_root.parent.is_dir():
        raise SystemExit(
            f"Default archive volume is unavailable: {configured_root.parent}. "
            "Mount the Lenovo volume or pass --output-dir explicitly."
        )
    archive_root = (
        args.output_dir or (configured_root / default_name)
    ).expanduser().resolve()

    password = os.environ.get(args.password_env)
    if not password:
        if args.no_prompt:
            raise SystemExit(f"Set {args.password_env}; no password was read from disk or command arguments")
        password = getpass.getpass(f"Gmail app password for {account}: ")
    if not password:
        raise SystemExit("No Gmail app password provided")
    password = password.replace(" ", "")

    gmail = GmailConnection(account, password, args.host, args.port, args.timeout, args.retries)
    database: sqlite3.Connection | None = None
    lock_handle = None
    run_id = uuid.uuid4().hex
    run_manifest_path: Path | None = None
    run_payload: dict[str, object] | None = None
    results: list[dict[str, object]] = []
    try:
        if not args.list_mailboxes:
            archive_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_handle = acquire_collection_lock(archive_root)
            run_manifest_path = archive_root / "manifests" / "imap_collection_run.json"
            run_payload = {
                "schema_version": 2,
                "run_id": run_id,
                "status": "running",
                "source_mode": "imap_app_password",
                "account": account,
                "archive_dir": str(archive_root),
                "database": str(archive_root / "archive.sqlite3"),
                "started_at": started_at,
                "expected_scope": expected_scope,
                "coverage_mode": coverage_mode,
                "collection_options": {
                    "since": args.since,
                    "limit": args.limit,
                    "reset_state": args.reset_state,
                    "batch_size": args.batch_size,
                },
                "selected_mailboxes": [],
                "mailbox_results": [],
                "total_unique_messages": None,
                "baseline_evidence": False,
                "full_sweep_evidence": False,
                "run_manifest": str(run_manifest_path),
            }
            atomic_write_bytes(
                run_manifest_path,
                (json.dumps(run_payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            )

        gmail.connect()
        available = gmail.list_mailboxes()
        if args.list_mailboxes:
            print(json.dumps({"account": account, "mailboxes": [box.display_name for box in available]}, ensure_ascii=False, indent=2))
            return 0

        selected = choose_mailboxes(available, args.mailbox or ["auto"])
        if not selected:
            raise RuntimeError("No selectable Gmail mailboxes found")

        database_path = archive_root / "archive.sqlite3"
        assert run_payload is not None and run_manifest_path is not None
        run_payload["selected_mailboxes"] = [box.display_name for box in selected]
        atomic_write_bytes(
            run_manifest_path,
            (json.dumps(run_payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        database = init_database(database_path)
        existing_accounts = {
            row[0] for row in database.execute("SELECT DISTINCT account FROM messages").fetchall()
        }
        if existing_accounts and existing_accounts != {account}:
            raise RuntimeError(
                "This archive directory belongs to a different Gmail account; choose a separate --output-dir"
            )

        for mailbox in selected:
            result = collect_mailbox(
                gmail,
                database,
                archive_root,
                account,
                mailbox,
                args.since,
                args.limit,
                args.batch_size,
                args.reset_state,
                run_id,
            )
            results.append(result)
            run_payload["mailbox_results"] = results
            run_payload["total_unique_messages"] = database.execute(
                "SELECT COUNT(*) FROM messages WHERE account = ?", (account,)
            ).fetchone()[0]
            atomic_write_bytes(
                run_manifest_path,
                (json.dumps(run_payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            )

        total_messages = database.execute("SELECT COUNT(*) FROM messages WHERE account = ?", (account,)).fetchone()[0]
        scope_baseline_path = archive_root / "manifests" / "imap_scope_baseline.json"
        full_sweep_manifest_path = archive_root / "manifests" / "imap_full_sweep.json"
        baseline_evidence = (
            coverage_mode == "unbounded"
            and bool(results)
            and all(
                item.get("scan_mode") == "full"
                and int(item.get("processed", -1)) == int(item.get("candidate_uids", -2))
                and int(item.get("candidate_uids", -1)) == int(item.get("server_message_count", -2))
                and int(item.get("current_membership_count", -1)) == int(item.get("server_message_count", -2))
                for item in results
            )
        )
        full_sweep_evidence = baseline_evidence and expected_scope == "all"
        run_payload.update(
            {
                "status": "completed",
                "completed_at": utc_now(),
                "mailbox_results": results,
                "total_unique_messages": total_messages,
                "baseline_evidence": baseline_evidence,
                "full_sweep_evidence": full_sweep_evidence,
                "scope_baseline_manifest": str(scope_baseline_path) if baseline_evidence else None,
                "full_sweep_manifest": str(full_sweep_manifest_path) if full_sweep_evidence else None,
            }
        )
        atomic_write_bytes(
            run_manifest_path,
            (json.dumps(run_payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        if baseline_evidence:
            atomic_write_bytes(
                scope_baseline_path,
                (json.dumps(run_payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            )
        if full_sweep_evidence:
            atomic_write_bytes(
                full_sweep_manifest_path,
                (json.dumps(run_payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            )
        print(json.dumps(run_payload, ensure_ascii=False, indent=2))
        return 0
    except BaseException as error:
        if run_payload is not None and run_manifest_path is not None:
            run_payload["status"] = "failed"
            run_payload["failed_at"] = utc_now()
            run_payload["mailbox_results"] = results
            run_payload["error"] = {
                "type": type(error).__name__,
                "message": str(error)[:500],
            }
            if database is not None:
                try:
                    run_payload["total_unique_messages"] = database.execute(
                        "SELECT COUNT(*) FROM messages WHERE account = ?", (account,)
                    ).fetchone()[0]
                except sqlite3.Error:
                    pass
            try:
                atomic_write_bytes(
                    run_manifest_path,
                    (json.dumps(run_payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                )
            except OSError:
                pass
        raise
    finally:
        if database is not None:
            database.close()
        if lock_handle is not None:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            finally:
                lock_handle.close()
        gmail.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Collection cancelled", file=sys.stderr)
        raise SystemExit(130)
    except (imaplib.IMAP4.error, OSError, RuntimeError, ValueError) as error:
        print(f"Collection failed: {error}", file=sys.stderr)
        raise SystemExit(1)
