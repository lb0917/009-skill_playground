#!/usr/bin/env python3
"""Stable Gmail API extension entrypoint.

The active skill uses IMAP with an app password. A future OAuth provider can be
added without changing the CLI or archive contract by exposing a run() method.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from archive_contract import (
    API_COMPLETE_STATUSES,
    GMAIL_READONLY_SCOPE,
    api_evidence_errors,
)

ENDPOINT_CONTRACT = {
    "profile": ["users.getProfile"],
    "labels": ["users.labels.list", "users.labels.get"],
    "messages": ["users.messages.list", "users.messages.get:RAW", "users.messages.get:FULL"],
    "threads": ["users.threads.list", "users.threads.get"],
    "drafts": ["users.drafts.list", "users.drafts.get"],
    "history": ["users.history.list"],
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
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
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
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


def load_provider(reference: str) -> ModuleType:
    candidate = Path(reference).expanduser()
    if candidate.suffix == ".py" or candidate.exists():
        if not candidate.exists():
            raise RuntimeError(f"Gmail API provider file not found: {candidate}")
        spec = importlib.util.spec_from_file_location("email_collect_analyze_gmail_api_provider", candidate.resolve())
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Unable to load Gmail API provider: {candidate}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    return importlib.import_module(reference)


def status_payload(archive_dir: Path, provider_reference: str | None) -> dict[str, Any]:
    configured = bool(provider_reference)
    return {
        "status": "configured" if configured else "not_configured",
        "generated_at": utc_now(),
        "archive_dir": str(archive_dir),
        "provider_module": provider_reference,
        "provider_contract_checked": False,
        "analysis_blocking": False,
        "required_scope": GMAIL_READONLY_SCOPE,
        "supported_modes": ["status", "snapshot", "sync", "verify"],
        "endpoint_contract": ENDPOINT_CONTRACT,
        "provider_contract": "run(mode: str, archive_dir: pathlib.Path) -> dict",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reserved Gmail API collection entrypoint")
    parser.add_argument("mode", choices=("status", "snapshot", "sync", "verify"))
    parser.add_argument("--archive-dir", type=Path, required=True)
    parser.add_argument(
        "--provider-module",
        default=os.environ.get("GMAIL_API_PROVIDER_MODULE"),
        help="Future OAuth provider module name or Python file path",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    archive_dir = args.archive_dir.expanduser().resolve()
    status = status_payload(archive_dir, args.provider_module)
    if args.mode == "status":
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0
    if status["status"] != "configured":
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 2

    try:
        provider = load_provider(args.provider_module)
        if not callable(getattr(provider, "run", None)):
            raise RuntimeError("Provider does not expose run(mode, archive_dir)")
        result = provider.run(args.mode, archive_dir)
        if not isinstance(result, dict):
            raise RuntimeError("Gmail API provider run() must return a dict")
    except Exception as error:
        failure = {
            "status": "failed",
            "generated_at": utc_now(),
            "mode": args.mode,
            "archive_dir": str(archive_dir),
            "provider_module": args.provider_module,
            "error": str(error),
        }
        atomic_write_json(archive_dir / "manifests" / "api_collection.json", failure)
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        return 1

    result.setdefault("status", "partial")
    result.setdefault("generated_at", utc_now())
    result.setdefault("mode", args.mode)
    result.setdefault("archive_dir", str(archive_dir))
    result.setdefault("provider_module", args.provider_module)
    result.setdefault("required_scope", GMAIL_READONLY_SCOPE)
    result.setdefault("endpoint_contract", ENDPOINT_CONTRACT)
    if result.get("status") in API_COMPLETE_STATUSES:
        evidence_errors = api_evidence_errors(result)
        if evidence_errors:
            result["claimed_status"] = result["status"]
            result["status"] = "partial"
            result["evidence_errors"] = evidence_errors
    atomic_write_json(archive_dir / "manifests" / "api_collection.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in API_COMPLETE_STATUSES else 1


if __name__ == "__main__":
    raise SystemExit(main())
