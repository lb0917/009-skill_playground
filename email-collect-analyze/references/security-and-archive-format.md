# Security And Archive Format

## Gmail Authentication

- Use a Google app password when the account supports it. App passwords normally require two-step verification and may be unavailable under some Google Workspace policies.
- Enter the app password through the collector's hidden prompt, or set it in `GMAIL_APP_PASSWORD` for the current process.
- Do not use a normal Google account password. Do not add a `--password` command-line option.
- If Google rejects IMAP login, verify the email address, app password, Workspace administrator policy, and whether the account allows IMAP clients.
- Revoke an app password after one-time migration work or whenever it may have been exposed.

## Read-Only Guarantees

The collector opens mailboxes with `readonly=True` and fetches messages with `BODY.PEEK[]`. These choices prevent the workflow from intentionally changing flags or marking messages as read. Preserve both behaviors.

The workflow does not send, delete, move, label, or archive Gmail messages. It only creates local files.

## Canonical Storage

Store account roots under `/Volumes/Lenovo/develper_mirror/email-archives/<account_slug>/`. Derive the slug from the complete lowercased email address by replacing every non-alphanumeric run with `_`. Keep one account per directory and never use a display name alone as the storage key. Once collection starts, keep that absolute directory stable because collection and completeness manifests bind it. Refuse the default when the Lenovo volume is not mounted instead of falling back to internal storage.

The configured Lenovo volume uses ExFAT with `noowners`. Unix mode bits such as `0700` and `0600` do not provide reliable confidentiality on this filesystem. Treat physical access to the drive as access to the archive, and use an encrypted container or encrypted volume if that risk is unacceptable.

`archive.sqlite3` contains:

- `messages`: one normalized row per unique Gmail message.
- `mailbox_membership`: Gmail mailbox and UID membership for each collected message.
- `mailbox_state`: UIDVALIDITY, last processed UID, and IMAP special-use flags for incremental runs.

Membership rows distinguish current (`is_current=1`) from historically observed relationships. A completed unbounded full scan marks missing UIDs non-current and records `removed_at`; bounded or limited scans never remove unseen older memberships.

Each `messages.raw_path` points to a complete `.eml` under `raw/YYYY/MM/`. Raw EML is the fidelity layer and includes MIME parts and attachment bytes. The database stores decoded text and HTML bodies plus attachment metadata for analysis.

`archive_id` prefers Gmail's stable `X-GM-MSGID`, then the RFC Message-ID, then a SHA-256 fallback. A single archive directory belongs to one Gmail account; use separate directories for separate accounts.

## Derived Storage

`analyze_archive.py` atomically replaces the files under `derived/`:

- `messages.jsonl` contains normalized records for the verified analysis scope, including decoded bodies. For bounded verification it contains only messages observed by the bound run ID, not older records retained in the archive.
- `conversations.jsonl` groups messages by Gmail thread ID or a deterministic fallback.
- `contacts.csv` contains message, direction, thread, date, and subject counts.
- `archive_report.md` contains deterministic overview statistics and a review queue.
- `mailboxes.csv` contains system/human label counts for current and historically observed memberships.
- `analysis_manifest.json` binds the derived outputs to the completeness-manifest SHA-256 and records each derived file hash.

The `needs_reply` flag only means the latest collected message in a conversation is inbound. It is not semantic intent classification.

## Completeness Manifests

The collector writes:

- `manifests/imap_collection_run.json`: the latest IMAP attempt, lifecycle status, and effective scope.
- `manifests/imap_full_sweep.json`: the latest unbounded all-mailbox proof retained across incremental runs.
- `manifests/imap_scope_baseline.json`: the latest unbounded full-scan proof for the exact mailbox set.

The run manifest is written as `running` before mailbox access, updated to `failed` on interruption, and changed to `completed` only after every selected mailbox succeeds. A filesystem lock prevents concurrent collectors from sharing one archive.

The verifier writes:

- `manifests/collection_manifest.json`: the machine-readable collection decision.
- `manifests/completeness_report.json`: the full verification evidence.
- `manifests/completeness_report.md`: the operator-readable report.

Allow analysis for `complete`, `bounded_complete`, or `legacy_local_complete` when the archive contains messages. Always display mailbox scope plus unbounded/bounded/limited coverage. A legacy-local result proves local integrity only. Limited, partial, stale, and failed results block analysis unless the user explicitly overrides the gate.

Reserve `manifests/api_collection.json` for the future OAuth readonly provider. See `complete-collection-plan.md` for the provider contract and migration rules.

## Retention And Handling

- Treat the archive as sensitive personal data. Store it on an encrypted device with restrictive filesystem permissions.
- Keep archives outside source repositories and synced public folders.
- Opening EML attachments can be unsafe. Scan or inspect untrusted attachments before opening them.
- Delete the entire archive directory when retention is no longer justified. Deleting derived files alone does not delete raw message content.
- Back up raw EML and SQLite together. Derived files can be regenerated.
