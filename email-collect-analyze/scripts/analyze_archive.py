#!/usr/bin/env python3
"""Build durable, deterministic views from a collect_gmail.py archive."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from archive_contract import API_COMPLETE_STATUSES

JSON_COLUMNS = {
    "to_json": "to",
    "cc_json": "cc",
    "bcc_json": "bcc",
    "flags_json": "flags",
    "source_mailboxes_json": "source_mailboxes",
    "attachments_json": "attachments",
}

STOPWORDS = {
    "about", "after", "again", "been", "before", "could", "email", "from",
    "have", "hello", "please", "regarding", "thanks", "thank", "that", "the",
    "this", "with", "would", "your", "re", "fwd", "回复", "邮件", "你好", "谢谢",
    "关于", "我们", "你们", "可以", "合作",
}


@dataclass
class ContactStats:
    email: str
    names: Counter[str] = field(default_factory=Counter)
    message_ids: set[str] = field(default_factory=set)
    thread_keys: set[str] = field(default_factory=set)
    inbound_ids: set[str] = field(default_factory=set)
    outbound_ids: set[str] = field(default_factory=set)
    dates: list[str] = field(default_factory=list)
    subjects: Counter[str] = field(default_factory=Counter)


def json_value(value: str | None) -> Any:
    if not value:
        return []
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return []


def identity_key(value: str | None) -> str:
    """Normalize addresses only where the provider documents mailbox equivalence."""
    address = (value or "").strip().lower()
    local, separator, domain = address.rpartition("@")
    if not separator:
        return address
    if domain in {"gmail.com", "googlemail.com"}:
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def is_identity(value: str | None, identities: set[str]) -> bool:
    key = identity_key(value)
    return bool(key) and key in {identity_key(identity) for identity in identities}


def load_collection_manifest(archive_dir: Path) -> dict[str, Any] | None:
    path = archive_dir / "manifests" / "collection_manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def atomic_text_writer(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    os.chmod(handle.name, 0o600)
    return handle


def finish_atomic(handle, destination: Path) -> None:
    temp_name = handle.name
    handle.flush()
    os.fsync(handle.fileno())
    handle.close()
    os.replace(temp_name, destination)


def abort_atomic(handle) -> None:
    temp_name = handle.name
    try:
        handle.close()
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def row_to_message(row: sqlite3.Row, include_bodies: bool = True) -> dict[str, Any]:
    output = dict(row)
    for database_name, output_name in JSON_COLUMNS.items():
        output[output_name] = json_value(output.pop(database_name, None))
    if not include_bodies:
        output.pop("text_body", None)
        output.pop("html_body", None)
    return output


def external_people(message: dict[str, Any], identities: set[str]) -> list[dict[str, str]]:
    people: list[dict[str, str]] = []
    seen: set[str] = set()
    sender_email = (message.get("from_email") or "").lower()
    if sender_email and not is_identity(sender_email, identities):
        people.append({"name": message.get("from_name") or "", "email": sender_email})
        seen.add(sender_email)
    for recipient_field in ("to", "cc", "bcc"):
        for person in message.get(recipient_field, []):
            email = (person.get("email") or "").lower()
            if email and not is_identity(email, identities) and email not in seen:
                people.append({"name": person.get("name") or "", "email": email})
                seen.add(email)
    return people


def message_time(message: dict[str, Any]) -> str:
    return message.get("sent_at") or message.get("internal_date") or ""


def parse_time_value(value: str | None) -> datetime | None:
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
    return parsed.astimezone(timezone.utc)


def time_sort_key(value: str | None) -> tuple[int, float, str]:
    parsed = parse_time_value(value)
    return (1, parsed.timestamp(), value or "") if parsed else (0, float("-inf"), value or "")


def message_epoch(sent_at: str | None, internal_date: str | None = None) -> float:
    parsed = parse_time_value(sent_at or internal_date)
    return parsed.timestamp() if parsed else float("-inf")


def message_sort_key(message: dict[str, Any]) -> tuple[int, float, str, str]:
    base = time_sort_key(message_time(message))
    return (*base, str(message.get("archive_id") or ""))


def direction(message: dict[str, Any], identities: set[str]) -> str:
    return "outbound" if is_identity(message.get("from_email"), identities) else "inbound"


def safe_csv(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


def markdown_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def top_keywords(subjects: Iterable[str], limit: int = 20) -> list[tuple[str, int]]:
    counts: Counter[str] = Counter()
    for subject in subjects:
        english = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", subject.lower())
        chinese = re.findall(r"[\u4e00-\u9fff]{2,8}", subject)
        counts.update(token for token in english + chinese if token not in STOPWORDS)
    return counts.most_common(limit)


def write_messages_jsonl(
    connection: sqlite3.Connection,
    path: Path,
    run_id: str | None = None,
) -> int:
    handle = atomic_text_writer(path)
    count = 0
    try:
        where = (
            "WHERE EXISTS (SELECT 1 FROM mailbox_membership mm "
            "WHERE mm.archive_id = messages.archive_id AND mm.last_seen_run_id = ?)"
            if run_id
            else ""
        )
        query = (
            f"SELECT * FROM messages {where} "
            "ORDER BY MESSAGE_EPOCH(sent_at, internal_date), archive_id"
        )
        parameters = (run_id,) if run_id else ()
        for row in connection.execute(query, parameters):
            handle.write(json.dumps(row_to_message(row), ensure_ascii=False) + "\n")
            count += 1
        finish_atomic(handle, path)
        return count
    except Exception:
        abort_atomic(handle)
        raise


def write_contacts_csv(contacts: dict[str, ContactStats], path: Path) -> int:
    handle = atomic_text_writer(path)
    fieldnames = [
        "email", "name", "message_count", "inbound_count", "outbound_count",
        "thread_count", "first_seen", "last_seen", "top_subjects",
    ]
    try:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        ordered = sorted(contacts.values(), key=lambda item: (-len(item.message_ids), item.email))
        for contact in ordered:
            dates = sorted((date for date in contact.dates if date), key=time_sort_key)
            writer.writerow(
                {
                    "email": safe_csv(contact.email),
                    "name": safe_csv(contact.names.most_common(1)[0][0] if contact.names else ""),
                    "message_count": len(contact.message_ids),
                    "inbound_count": len(contact.inbound_ids),
                    "outbound_count": len(contact.outbound_ids),
                    "thread_count": len(contact.thread_keys),
                    "first_seen": dates[0] if dates else "",
                    "last_seen": dates[-1] if dates else "",
                    "top_subjects": safe_csv(" | ".join(subject for subject, _ in contact.subjects.most_common(5))),
                }
            )
        finish_atomic(handle, path)
        return len(ordered)
    except Exception:
        abort_atomic(handle)
        raise


def conversation_record(thread_key: str, messages: list[dict[str, Any]], identities: set[str]) -> dict[str, Any]:
    ordered = sorted(messages, key=message_sort_key)
    participants: dict[str, str] = {}
    for message in ordered:
        for person in external_people(message, identities):
            participants[person["email"]] = person["name"] or participants.get(person["email"], "")
    subject_counter = Counter(message.get("subject") or "(no subject)" for message in ordered)
    latest = ordered[-1]
    return {
        "thread_key": thread_key,
        "subject": subject_counter.most_common(1)[0][0],
        "participants": [{"name": name, "email": email} for email, name in sorted(participants.items())],
        "message_count": len(ordered),
        "start_at": message_time(ordered[0]),
        "end_at": message_time(latest),
        "latest_direction": direction(latest, identities),
        "needs_reply": direction(latest, identities) == "inbound",
        "attachment_count": sum(len(message.get("attachments", [])) for message in ordered),
        "message_ids": [message["archive_id"] for message in ordered],
        "raw_paths": [message["raw_path"] for message in ordered],
    }


def write_conversations_jsonl(conversations: list[dict[str, Any]], path: Path) -> None:
    handle = atomic_text_writer(path)
    try:
        for conversation in sorted(
            conversations,
            key=lambda item: (*time_sort_key(item["start_at"]), item["thread_key"]),
        ):
            handle.write(json.dumps(conversation, ensure_ascii=False) + "\n")
        finish_atomic(handle, path)
    except Exception:
        abort_atomic(handle)
        raise


def mailbox_statistics(
    connection: sqlite3.Connection,
    included_archive_ids: set[str] | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    membership_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(mailbox_membership)").fetchall()
    }
    state_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(mailbox_state)").fetchall()
    }
    has_current = "is_current" in membership_columns
    flags_column = "special_use_flags_json" if "special_use_flags_json" in state_columns else "'[]'"
    states = connection.execute(
        f"SELECT mailbox, {flags_column} AS flags_json FROM mailbox_state ORDER BY mailbox"
    ).fetchall()
    output: list[dict[str, Any]] = []
    for state in states:
        mailbox = state["mailbox"]
        flags = json_value(state["flags_json"])
        membership_where = " WHERE mailbox = ?"
        membership_parameters: tuple[Any, ...] = (mailbox,)
        if run_id:
            membership_where += " AND last_seen_run_id = ?"
            membership_parameters += (run_id,)
        memberships = connection.execute(
            "SELECT archive_id"
            + (", is_current" if has_current else "")
            + " FROM mailbox_membership"
            + membership_where,
            membership_parameters,
        ).fetchall()
        observed_ids = {
            str(row["archive_id"])
            for row in memberships
            if included_archive_ids is None or str(row["archive_id"]) in included_archive_ids
        }
        current_ids = {
            str(row["archive_id"])
            for row in memberships
            if (not has_current or int(row["is_current"]) == 1)
            and (included_archive_ids is None or str(row["archive_id"]) in included_archive_ids)
        }
        observed = len(observed_ids)
        current = len(current_ids)
        special_use = {
            r"\all",
            r"\drafts",
            r"\flagged",
            r"\important",
            r"\inbox",
            r"\junk",
            r"\sent",
            r"\trash",
        }
        system_flags = {str(flag).casefold() for flag in flags} & special_use
        normalized_name = mailbox.casefold().rsplit("/", 1)[-1]
        legacy_system_names = {
            "all mail",
            "drafts",
            "important",
            "inbox",
            "sent",
            "sent mail",
            "spam",
            "starred",
            "trash",
        }
        label_kind = "system" if system_flags or normalized_name in legacy_system_names else "user"
        output.append(
            {
                "mailbox": mailbox,
                "label_kind": label_kind,
                "special_use_flags": flags,
                "current_message_count": current,
                "observed_message_count": observed,
                "current_state_available": has_current,
            }
        )
    return output


def infer_account_identities(
    connection: sqlite3.Connection,
    primary_account: str,
    explicit_identities: Iterable[str],
) -> set[str]:
    identities = {primary_account.lower()}
    identities.update(value.strip().lower() for value in explicit_identities if value.strip())
    state_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(mailbox_state)").fetchall()
    }
    sent_mailboxes: set[str] = set()
    if "special_use_flags_json" in state_columns:
        for row in connection.execute("SELECT mailbox, special_use_flags_json FROM mailbox_state"):
            flags = {str(flag).casefold() for flag in json_value(row["special_use_flags_json"])}
            if r"\sent" in flags:
                sent_mailboxes.add(row["mailbox"])
    if not sent_mailboxes:
        for row in connection.execute("SELECT mailbox FROM mailbox_state"):
            name = str(row["mailbox"]).casefold()
            if name == "sent" or name.endswith("/sent") or name.endswith("/sent mail"):
                sent_mailboxes.add(row["mailbox"])
    for mailbox in sent_mailboxes:
        for row in connection.execute(
            "SELECT DISTINCT m.from_email FROM messages m "
            "JOIN mailbox_membership mm ON mm.archive_id = m.archive_id "
            "WHERE mm.mailbox = ? AND m.from_email <> ''",
            (mailbox,),
        ):
            identities.add(str(row[0]).lower())
    return identities


def write_mailboxes_csv(records: list[dict[str, Any]], path: Path) -> None:
    handle = atomic_text_writer(path)
    fieldnames = [
        "mailbox",
        "label_kind",
        "special_use_flags",
        "current_message_count",
        "observed_message_count",
        "current_state_available",
    ]
    try:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            row = dict(record)
            row["mailbox"] = safe_csv(row["mailbox"])
            row["special_use_flags"] = " | ".join(row["special_use_flags"])
            writer.writerow(row)
        finish_atomic(handle, path)
    except Exception:
        abort_atomic(handle)
        raise


def build_report(
    account: str,
    identities: set[str],
    metadata: list[dict[str, Any]],
    contacts: dict[str, ContactStats],
    conversations: list[dict[str, Any]],
    mailbox_stats: list[dict[str, Any]],
    output_paths: dict[str, Path],
    collection_manifest: dict[str, Any],
) -> str:
    ordered_metadata = sorted(metadata, key=message_sort_key)
    dates = [message_time(message) for message in ordered_metadata if message_time(message)]
    outbound = sum(direction(message, identities) == "outbound" for message in metadata)
    inbound = len(metadata) - outbound
    attachments = sum(len(message.get("attachments", [])) for message in metadata)
    total_bytes = sum(int(message.get("size_bytes") or 0) for message in metadata)
    needs_reply = sorted(
        (conversation for conversation in conversations if conversation["needs_reply"]),
        key=lambda item: time_sort_key(item["end_at"]),
        reverse=True,
    )
    keywords = top_keywords(message.get("subject") or "" for message in metadata)
    top_contacts = sorted(contacts.values(), key=lambda item: (-len(item.message_ids), item.email))[:20]
    generated = datetime.now(timezone.utc).isoformat()

    lines = [
        "# Gmail 邮件归档报告",
        "",
        "## 采集完整性说明",
        "",
        f"- 邮箱范围：**{collection_manifest.get('expected_scope', 'unknown')}**",
        f"- 时间范围模式：**{collection_manifest.get('coverage_mode', 'unknown')}**",
        f"- 起始日期：**{collection_manifest.get('since') or '无'}**",
        f"- 采集状态：**{collection_manifest.get('collection_status', 'unknown')}**",
        f"- IMAP 状态：**{collection_manifest.get('imap_status', 'unknown')}**",
        f"- Gmail API 状态：**{collection_manifest.get('api_status', 'unknown')}**",
        "",
    ]
    if collection_manifest.get("api_status") not in API_COMPLETE_STATUSES:
        lines.extend(
            [
                "> **API 数据缺口：** 本报告基于已验证的 IMAP/应用专用密码快照。尚未采集 Gmail API 专属元数据，",
                "> 包括原生标签 ID 与属性、History 变更事件、Draft 资源 ID、API Snippet、Size Estimate 和 API MIME Part ID。",
                "",
            ]
        )
    if collection_manifest.get("imap_status") == "legacy_local_complete":
        lines.extend(
            [
                "> **旧归档证据限制：** 本地文件与索引通过校验，但缺少绑定服务器计数的运行清单，不能视为服务器级全量证明。",
                "",
            ]
        )
    if collection_manifest.get("imap_status") == "bounded_complete":
        lines.extend(
            [
                f"> **日期范围限制：** 本报告只使用本次 run `{collection_manifest.get('run_id')}` 中由 IMAP `SINCE {collection_manifest.get('since')}` 实际命中的邮件。",
                "",
            ]
        )
    lines.extend(
        [
        "## 邮箱概览",
        "",
        f"- 账号：`{account}`",
        f"- 发件身份：`{', '.join(sorted(identities))}`",
        f"- 生成时间：`{generated}`",
        f"- 唯一邮件：**{len(metadata)}**",
        f"- 收件 / 发件：**{inbound} / {outbound}**",
        f"- 会话：**{len(conversations)}**",
        f"- 外部联系人：**{len(contacts)}**",
        f"- 待回复会话：**{len(needs_reply)}**",
        f"- 附件条目：**{attachments}**（原始附件保存在 `.eml` 内）",
        f"- 原始邮件体积：**{total_bytes / 1024 / 1024:.2f} MiB**",
        f"- 时间范围：`{dates[0] if dates else '无'}` 至 `{dates[-1] if dates else '无'}`",
        "",
        "## 主题关键词",
        "",
        ", ".join(f"{word} ({count})" for word, count in keywords) or "暂无可提取关键词。",
        "",
        "## 高频联系人",
        "",
        "| 联系人 | 名称 | 邮件 | 收到 | 发出 | 会话 | 最后往来 |",
        "|---|---|---:|---:|---:|---:|---|",
        ]
    )
    for contact in top_contacts:
        dates_for_contact = sorted((date for date in contact.dates if date), key=time_sort_key)
        name = contact.names.most_common(1)[0][0] if contact.names else ""
        lines.append(
            f"| {markdown_cell(contact.email)} | {markdown_cell(name)} | {len(contact.message_ids)} | "
            f"{len(contact.inbound_ids)} | {len(contact.outbound_ids)} | {len(contact.thread_keys)} | "
            f"{markdown_cell(dates_for_contact[-1] if dates_for_contact else '')} |"
        )

    lines.extend(
        [
            "",
            "## 邮箱与标签统计",
            "",
            "| 邮箱或标签 | 类型 | 当前邮件 | 历史观察 | Special-use flags |",
            "|---|---|---:|---:|---|",
        ]
    )
    for item in sorted(
        mailbox_stats,
        key=lambda value: (-int(value["current_message_count"]), str(value["mailbox"])),
    ):
        lines.append(
            f"| {markdown_cell(item['mailbox'])} | {item['label_kind']} | "
            f"{item['current_message_count']} | {item['observed_message_count']} | "
            f"{markdown_cell(', '.join(item['special_use_flags']))} |"
        )
    if any(not item["current_state_available"] for item in mailbox_stats):
        lines.extend(
            [
                "",
                "> 旧版 membership 表没有当前态字段；上表“当前邮件”暂按历史观察数展示，完成一次无边界全量重扫后才会变成当前快照。",
            ]
        )

    lines.extend(
        [
            "",
            "## 最近待回复会话",
            "",
            "| 最后时间 | 主题 | 联系人 | 邮件数 |",
            "|---|---|---|---:|",
        ]
    )
    for conversation in needs_reply[:30]:
        participant_text = ", ".join(person["email"] for person in conversation["participants"])
        lines.append(
            f"| {markdown_cell(conversation['end_at'])} | {markdown_cell(conversation['subject'])} | "
            f"{markdown_cell(participant_text)} | {conversation['message_count']} |"
        )
    if not needs_reply:
        lines.append("| - | 当前没有以外部来信结束的会话 | - | 0 |")

    lines.extend(
        [
            "",
            "## 沉淀文件",
            "",
            f"- 全量结构化邮件：`{output_paths['messages']}`",
            f"- 会话索引：`{output_paths['conversations']}`",
            f"- 联系人表：`{output_paths['contacts']}`",
            f"- 邮箱与标签表：`{output_paths['mailboxes']}`",
            "",
            "> 待回复判断基于“会话最后一封邮件是否来自外部”这一确定性规则；请在采取行动前人工确认。",
            "",
        ]
    )
    return "\n".join(lines)


def write_text(path: Path, content: str) -> None:
    handle = atomic_text_writer(path)
    try:
        handle.write(content)
        finish_atomic(handle, path)
    except Exception:
        abort_atomic(handle)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze a local Gmail archive")
    parser.add_argument("--archive-dir", type=Path, required=True)
    parser.add_argument(
        "--identity",
        action="append",
        default=[],
        help="Additional account/send-as email identity; repeat as needed",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Allow analysis without a passing completeness manifest",
    )
    parser.add_argument(
        "--check-freshness",
        action="store_true",
        help="Check whether existing derived outputs match the current completeness manifest",
    )
    return parser


def check_freshness(archive_dir: Path) -> int:
    collection_path = archive_dir / "manifests" / "collection_manifest.json"
    analysis_path = archive_dir / "derived" / "analysis_manifest.json"
    collection_hash = file_sha256(collection_path)
    try:
        analysis_manifest = json.loads(analysis_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        analysis_manifest = {}
    fresh = bool(
        collection_hash
        and analysis_manifest.get("status") == "completed"
        and analysis_manifest.get("collection_manifest_sha256") == collection_hash
    )
    output_hashes = analysis_manifest.get("output_sha256") or {}
    outputs_match = bool(output_hashes) and all(
        file_sha256(archive_dir / relative_path) == expected_hash
        for relative_path, expected_hash in output_hashes.items()
    )
    fresh = fresh and outputs_match
    print(
        json.dumps(
            {
                "status": "fresh" if fresh else "stale",
                "archive_dir": str(archive_dir),
                "collection_manifest_sha256": collection_hash,
                "analysis_manifest": str(analysis_path),
                "outputs_match": outputs_match,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if fresh else 1


def main() -> int:
    args = build_parser().parse_args()
    archive_dir = args.archive_dir.expanduser().resolve()
    if args.check_freshness:
        return check_freshness(archive_dir)
    database_path = archive_dir / "archive.sqlite3"
    if not database_path.exists():
        raise SystemExit(f"Archive database not found: {database_path}")
    collection_manifest = load_collection_manifest(archive_dir)
    if not collection_manifest:
        if not args.allow_partial:
            raise SystemExit(
                "Completeness manifest not found. Run verify_archive.py before analysis or use --allow-partial explicitly."
            )
        collection_manifest = {
            "collection_status": "partial",
            "imap_status": "unverified",
            "api_status": "not_configured",
            "analysis_allowed": False,
            "warnings": ["Analysis was explicitly allowed without a completeness manifest."],
        }
    elif int(collection_manifest.get("schema_version", 1)) < 2 and not args.allow_partial:
        raise SystemExit(
            "Completeness manifest predates schema v2. Run verify_archive.py again before analysis."
        )
    elif not collection_manifest.get("analysis_allowed") and not args.allow_partial:
        raise SystemExit(
            "Archive verification does not allow analysis. Resolve critical completeness failures or use --allow-partial explicitly."
        )

    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.create_function("MESSAGE_EPOCH", 2, message_epoch)
    connection.row_factory = sqlite3.Row
    try:
        accounts = [row[0] for row in connection.execute("SELECT DISTINCT account FROM messages")]
        if not accounts:
            raise SystemExit("Archive contains no messages")
        if len(accounts) != 1:
            raise SystemExit("Archive contains multiple accounts; separate them before analysis")
        account = accounts[0].lower()
        identities = infer_account_identities(connection, account, args.identity)

        analysis_run_id = (
            str(collection_manifest.get("run_id") or "")
            if collection_manifest.get("coverage_mode") == "bounded"
            else ""
        )
        if collection_manifest.get("coverage_mode") == "bounded" and not analysis_run_id:
            raise SystemExit("Bounded collection manifest is missing its run_id; run verification again")
        metadata_where = (
            "WHERE EXISTS (SELECT 1 FROM mailbox_membership mm "
            "WHERE mm.archive_id = messages.archive_id AND mm.last_seen_run_id = ?)"
            if analysis_run_id
            else ""
        )
        metadata_rows = connection.execute(
            f"""
            SELECT archive_id, account, thread_key, message_id, sent_at, internal_date,
                   subject, from_name, from_email, to_json, cc_json, bcc_json,
                   source_mailboxes_json, raw_path, attachments_json, size_bytes
            FROM messages
            {metadata_where}
            ORDER BY MESSAGE_EPOCH(sent_at, internal_date), archive_id
            """,
            (analysis_run_id,) if analysis_run_id else (),
        ).fetchall()
        metadata = [row_to_message(row, include_bodies=False) for row in metadata_rows]
        if not metadata:
            raise SystemExit("The verified analysis scope contains no messages")

        contacts: dict[str, ContactStats] = {}
        threads: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for message in metadata:
            message_direction = direction(message, identities)
            threads[message["thread_key"]].append(message)
            for person in external_people(message, identities):
                contact = contacts.setdefault(person["email"], ContactStats(person["email"]))
                if person["name"]:
                    contact.names[person["name"]] += 1
                contact.message_ids.add(message["archive_id"])
                contact.thread_keys.add(message["thread_key"])
                contact.dates.append(message_time(message))
                if message.get("subject"):
                    contact.subjects[message["subject"]] += 1
                if message_direction == "outbound":
                    contact.outbound_ids.add(message["archive_id"])
                else:
                    contact.inbound_ids.add(message["archive_id"])

        conversations = [conversation_record(key, messages, identities) for key, messages in threads.items()]
        included_archive_ids = {str(message["archive_id"]) for message in metadata}
        mailbox_stats = mailbox_statistics(
            connection,
            included_archive_ids if analysis_run_id else None,
            analysis_run_id or None,
        )
        derived_dir = archive_dir / "derived"
        output_paths = {
            "messages": derived_dir / "messages.jsonl",
            "conversations": derived_dir / "conversations.jsonl",
            "contacts": derived_dir / "contacts.csv",
            "mailboxes": derived_dir / "mailboxes.csv",
            "report": derived_dir / "archive_report.md",
            "analysis_manifest": derived_dir / "analysis_manifest.json",
        }
        message_count = write_messages_jsonl(
            connection,
            output_paths["messages"],
            analysis_run_id or None,
        )
        write_conversations_jsonl(conversations, output_paths["conversations"])
        contact_count = write_contacts_csv(contacts, output_paths["contacts"])
        write_mailboxes_csv(mailbox_stats, output_paths["mailboxes"])
        report = build_report(
            account,
            identities,
            metadata,
            contacts,
            conversations,
            mailbox_stats,
            output_paths,
            collection_manifest,
        )
        write_text(output_paths["report"], report)

        collection_manifest_path = archive_dir / "manifests" / "collection_manifest.json"
        collection_hash = file_sha256(collection_manifest_path)
        analysis_generated_at = datetime.now(timezone.utc).isoformat()
        analysis_manifest = {
            "schema_version": 1,
            "status": "completed",
            "generated_at": analysis_generated_at,
            "archive_dir": str(archive_dir),
            "collection_manifest_generated_at": collection_manifest.get("generated_at"),
            "collection_manifest_sha256": collection_hash,
            "run_id": collection_manifest.get("run_id"),
            "expected_scope": collection_manifest.get("expected_scope"),
            "coverage_mode": collection_manifest.get("coverage_mode"),
            "messages": message_count,
            "conversations": len(conversations),
            "contacts": contact_count,
            "identities": sorted(identities),
            "mailboxes": len(mailbox_stats),
            "output_sha256": {
                str(path.relative_to(archive_dir)): file_sha256(path)
                for name, path in output_paths.items()
                if name != "analysis_manifest"
            },
        }
        write_text(
            output_paths["analysis_manifest"],
            json.dumps(analysis_manifest, ensure_ascii=False, indent=2) + "\n",
        )

        result = {
            "status": "completed",
            "archive_dir": str(archive_dir),
            "messages": message_count,
            "conversations": len(conversations),
            "contacts": contact_count,
            "needs_reply": sum(item["needs_reply"] for item in conversations),
            "collection_status": collection_manifest.get("collection_status"),
            "run_id": collection_manifest.get("run_id"),
            "expected_scope": collection_manifest.get("expected_scope"),
            "coverage_mode": collection_manifest.get("coverage_mode"),
            "since": collection_manifest.get("since"),
            "imap_status": collection_manifest.get("imap_status"),
            "api_status": collection_manifest.get("api_status"),
            "warnings": collection_manifest.get("warnings", []),
            "outputs": {name: str(path) for name, path in output_paths.items()},
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
