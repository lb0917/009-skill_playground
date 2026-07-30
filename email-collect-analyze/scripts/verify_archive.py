#!/usr/bin/env python3
"""Verify local IMAP archive integrity and write completeness manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from archive_contract import (
    API_COMPLETE_STATUSES,
    API_OMISSIONS,
    HARD_LIMITS,
    api_evidence_errors,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        os.chmod(handle.name, 0o600)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(handle.name, path)
    finally:
        if not handle.closed:
            handle.close()
        try:
            os.unlink(handle.name)
        except FileNotFoundError:
            pass


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def check_record(name: str, passed: bool, critical: bool, details: Any) -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "critical": bool(critical),
        "details": details,
    }


def special_mailbox_present(mailboxes: list[dict[str, Any]], kind: str) -> bool:
    flag_patterns = {
        "all_mail": {r"\all"},
        "spam": {r"\junk"},
        "trash": {r"\trash"},
    }
    for mailbox in mailboxes:
        flags = {str(flag).casefold() for flag in mailbox.get("flags", [])}
        if flags & flag_patterns[kind]:
            return True
    lowered = {str(mailbox.get("name", "")).casefold() for mailbox in mailboxes}
    patterns = {
        "all_mail": ("/all mail", "/allmail", "all mail"),
        "spam": ("/spam", "/junk", "spam", "junk"),
        "trash": ("/trash", "trash"),
    }
    return any(name.endswith(pattern) for name in lowered for pattern in patterns[kind])


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def derived_coverage_mode(options: dict[str, Any]) -> str:
    if options.get("limit") not in (None, ""):
        return "limited"
    if options.get("since") not in (None, ""):
        return "bounded"
    return "unbounded"


def integer(value: Any, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(value or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def verify_archive(
    archive_dir: Path,
    expected_scope: str,
    require_api: bool,
    max_baseline_age_days: int = 30,
) -> dict[str, Any]:
    archive_dir = archive_dir.expanduser().resolve()
    database_path = archive_dir / "archive.sqlite3"
    if not database_path.exists():
        raise RuntimeError(f"Archive database not found: {database_path}")

    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    checks: list[dict[str, Any]] = []
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        required_tables = {"messages", "mailbox_membership", "mailbox_state"}
        missing_tables = sorted(required_tables - tables)
        checks.append(check_record("required_tables", not missing_tables, True, {"missing": missing_tables}))
        if missing_tables:
            raise RuntimeError(f"Missing required tables: {', '.join(missing_tables)}")

        rows = connection.execute(
            """
            SELECT archive_id, gmail_msgid, thread_key, message_id, sent_at,
                   internal_date, from_email, raw_path, size_bytes, sha256,
                   attachments_json
            FROM messages
            """
        ).fetchall()
        accounts = {
            row[0] for row in connection.execute("SELECT DISTINCT account FROM messages").fetchall()
        }
        checks.append(check_record("single_database_account", len(accounts) <= 1, True, {"accounts": sorted(accounts)}))
        checks.append(check_record("messages_table_scanned", True, True, {"count": len(rows)}))

        raw_root = archive_dir / "raw"
        raw_files = {path.resolve() for path in raw_root.rglob("*.eml")} if raw_root.exists() else set()
        referenced: set[Path] = set()
        missing_raw: list[str] = []
        size_mismatches: list[str] = []
        hash_mismatches: list[str] = []
        parse_errors: list[dict[str, str]] = []
        outside_raw_root: list[str] = []
        missing_gmail_ids: list[str] = []
        missing_thread_keys: list[str] = []
        missing_dates: list[str] = []
        missing_senders: list[str] = []
        invalid_attachment_json: list[str] = []
        structured_attachments = 0
        mime_attachment_candidates = 0
        unindexed_leaf_types: Counter[str] = Counter()

        for row in rows:
            archive_id = row["archive_id"]
            if not row["gmail_msgid"]:
                missing_gmail_ids.append(archive_id)
            if not row["thread_key"]:
                missing_thread_keys.append(archive_id)
            if not (row["sent_at"] or row["internal_date"]):
                missing_dates.append(archive_id)
            if not row["from_email"]:
                missing_senders.append(archive_id)

            try:
                attachments = json.loads(row["attachments_json"] or "[]")
                if not isinstance(attachments, list):
                    raise ValueError("attachments_json is not a list")
                structured_attachments += len(attachments)
            except (json.JSONDecodeError, TypeError, ValueError):
                invalid_attachment_json.append(archive_id)

            raw_path = (archive_dir / row["raw_path"]).resolve()
            referenced.add(raw_path)
            try:
                raw_path.relative_to(raw_root.resolve())
            except ValueError:
                outside_raw_root.append(archive_id)
            if not raw_path.exists():
                missing_raw.append(archive_id)
                continue
            raw = raw_path.read_bytes()
            if len(raw) != int(row["size_bytes"]):
                size_mismatches.append(archive_id)
            if hashlib.sha256(raw).hexdigest() != row["sha256"]:
                hash_mismatches.append(archive_id)
            try:
                message = BytesParser(policy=policy.default).parsebytes(raw)
                for part in message.walk():
                    if part.is_multipart():
                        continue
                    content_type = part.get_content_type()
                    disposition = (part.get_content_disposition() or "").lower()
                    filename = part.get_filename() or ""
                    if disposition == "attachment" or filename:
                        mime_attachment_candidates += 1
                    elif content_type not in {"text/plain", "text/html"}:
                        unindexed_leaf_types[content_type] += 1
            except Exception as error:
                parse_errors.append({"archive_id": archive_id, "error": type(error).__name__})

        orphan_raw = sorted(str(path) for path in raw_files - referenced)
        checks.extend(
            [
                check_record("raw_file_count", len(raw_files) == len(rows), True, {"database": len(rows), "raw": len(raw_files)}),
                check_record("unique_raw_paths", len(referenced) == len(rows), True, {"database": len(rows), "unique_paths": len(referenced)}),
                check_record("raw_paths_within_archive", not outside_raw_root, True, {"outside_count": len(outside_raw_root), "sample": outside_raw_root[:10]}),
                check_record("raw_files_present", not missing_raw, True, {"missing_count": len(missing_raw), "sample": missing_raw[:10]}),
                check_record("no_orphan_raw_files", not orphan_raw, True, {"orphan_count": len(orphan_raw), "sample": orphan_raw[:10]}),
                check_record("raw_sizes_match", not size_mismatches, True, {"mismatch_count": len(size_mismatches), "sample": size_mismatches[:10]}),
                check_record("raw_hashes_match", not hash_mismatches, True, {"mismatch_count": len(hash_mismatches), "sample": hash_mismatches[:10]}),
                check_record("raw_messages_parse", not parse_errors, True, {"error_count": len(parse_errors), "sample": parse_errors[:10]}),
                check_record("gmail_ids_present", not missing_gmail_ids, True, {"missing_count": len(missing_gmail_ids), "sample": missing_gmail_ids[:10]}),
                check_record("thread_keys_present", not missing_thread_keys, True, {"missing_count": len(missing_thread_keys), "sample": missing_thread_keys[:10]}),
                check_record("message_dates_present", not missing_dates, True, {"missing_count": len(missing_dates), "sample": missing_dates[:10]}),
                check_record("sender_addresses_present", not missing_senders, False, {"missing_count": len(missing_senders), "sample": missing_senders[:10]}),
                check_record("attachment_metadata_valid", not invalid_attachment_json, True, {"invalid_count": len(invalid_attachment_json), "sample": invalid_attachment_json[:10]}),
                check_record(
                    "standard_attachments_indexed",
                    structured_attachments == mime_attachment_candidates,
                    True,
                    {"structured": structured_attachments, "mime_candidates": mime_attachment_candidates},
                ),
            ]
        )

        membership_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(mailbox_membership)").fetchall()
        }
        has_current_membership = "is_current" in membership_columns
        has_membership_run_id = "last_seen_run_id" in membership_columns
        membership_rows = connection.execute("SELECT COUNT(*) FROM mailbox_membership").fetchone()[0]
        current_membership_rows = connection.execute(
            "SELECT COUNT(*) FROM mailbox_membership" + (" WHERE is_current = 1" if has_current_membership else "")
        ).fetchone()[0]
        nonempty_mailboxes = connection.execute(
            "SELECT COUNT(DISTINCT mailbox) FROM mailbox_membership"
            + (" WHERE is_current = 1" if has_current_membership else "")
        ).fetchone()[0]
        dangling_memberships = connection.execute(
            "SELECT COUNT(*) FROM mailbox_membership mm "
            "LEFT JOIN messages m ON m.archive_id = mm.archive_id WHERE m.archive_id IS NULL"
        ).fetchone()[0]
        state_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(mailbox_state)").fetchall()
        }
        if "special_use_flags_json" in state_columns:
            mailbox_state_rows = connection.execute(
                "SELECT mailbox, special_use_flags_json FROM mailbox_state"
            ).fetchall()
            mailbox_records = [
                {
                    "name": row["mailbox"],
                    "flags": json_list(row["special_use_flags_json"]),
                }
                for row in mailbox_state_rows
            ]
        else:
            mailbox_state_rows = connection.execute("SELECT mailbox FROM mailbox_state").fetchall()
            mailbox_records = [{"name": row["mailbox"], "flags": []} for row in mailbox_state_rows]
        mailbox_names = {str(item["name"]) for item in mailbox_records}
        without_membership = connection.execute(
            """
            SELECT COUNT(*)
            FROM messages m
            LEFT JOIN mailbox_membership mm ON mm.archive_id = m.archive_id
            WHERE mm.archive_id IS NULL
            """
        ).fetchone()[0]
        checks.append(
            check_record(
                "messages_have_mailbox_membership",
                without_membership == 0,
                True,
                {"without_membership": without_membership, "membership_rows": membership_rows},
            )
        )
        checks.append(
            check_record(
                "no_dangling_mailbox_memberships",
                dangling_memberships == 0,
                True,
                {"dangling_memberships": dangling_memberships},
            )
        )
        checks.append(
            check_record(
                "mailbox_state_present",
                bool(mailbox_names),
                True,
                {"mailbox_state_count": len(mailbox_names), "nonempty_mailbox_count": nonempty_mailboxes},
            )
        )
        run_manifest_path = archive_dir / "manifests" / "imap_collection_run.json"
        loaded_run_manifest = load_json(run_manifest_path)
        legacy_run_manifest = bool(
            loaded_run_manifest and integer(loaded_run_manifest.get("schema_version", 1), 1) < 2
        )
        run_manifest = None if legacy_run_manifest else loaded_run_manifest
        coverage_mode = "legacy"
        collection_options: dict[str, Any] = {}
        baseline_manifest: dict[str, Any] | None = None
        baseline_age_days: float | None = None
        analysis_scope_messages = len(rows)
        if run_manifest:
            results = run_manifest.get("mailbox_results") or []
            collection_options = run_manifest.get("collection_options") or {}
            coverage_mode = str(run_manifest.get("coverage_mode") or derived_coverage_mode(collection_options))
            selected_mailboxes = set(run_manifest.get("selected_mailboxes") or [])
            if expected_scope == "all":
                selected_records = [
                    item for item in mailbox_records if str(item["name"]) in selected_mailboxes
                ]
                selected_special_checks = {
                    kind: special_mailbox_present(selected_records, kind)
                    for kind in ("all_mail", "spam", "trash")
                }
                checks.append(
                    check_record(
                        "full_scope_selected_special_mailboxes",
                        all(selected_special_checks.values()),
                        True,
                        selected_special_checks,
                    )
                )
            result_mailboxes = {
                str(item.get("mailbox")) for item in results if item.get("mailbox")
            }
            processed_all = all(
                integer(item.get("processed"), -1) == integer(item.get("candidate_uids"), -2)
                for item in results
            )
            run_mailboxes_ok = (
                bool(selected_mailboxes)
                and selected_mailboxes == result_mailboxes
                and selected_mailboxes.issubset(mailbox_names)
            )
            completed_at = parse_timestamp(run_manifest.get("completed_at"))
            started_at = parse_timestamp(run_manifest.get("started_at"))
            run_binding_ok = (
                bool(run_manifest.get("run_id"))
                and run_manifest.get("account") in accounts if accounts else bool(run_manifest.get("account"))
            )
            run_binding_ok = bool(
                run_binding_ok
                and Path(str(run_manifest.get("archive_dir", ""))).expanduser().resolve() == archive_dir
                and Path(str(run_manifest.get("database", ""))).expanduser().resolve() == database_path
                and integer(run_manifest.get("total_unique_messages"), -1) == len(rows)
                and run_manifest.get("expected_scope") == expected_scope
                and coverage_mode == derived_coverage_mode(collection_options)
                and completed_at is not None
                and started_at is not None
                and completed_at >= started_at
            )
            run_ok = (
                run_manifest.get("status") == "completed"
                and bool(results)
                and processed_all
                and run_mailboxes_ok
                and run_binding_ok
            )
            checks.append(
                check_record(
                    "latest_collection_run_completed",
                    run_ok,
                    True,
                    {
                        "status": run_manifest.get("status"),
                        "mailboxes": len(results),
                        "all_candidates_processed": processed_all,
                        "selected_results_and_state_match": run_mailboxes_ok,
                        "run_binding_matches_archive": run_binding_ok,
                    },
                )
            )
            if coverage_mode == "limited":
                checks.append(
                    check_record(
                        "limited_collection_cannot_be_complete",
                        False,
                        True,
                        {"limit": collection_options.get("limit")},
                    )
                )
            elif coverage_mode == "bounded":
                bounded_ok = collection_options.get("since") not in (None, "") and collection_options.get("limit") in (None, "")
                checks.append(
                    check_record(
                        "bounded_scope_declared",
                        bounded_ok,
                        True,
                        {"since": collection_options.get("since"), "limit": collection_options.get("limit")},
                    )
                )
                analysis_scope_messages = (
                    connection.execute(
                        "SELECT COUNT(DISTINCT archive_id) FROM mailbox_membership "
                        "WHERE last_seen_run_id = ?",
                        (run_manifest.get("run_id"),),
                    ).fetchone()[0]
                    if has_membership_run_id
                    else 0
                )
            elif coverage_mode == "unbounded":
                baseline_manifest = (
                    run_manifest
                    if run_manifest.get("baseline_evidence")
                    else load_json(archive_dir / "manifests" / "imap_scope_baseline.json")
                )
                evidence = baseline_manifest or {}
                evidence_results = evidence.get("mailbox_results") or []
                evidence_selected = set(evidence.get("selected_mailboxes") or [])
                evidence_result_names = {
                    str(item.get("mailbox")) for item in evidence_results if item.get("mailbox")
                }
                options = evidence.get("collection_options") or {}
                unbounded = options.get("since") in (None, "") and options.get("limit") in (None, "")
                full_scan = bool(evidence_results) and all(
                    item.get("scan_mode") == "full" for item in evidence_results
                )
                server_counts_match = all(
                    integer(item.get("candidate_uids"), -1) == integer(item.get("server_message_count"), -2)
                    and integer(item.get("processed"), -1) == integer(item.get("candidate_uids"), -2)
                    and integer(item.get("current_membership_count"), -1) == integer(item.get("server_message_count"), -2)
                    for item in evidence_results
                )
                baseline_completed_at = parse_timestamp(evidence.get("completed_at"))
                if baseline_completed_at is not None:
                    baseline_age_days = max(
                        0.0,
                        (datetime.now(timezone.utc) - baseline_completed_at).total_seconds() / 86400,
                    )
                baseline_binding_ok = bool(
                    evidence.get("account") == run_manifest.get("account")
                    and Path(str(evidence.get("archive_dir", ""))).expanduser().resolve() == archive_dir
                    and evidence.get("expected_scope") == expected_scope
                    and evidence_selected == selected_mailboxes
                )
                baseline_evidence_ok = (
                    evidence.get("status") == "completed"
                    and evidence.get("coverage_mode") == "unbounded"
                    and bool(evidence_selected)
                    and evidence_selected == evidence_result_names
                    and evidence_selected.issubset(mailbox_names)
                    and baseline_binding_ok
                    and unbounded
                    and full_scan
                    and server_counts_match
                )
                checks.append(
                    check_record(
                        "scope_baseline_evidence",
                        baseline_evidence_ok,
                        True,
                        {
                            "scope": evidence.get("expected_scope"),
                            "source": "latest_run" if baseline_manifest is run_manifest else "persisted_scope_baseline",
                            "binding_matches_latest_run": baseline_binding_ok,
                            "selected_results_and_state_match": (
                                bool(evidence_selected)
                                and evidence_selected == evidence_result_names
                                and evidence_selected.issubset(mailbox_names)
                            ),
                            "unbounded": unbounded,
                            "all_mailboxes_full_scan": full_scan,
                            "server_counts_match_candidate_uids": server_counts_match,
                        },
                    )
                )
                checks.append(
                    check_record(
                        "scope_baseline_recent",
                        baseline_age_days is not None and baseline_age_days <= max_baseline_age_days,
                        True,
                        {
                            "age_days": baseline_age_days,
                            "max_age_days": max_baseline_age_days,
                            "completed_at": evidence.get("completed_at"),
                        },
                    )
                )
                if expected_scope == "all":
                    full_sweep_manifest = load_json(archive_dir / "manifests" / "imap_full_sweep.json")
                    full_sweep_ok = bool(
                        full_sweep_manifest
                        and full_sweep_manifest.get("run_id") == evidence.get("run_id")
                        and full_sweep_manifest.get("full_sweep_evidence") is True
                    )
                    checks.append(
                        check_record(
                            "full_sweep_alias_matches_baseline",
                            full_sweep_ok,
                            True,
                            {"baseline_run_id": evidence.get("run_id")},
                        )
                    )
            else:
                checks.append(check_record("known_coverage_mode", False, True, {"coverage_mode": coverage_mode}))
        else:
            if expected_scope == "all":
                special_checks = {
                    kind: special_mailbox_present(mailbox_records, kind)
                    for kind in ("all_mail", "spam", "trash")
                }
                checks.append(
                    check_record(
                        "legacy_full_scope_special_mailboxes",
                        all(special_checks.values()),
                        True,
                        special_checks,
                    )
                )
            checks.append(
                check_record(
                    "latest_collection_run_manifest",
                    False,
                    False,
                    (
                        "Legacy archive: the run manifest predates schema v2 evidence binding"
                        if legacy_run_manifest
                        else "Legacy archive: the successful run predates persisted run manifests"
                    ),
                )
            )

        api_manifest_path = archive_dir / "manifests" / "api_collection.json"
        api_manifest = load_json(api_manifest_path)
        api_status = "not_configured"
        api_errors: list[str] = []
        if api_manifest:
            api_status = str(api_manifest.get("status") or "partial")
            if api_status in API_COMPLETE_STATUSES:
                api_errors = api_evidence_errors(api_manifest)
                if accounts and api_manifest.get("account") not in accounts:
                    api_errors.append("API account does not match the archive account")
                if Path(str(api_manifest.get("archive_dir", ""))).expanduser().resolve() != archive_dir:
                    api_errors.append("API archive_dir does not match the verified archive")
                if api_errors:
                    api_status = "invalid"
            elif api_manifest.get("claimed_status") in API_COMPLETE_STATUSES:
                api_errors = [str(item) for item in api_manifest.get("evidence_errors") or []]
                api_status = "invalid"
        api_complete = bool(api_manifest) and not api_errors and api_manifest.get("status") in API_COMPLETE_STATUSES

        critical_failures = [item for item in checks if item["critical"] and not item["passed"]]
        critical_failure_names = {item["name"] for item in critical_failures}
        if critical_failures:
            if run_manifest and run_manifest.get("status") in {"running", "failed"}:
                imap_status = "failed"
            elif critical_failure_names == {"scope_baseline_recent"}:
                imap_status = "stale"
            else:
                imap_status = "partial"
        elif not run_manifest:
            imap_status = "legacy_local_complete"
        elif coverage_mode == "bounded":
            imap_status = "bounded_complete"
        else:
            imap_status = "complete"

        analyzable_imap = imap_status in {"complete", "bounded_complete", "legacy_local_complete"}
        analysis_allowed = analyzable_imap and analysis_scope_messages > 0 and (api_complete or not require_api)
        if imap_status == "failed":
            collection_status = "failed"
        elif imap_status == "stale":
            collection_status = "stale"
        elif imap_status == "partial":
            collection_status = "partial"
        elif imap_status == "complete" and api_complete:
            collection_status = "complete"
        elif imap_status == "complete":
            collection_status = "imap_complete_api_pending"
        elif imap_status == "bounded_complete":
            collection_status = "imap_bounded_complete" if api_complete else "imap_bounded_complete_api_pending"
        else:
            collection_status = "imap_legacy_local_complete" if api_complete else "imap_legacy_local_complete_api_pending"

        warnings = []
        if not api_complete:
            warnings.append(
                "IMAP/app-password collection passed local verification, but Gmail API-only metadata was not collected."
                if imap_status == "complete"
                else "Gmail API-only metadata was not collected."
            )
        if api_errors:
            warnings.append("The Gmail API manifest claimed completeness but failed the evidence contract.")
        if unindexed_leaf_types:
            warnings.append("Non-standard MIME leaf types remain preserved in raw EML but are not separately indexed.")
        if not run_manifest:
            warnings.append(
                "Legacy archive: local files are verified, but schema v2 server-level collection completeness is not proven."
            )
        if not has_current_membership:
            warnings.append("Legacy membership schema: label memberships are observed history, not a reconciled current-state snapshot.")
        if coverage_mode == "bounded":
            warnings.append(f"Analysis is bounded to messages collected since {collection_options.get('since')}.")
        if coverage_mode == "limited":
            warnings.append("A message limit was used; the collection is intentionally partial and analysis is blocked by default.")
        if not rows:
            warnings.append("The verified archive contains no messages, so analysis is not useful and remains blocked.")
        elif analysis_scope_messages == 0:
            warnings.append("The verified collection scope contains no messages, so analysis remains blocked.")

        manifest = {
            "schema_version": 2,
            "generated_at": utc_now(),
            "archive_dir": str(archive_dir),
            "source_mode": "imap_app_password",
            "run_id": run_manifest.get("run_id") if run_manifest else None,
            "expected_scope": expected_scope,
            "coverage_mode": coverage_mode,
            "since": collection_options.get("since"),
            "limit": collection_options.get("limit"),
            "collection_status": collection_status,
            "imap_status": imap_status,
            "api_status": api_status,
            "analysis_allowed": analysis_allowed,
            "require_api": require_api,
            "counts": {
                "messages": len(rows),
                "analysis_scope_messages": analysis_scope_messages,
                "raw_eml_files": len(raw_files),
                "mailbox_states": len(mailbox_names),
                "nonempty_mailboxes": nonempty_mailboxes,
                "mailbox_memberships": membership_rows,
                "current_mailbox_memberships": current_membership_rows,
                "structured_attachments": structured_attachments,
                "mime_attachment_candidates": mime_attachment_candidates,
            },
            "checks": checks,
            "critical_failure_count": len(critical_failures),
            "baseline_age_days": baseline_age_days,
            "max_baseline_age_days": max_baseline_age_days,
            "membership_current_state_available": has_current_membership,
            "unindexed_mime_leaf_types": dict(sorted(unindexed_leaf_types.items())),
            "api_omissions": [] if api_complete else API_OMISSIONS,
            "api_evidence_errors": api_errors,
            "hard_limits": HARD_LIMITS,
            "warnings": warnings,
        }
        return manifest
    finally:
        connection.close()


def markdown_report(manifest: dict[str, Any]) -> str:
    counts = manifest["counts"]
    lines = [
        "# Gmail 采集完整性报告",
        "",
        f"- 归档：`{manifest['archive_dir']}`",
        f"- 生成时间：`{manifest['generated_at']}`",
        f"- 邮箱范围：**{manifest['expected_scope']}**",
        f"- 时间范围模式：**{manifest['coverage_mode']}**",
        f"- 起始日期：**{manifest.get('since') or '无'}**",
        f"- 数量限制：**{manifest.get('limit') or '无'}**",
        f"- 采集状态：**{manifest['collection_status']}**",
        f"- IMAP 状态：**{manifest['imap_status']}**",
        f"- Gmail API 状态：**{manifest['api_status']}**",
        f"- 允许分析：**{'是' if manifest['analysis_allowed'] else '否'}**",
        f"- 邮件 / EML：**{counts['messages']} / {counts['raw_eml_files']}**",
        f"- 邮箱状态 / 非空邮箱：**{counts['mailbox_states']} / {counts['nonempty_mailboxes']}**",
        f"- 标签归属：**{counts['mailbox_memberships']}**",
        f"- 当前标签归属：**{counts['current_mailbox_memberships']}**",
        f"- 标准附件：**{counts['structured_attachments']}**",
        "",
        "## 校验项",
        "",
        "| 校验 | 结果 | 关键 | 详情 |",
        "|---|---|---|---|",
    ]
    for item in manifest["checks"]:
        details = json.dumps(item["details"], ensure_ascii=False).replace("|", "\\|")
        lines.append(
            f"| {item['name']} | {'通过' if item['passed'] else '未通过'} | "
            f"{'是' if item['critical'] else '否'} | `{details}` |"
        )

    lines.extend(["", "## Gmail API 缺口", ""])
    if manifest["api_omissions"]:
        lines.extend(f"- {item}" for item in manifest["api_omissions"])
    else:
        lines.append("- 无；API 完整性校验已通过。")

    if manifest.get("api_evidence_errors"):
        lines.extend(["", "### API 完整证据错误", ""])
        lines.extend(f"- {item}" for item in manifest["api_evidence_errors"])

    lines.extend(["", "## 无法通过公开只读接口保证的信息", ""])
    lines.extend(f"- {item}" for item in manifest["hard_limits"])

    if manifest["warnings"]:
        lines.extend(["", "## 警告", ""])
        lines.extend(f"- {item}" for item in manifest["warnings"])
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify an email-collect-analyze archive")
    parser.add_argument("--archive-dir", type=Path, required=True)
    parser.add_argument(
        "--expected-scope",
        choices=("auto", "all", "normal", "selected"),
        default="auto",
        help="Scope to verify; auto reads manifests/imap_collection_run.json",
    )
    parser.add_argument("--require-api", action="store_true", help="Block analysis unless the Gmail API manifest is complete")
    parser.add_argument(
        "--max-baseline-age-days",
        type=int,
        default=30,
        help="Maximum age of the last unbounded full-scan baseline (default: 30)",
    )
    return parser


def write_failed_verification(archive_dir: Path, error: str, expected_scope: str) -> dict[str, Any]:
    manifest = {
        "schema_version": 2,
        "generated_at": utc_now(),
        "archive_dir": str(archive_dir),
        "source_mode": "imap_app_password",
        "expected_scope": expected_scope or "unknown",
        "coverage_mode": "unknown",
        "collection_status": "failed",
        "imap_status": "failed",
        "api_status": "unknown",
        "analysis_allowed": False,
        "critical_failure_count": 1,
        "error": error,
        "warnings": ["Verification failed; existing derived outputs must be treated as stale."],
    }
    manifest_dir = archive_dir / "manifests"
    manifest_path = manifest_dir / "collection_manifest.json"
    report_json_path = manifest_dir / "completeness_report.json"
    report_path = manifest_dir / "completeness_report.md"
    manifest["outputs"] = {
        "manifest": str(manifest_path),
        "report": str(report_path),
        "report_json": str(report_json_path),
    }
    serialized = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(manifest_path, serialized)
    atomic_write_text(report_json_path, serialized)
    atomic_write_text(
        report_path,
        "# Gmail 采集完整性报告\n\n"
        "- 采集状态：**failed**\n"
        "- 允许分析：**否**\n"
        f"- 错误：{error}\n",
    )
    return manifest


def main() -> int:
    args = build_parser().parse_args()
    if args.max_baseline_age_days < 1:
        raise SystemExit("--max-baseline-age-days must be positive")
    archive_dir = args.archive_dir.expanduser().resolve()
    expected_scope = args.expected_scope
    if expected_scope == "auto":
        run_manifest = load_json(archive_dir / "manifests" / "imap_collection_run.json")
        expected_scope = str((run_manifest or {}).get("expected_scope") or "")
        if expected_scope not in {"all", "normal", "selected"}:
            error = (
                "Unable to infer collection scope from manifests/imap_collection_run.json; "
                "pass --expected-scope all, normal, or selected for a legacy archive"
            )
            failure = write_failed_verification(archive_dir, error, "unknown")
            print(json.dumps(failure, ensure_ascii=False, indent=2))
            return 1
    try:
        manifest = verify_archive(
            archive_dir,
            expected_scope,
            args.require_api,
            args.max_baseline_age_days,
        )
    except Exception as error:
        failure = write_failed_verification(archive_dir, str(error), expected_scope)
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        return 1

    manifest_dir = archive_dir / "manifests"
    manifest_path = manifest_dir / "collection_manifest.json"
    report_path = manifest_dir / "completeness_report.md"
    report_json_path = manifest_dir / "completeness_report.json"
    manifest["outputs"] = {
        "manifest": str(manifest_path),
        "report": str(report_path),
        "report_json": str(report_json_path),
    }
    serialized = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(manifest_path, serialized)
    atomic_write_text(report_json_path, serialized)
    atomic_write_text(report_path, markdown_report(manifest))
    print(serialized, end="")
    return 0 if manifest["analysis_allowed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
