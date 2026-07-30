# Gmail Complete Collection Plan

## Contents

1. [Objective](#objective)
2. [Completeness Levels](#completeness-levels)
3. [Current IMAP-First Policy](#current-imap-first-policy)
4. [Data Coverage Matrix](#data-coverage-matrix)
5. [Collection Workflow](#collection-workflow)
6. [IMAP Completeness Contract](#imap-completeness-contract)
7. [Reserved Gmail API Contract](#reserved-gmail-api-contract)
8. [Runtime Output Contract](#runtime-output-contract)
9. [Analysis Gate](#analysis-gate)
10. [Future Database Model](#future-database-model)
11. [Migration Of Existing Archives](#migration-of-existing-archives)
12. [Acceptance Criteria](#acceptance-criteria)
13. [Known Hard Limits](#known-hard-limits)
14. [Implementation Checklist](#implementation-checklist)

## Objective

Collect every mailbox item and every metadata field that the configured read-only source exposes, preserve complete raw messages, prove local integrity, and distinguish source-specific completeness from absolute completeness.

Do not use a successful process exit as proof of completeness. Generate a machine-readable manifest and an operator-readable report after every collection.

## Completeness Levels

Use these IMAP statuses:

- `complete`: An unbounded schema-v2 baseline proves the exact declared mailbox set and is no more than 30 days old.
- `bounded_complete`: Every UID matching the declared `--since` boundary was processed; this is not full history.
- `legacy_local_complete`: Local EML, hashes, indexes, and observed memberships pass, but server-level completeness is unproven.
- `partial`: The run is limited, evidence is missing, or a critical integrity check failed.
- `stale`: The bound full-scan baseline is older than the allowed age.
- `failed`: The latest collection attempt is running, interrupted, or failed.

Use `imap_complete_api_pending`, `imap_bounded_complete_api_pending`, or `imap_legacy_local_complete_api_pending` while API evidence is absent. Use overall `complete` only when strict IMAP and API contracts both pass.

Treat completeness as relative to official, read-only interfaces. Never claim access to Google-internal fields that are not publicly exposed.

## Current IMAP-First Policy

Use Gmail IMAP with an app password as the active collection method.

- Open mailboxes with `readonly=True`.
- Fetch messages with `BODY.PEEK[]`.
- Use `--mailbox all` for an explicit complete sweep, including Spam and Trash.
- Preserve `UID`, `UIDVALIDITY`, `FLAGS`, `INTERNALDATE`, `X-GM-MSGID`, and `X-GM-THRID`.
- Preserve every complete RFC message as `.eml`.
- Store decoded text and HTML plus attachment metadata in SQLite.
- Deduplicate across labels by `X-GM-MSGID`.
- Verify the archive before analysis.

When IMAP verification passes and Gmail API is not configured, set:

```json
{
  "collection_status": "imap_complete_api_pending",
  "imap_status": "complete",
  "api_status": "not_configured",
  "analysis_allowed": true
}
```

Always tell the user that analysis omits Gmail API-only metadata.

## Data Coverage Matrix

| Data family | Active IMAP/app-password collection | Reserved Gmail API enrichment |
|---|---|---|
| Account | Account address used for collection | Profile email, message total, thread total, current history ID |
| Message identity | `X-GM-MSGID`, RFC Message-ID, SHA-256 archive ID fallback | Canonical API message ID and history ID |
| Conversation identity | `X-GM-THRID`, References, normalized-subject fallback | Canonical API thread ID, snippet, ordered message membership |
| Headers and addresses | Complete RFC headers in raw EML; normalized From, To, Cc, Bcc, subject, date | API FULL payload headers for reconciliation |
| Bodies and MIME | Complete raw EML, decoded plain text and HTML, MIME types | API RAW/FULL, MIME part IDs, attachment IDs, API body sizes |
| Attachments | Attachment bytes preserved inside EML; filename, type, disposition, content ID, size indexed | Canonical attachment IDs and complete MIME-tree reconciliation |
| Labels and mailboxes | All selectable system and human-created label mailboxes, UID, UIDVALIDITY, special-use flags, current membership, historical membership, observed removal time | Canonical label IDs, name, type, visibility, color, message/thread counts, per-message label IDs |
| Dates and size | Header date, IMAP INTERNALDATE, raw byte size | API internalDate and sizeEstimate |
| Drafts | Draft messages visible through IMAP as messages | Draft resource ID and draft-to-message mapping |
| Change history | Current versus historically observed IMAP memberships after full rescans | Added/deleted messages, added/removed labels, history checkpoint |
| Search and analysis | Local messages, contacts, conversations, attachments, label membership, reply-review queue | Same analysis enriched with canonical Gmail metadata |

The raw EML is the content-fidelity layer. Gmail API enrichment is the Gmail-native metadata layer. Neither source exposes the private/internal fields listed under [Known Hard Limits](#known-hard-limits).

## Collection Workflow

Run the active workflow in this order:

1. Acquire the archive lock and persist a schema-v2 `running` manifest before mailbox access.
2. Collect the declared mailboxes, updating current and historical memberships only within a completed full-scan scope.
3. On interruption write `failed`; on success bind `completed` to run ID, account, paths, options, selected mailboxes, server counts, membership counts, and database total.
4. Verify database rows, raw files, hashes, MIME parsing, run binding, mailbox state, scope baseline, and membership coverage.
5. Persist `imap_scope_baseline.json` after any unbounded full scan and `imap_full_sweep.json` when that exact scope is `all`.
6. Write `collection_manifest.json` and the completeness reports.
7. Continue to analysis only when `analysis_allowed` is true, then verify analysis-manifest freshness.
8. Add the API-omission warning whenever API evidence is incomplete.

For incremental runs:

1. Reuse the same archive directory.
2. Fetch new UIDs.
3. Refresh the full-scan baseline at least every 30 days to reconcile existing flags and removed memberships.
4. Re-run verification.
5. Refresh analysis only after verification.

## IMAP Completeness Contract

Require all critical checks:

- The messages table is readable; an empty mailbox may verify, but analysis remains blocked because it has no content.
- Every database message points to an existing raw EML file.
- Every raw EML is referenced by exactly one canonical message.
- Stored byte size equals the raw file size.
- Stored SHA-256 equals the raw file SHA-256.
- Every raw message parses successfully.
- Every message has `X-GM-MSGID`, a thread key, and an internal or header date. Report a missing sender as non-critical data quality because valid drafts or malformed mail may omit it.
- Every message belongs to at least one mailbox or label.
- All expected special mailboxes have collection state. For a full sweep, require All Mail, Spam, and Trash state even when empty.
- Standard attachment metadata count equals MIME parts with a filename or attachment disposition.
- No batch reports missing fetched UIDs.
- The selected mailbox list, per-mailbox result list, and persisted mailbox-state list agree.
- Run ID, account, archive path, database path, mailbox scope, coverage mode, and database count agree.
- Limited runs are always partial. Bounded runs display the date boundary.
- For an unbounded baseline, every selected mailbox uses a full scan and `processed == candidate_uids == server_message_count == current_membership_count`.
- The exact-scope baseline is no more than 30 days old.

Incremental runs refresh new UIDs but do not replace the exact-scope baseline. Run `--mailbox all --reset-state` when a fresh full-scope proof or current label reconciliation is required.

Report non-critical structured-index omissions separately. Examples include AMP Email, reaction JSON, calendar bodies, or other MIME leaf types that remain preserved in raw EML.

## Reserved Gmail API Contract

Keep the following command surface stable even while API execution is disabled:

```bash
python3 scripts/gmail_api_entrypoint.py status --archive-dir ARCHIVE
python3 scripts/gmail_api_entrypoint.py snapshot --archive-dir ARCHIVE
python3 scripts/gmail_api_entrypoint.py sync --archive-dir ARCHIVE
python3 scripts/gmail_api_entrypoint.py verify --archive-dir ARCHIVE
```

Load the future provider from `GMAIL_API_PROVIDER_MODULE`. The provider must expose:

```python
def run(mode: str, archive_dir: pathlib.Path) -> dict:
    ...
```

Until that provider exists, return `not_configured` and never imply that API metadata was collected.

Do not trust a provider's status string alone. A complete API manifest must include account, snapshot ID, verification time, `gmail.readonly` in granted scopes, and positive evidence for profile, labels, messages, threads, drafts, and history. Downgrade unsupported completeness claims to `partial`.

Implement these read-only Gmail API operations in the future provider:

| Phase | Gmail API operations | Required data |
|---|---|---|
| Profile | `users.getProfile` | email, message total, thread total, history ID |
| Labels | `users.labels.list/get` | ID, name, type, visibility, color, message/thread counts |
| Messages | `users.messages.list/get` | all IDs including Spam/Trash, RAW, FULL, label IDs, snippet, history ID, internal date, size |
| Threads | `users.threads.list/get` | thread ID, snippet, history ID, ordered members |
| Drafts | `users.drafts.list/get` | draft ID and message mapping |
| History | `users.history.list` | added/deleted messages and added/removed labels |

Use only `https://www.googleapis.com/auth/gmail.readonly`. Do not request modify, compose, send, or delete permissions.

Official references:

- [Gmail synchronization guide](https://developers.google.com/workspace/gmail/api/guides/sync)
- [Gmail Message resource](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages)
- [Gmail Label resource](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.labels)
- [Gmail History API](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list)
- [Gmail OAuth scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)

## Runtime Output Contract

Maintain this layout:

```text
email-archive/
├── archive.sqlite3
├── raw/YYYY/MM/*.eml
├── source/
│   ├── profile.json
│   ├── labels.json
│   ├── messages-api.jsonl.gz
│   ├── threads-api.jsonl.gz
│   └── drafts-api.jsonl.gz
├── blobs/sha256/...
├── events/history.jsonl
├── manifests/
│   ├── collection_manifest.json
│   ├── imap_collection_run.json
│   ├── imap_scope_baseline.json
│   ├── imap_full_sweep.json
│   ├── completeness_report.json
│   ├── completeness_report.md
│   └── api_collection.json
└── derived/
    ├── messages.jsonl
    ├── conversations.jsonl
    ├── contacts.csv
    ├── mailboxes.csv
    ├── analysis_manifest.json
    └── archive_report.md
```

The active IMAP method writes `archive.sqlite3`, `raw/`, `manifests/`, and `derived/`. Reserve `source/`, `blobs/`, `events/`, and the API manifest for the future provider.

## Analysis Gate

Allow normal analysis when:

```text
imap_status in {complete, bounded_complete, legacy_local_complete}
and analysis_scope_message_count > 0
```

For `bounded_complete`, build every derived message, conversation, contact, and label count only from memberships observed by the bound run ID. Older records remain in the canonical archive but must not leak into that bounded analysis.

Do not require Gmail API for current analysis. Instead, add this warning:

```text
This analysis is based on a verified IMAP/app-password snapshot. Gmail API-only
metadata was not collected, including canonical label IDs and properties,
history events, draft resource IDs, API snippets, and API size estimates.
```

Refuse analysis for `partial`, `stale`, or `failed` unless the user explicitly authorizes partial analysis. Require `analysis_manifest.json` to match the current collection-manifest SHA-256 before reading derived outputs.

## Future Database Model

Add these tables during the API implementation:

- `accounts`
- `sync_runs`
- `messages`
- `message_headers`
- `mime_parts`
- `blobs`
- `message_blobs`
- `labels`
- `message_labels`
- `threads`
- `thread_messages`
- `drafts`
- `history_events`
- `imap_mailboxes`
- `imap_memberships`
- `completeness_checks`

Store source-native IDs and observation timestamps. Do not overwrite raw history when a message or label is later removed; mark the current state and preserve the observed event.

## Migration Of Existing Archives

Preserve current EML and SQLite content.

1. Preserve v1 archives as `legacy_local_complete` until an app-password full rescan creates schema-v2 run and membership evidence.
2. Add future API tables without dropping v1 tables.
3. Convert decimal `X-GM-MSGID` values to Gmail API hexadecimal message IDs.
4. Match API IDs to existing raw EML.
5. Compare API RAW bytes with current EML SHA-256.
6. Backfill labels, message metadata, threads, drafts, and the history baseline.
7. Extract every MIME leaf and content-addressed blob.
8. Re-run verification.
9. Keep the existing archive when hashes match; quarantine discrepancies instead of overwriting evidence.

## Acceptance Criteria

Mark strict IMAP complete only when every local, run-binding, exact-scope baseline, current-membership, and age check passes. Mark bounded and legacy-local results explicitly; never collapse them into strict complete.

Mark API complete only when:

- The paginated API message ID set equals the local active API message set.
- Every message has RAW and FULL results.
- Every API label ID resolves to a stored label.
- Draft and thread ID sets reconcile.
- Every MIME leaf is indexed or has an explicit unsupported reason.
- The history checkpoint has no pending pages.
- A repeated run is idempotent.
- No OAuth token or app password appears in files or logs.

Mark overall complete only when both required source contracts pass.

## Known Hard Limits

Do not claim to collect:

- Messages permanently deleted before the first snapshot.
- Hidden envelope recipients absent from the RFC message.
- The person who applied a label.
- The exact historical label application time before observation began.
- Expired Gmail history records.
- Google-internal spam scores, trust signals, or user read timestamps.
- Any private Gmail field not exposed by an official read-only interface.

## Implementation Checklist

Current IMAP phase:

- [x] Preserve full raw EML.
- [x] Store normalized messages and mailbox memberships.
- [x] Support full and incremental IMAP collection.
- [x] Persist running/failed/completed attempts and prevent concurrent collectors.
- [x] Distinguish unbounded, bounded, limited, and legacy-local evidence.
- [x] Reconcile current versus historical label memberships on full scans.
- [x] Reserve Gmail API command surface.
- [x] Verify local archive completeness.
- [x] Bind and age exact-scope baselines for complete runs.
- [x] Gate analysis on IMAP verification.
- [x] Bind derived outputs to the collection-manifest hash.
- [x] Warn about API omissions.

Future Gmail API phase:

- [ ] Add OAuth readonly provider.
- [ ] Implement profile, labels, messages RAW/FULL, threads, and drafts.
- [ ] Implement history synchronization and 404 full-sync fallback.
- [ ] Add complete MIME leaf and blob storage.
- [ ] Migrate existing archives to the v2 schema.
- [ ] Cross-check API, IMAP, and raw EML identifiers and hashes.
- [ ] Forward-test snapshot, sync, stale-history, and partial-failure flows.
