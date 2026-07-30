#!/usr/bin/env python3
"""Search a local Gmail archive without exposing bodies by default."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_json(value: str | None) -> Any:
    try:
        return json.loads(value or "[]")
    except json.JSONDecodeError:
        return []


def markdown(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def timestamp_value(sent_at: str | None, internal_date: str | None = None) -> float | None:
    value = sent_at or internal_date
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(value, "%d-%b-%Y %H:%M:%S %z")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search a collect_gmail.py archive")
    parser.add_argument("--archive-dir", type=Path, required=True)
    parser.add_argument("--query", help="Case-insensitive subject or body text search")
    parser.add_argument("--from-email", help="Exact sender email")
    parser.add_argument("--after", help="Only messages on or after this ISO date/time")
    parser.add_argument("--before", help="Only messages before this ISO date/time")
    parser.add_argument("--mailbox", help="Mailbox name contained in the source mailbox list")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--include-body", action="store_true", help="Include full text and HTML bodies")
    parser.add_argument("--format", choices=("jsonl", "markdown"), default="markdown")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.limit < 1 or args.limit > 1000:
        raise SystemExit("--limit must be between 1 and 1000")
    database_path = args.archive_dir.expanduser().resolve() / "archive.sqlite3"
    if not database_path.exists():
        raise SystemExit(f"Archive database not found: {database_path}")

    conditions: list[str] = []
    parameters: list[Any] = []
    if args.query:
        conditions.append("(subject LIKE ? COLLATE NOCASE OR text_body LIKE ? COLLATE NOCASE OR html_body LIKE ? COLLATE NOCASE)")
        pattern = f"%{args.query}%"
        parameters.extend((pattern, pattern, pattern))
    if args.from_email:
        conditions.append("from_email = ? COLLATE NOCASE")
        parameters.append(args.from_email.strip())
    if args.after:
        after = timestamp_value(args.after)
        if after is None:
            raise SystemExit("--after must be a valid ISO date/time")
        conditions.append("MESSAGE_EPOCH(sent_at, internal_date) >= ?")
        parameters.append(after)
    if args.before:
        before = timestamp_value(args.before)
        if before is None:
            raise SystemExit("--before must be a valid ISO date/time")
        conditions.append("MESSAGE_EPOCH(sent_at, internal_date) < ?")
        parameters.append(before)
    if args.mailbox:
        conditions.append("source_mailboxes_json LIKE ?")
        parameters.append(f'%"{args.mailbox}"%')

    body_columns = ", text_body, html_body" if args.include_body else ""
    query = f"""
        SELECT archive_id, sent_at, internal_date, subject, from_name, from_email,
               to_json, cc_json, source_mailboxes_json, raw_path, attachments_json
               {body_columns}
        FROM messages
        {"WHERE " + " AND ".join(conditions) if conditions else ""}
        ORDER BY MESSAGE_EPOCH(sent_at, internal_date) DESC, archive_id DESC
        LIMIT ?
    """
    parameters.append(args.limit)

    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.create_function("MESSAGE_EPOCH", 2, timestamp_value)
    connection.row_factory = sqlite3.Row
    try:
        records = []
        for row in connection.execute(query, parameters):
            record = dict(row)
            record["to"] = parse_json(record.pop("to_json"))
            record["cc"] = parse_json(record.pop("cc_json"))
            record["source_mailboxes"] = parse_json(record.pop("source_mailboxes_json"))
            record["attachments"] = parse_json(record.pop("attachments_json"))
            records.append(record)
    finally:
        connection.close()

    if args.format == "jsonl":
        for record in records:
            print(json.dumps(record, ensure_ascii=False))
    else:
        print(f"# 邮件检索结果（{len(records)}）\n")
        print("| 时间 | 发件人 | 主题 | 邮箱 | 附件 |")
        print("|---|---|---|---|---:|")
        for record in records:
            print(
                f"| {markdown(record.get('sent_at') or record.get('internal_date'))} | "
                f"{markdown(record.get('from_email'))} | {markdown(record.get('subject'))} | "
                f"{markdown(', '.join(record.get('source_mailboxes', [])))} | "
                f"{len(record.get('attachments', []))} |"
            )
        if args.include_body:
            for record in records:
                print(f"\n## {record.get('subject') or '(no subject)'}\n")
                print(record.get("text_body") or record.get("html_body") or "(empty body)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
