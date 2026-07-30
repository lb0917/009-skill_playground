#!/usr/bin/env python3
"""Shared status and evidence contract for email-collect-analyze scripts."""

from __future__ import annotations

from typing import Any


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
API_COMPLETE_STATUSES = {"complete", "api_complete"}
REQUIRED_API_EVIDENCE = (
    "profile_complete",
    "labels_complete",
    "messages_complete",
    "threads_complete",
    "drafts_complete",
    "history_complete",
)

API_OMISSIONS = [
    "canonical Gmail label IDs and label properties",
    "message and thread history IDs",
    "message-added, message-deleted, label-added, and label-removed history events",
    "Gmail draft resource IDs",
    "Gmail API snippets and size estimates",
    "Gmail API MIME part and attachment IDs",
    "Google Workspace classification labels when applicable",
]

HARD_LIMITS = [
    "messages permanently deleted before the first snapshot",
    "hidden envelope recipients absent from the RFC message",
    "the actor who applied a label",
    "historical label application times before observation began",
    "expired Gmail history records",
    "Google-internal spam, trust, and user-read signals",
]


def api_evidence_errors(manifest: dict[str, Any] | None) -> list[str]:
    """Return reasons a claimed API-complete manifest is not independently usable."""
    if not manifest:
        return ["API manifest is missing"]
    errors: list[str] = []
    if manifest.get("status") not in API_COMPLETE_STATUSES:
        errors.append("API status is not complete")
    if not manifest.get("account"):
        errors.append("API account is missing")
    if not manifest.get("snapshot_id"):
        errors.append("API snapshot_id is missing")
    if not manifest.get("verified_at"):
        errors.append("API verified_at is missing")
    granted_scopes = manifest.get("granted_scopes") or []
    if GMAIL_READONLY_SCOPE not in granted_scopes:
        errors.append("gmail.readonly is not recorded in granted_scopes")
    evidence = manifest.get("evidence") or {}
    for key in REQUIRED_API_EVIDENCE:
        if evidence.get(key) is not True:
            errors.append(f"API evidence is missing or false: {key}")
    return errors
