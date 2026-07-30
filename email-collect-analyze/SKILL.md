---
name: email-collect-analyze
description: Collect, verify, archive, search, and organize Gmail or Google Workspace mailbox content. Use when a user provides or plans to provide a Google email account and app password, asks to export or back up all email content, verify that an IMAP archive is complete, preserve raw messages and attachments, incrementally sync Gmail, organize messages by contact or conversation, identify conversations that may need replies, or prepare Gmail API enrichment while continuing with app-password collection.
---

# Email Collect Analyze

Archive Gmail without mutating the mailbox. Use IMAP with an app password as the active source, preserve each complete message as raw EML, prove the declared collection scope before analysis, and keep a dormant Gmail API extension contract for future OAuth enrichment.

## Collect Required Inputs

Obtain only the missing values:

- Gmail or Google Workspace email address.
- Scope: all selectable mailboxes, normal Gmail history, named mailboxes, or a date boundary.
- Local output directory. Default to `/Volumes/Lenovo/develper_mirror/email-archives/<account_slug>/`, where the slug lowercases the complete email address and replaces every non-alphanumeric run with `_` (for example, `next.person@gmail.com` becomes `next_person_gmail_com`).

Keep one account per archive directory. Use the email-derived slug as the stable identity; do not key storage only by a person's display name, reuse another account's directory, or move/rename an established archive because manifests bind its absolute path. Keep the root outside Git and shared project `exports/` directories.

Require `/Volumes/Lenovo/develper_mirror` to be mounted before using the default. Never silently fall back to the internal disk. Use `EMAIL_ARCHIVE_ROOT` or an explicit `--output-dir` only when the user intentionally selects another root.

Use a Gmail app password, not the normal Google account password. Prefer the collector's hidden prompt. If `GMAIL_APP_PASSWORD` is already set in the local environment, use it without printing it. Never place a password in a command argument, config file, generated artifact, log, or response. If the user posts a password in chat, do not echo it; direct them to revoke it afterward and use the hidden prompt for future runs.

Read [security-and-archive-format.md](references/security-and-archive-format.md) before troubleshooting authentication, changing the storage contract, or advising on retention.

Read [complete-collection-plan.md](references/complete-collection-plan.md) before changing completeness rules, adding Gmail API support, migrating the database, or claiming that all collectable Gmail metadata is complete.

## Apply The Source Policy

- Use IMAP/app-password collection now.
- Allow analysis only for `complete`, `bounded_complete`, or `legacy_local_complete` IMAP states.
- Describe `bounded_complete` with its `--since` boundary and `legacy_local_complete` as local evidence only.
- Block `partial`, `stale`, and `failed` states unless the user explicitly authorizes `--allow-partial`.
- Always tell the user that Gmail API-only metadata is omitted from the analysis.
- Do not call the overall archive `complete` until strict IMAP and API evidence contracts both pass.

## Select Collection Scope

- Use `--mailbox auto` for the normal archive. It selects Gmail's `\All` mailbox when available and falls back to Inbox plus Sent. Gmail All Mail normally excludes Spam and Trash.
- Use `--mailbox all` when the user explicitly asks for every selectable mailbox, including Spam and Trash. Expect duplicate server fetches; the archive deduplicates messages.
- Repeat `--mailbox "NAME"` for specific mailboxes.
- Add `--since YYYY-MM-DD` for a declared bounded collection. It may produce `bounded_complete`, never full-history `complete`.
- Use `--limit` only for tests or sampling. Limited runs are always partial and block analysis by default.
- Reuse the same output directory for incremental updates. An unbounded full-scan baseline expires after 30 days; use `--reset-state` to refresh flags, current label memberships, and the baseline.

## Run The Archive Workflow

Resolve this skill's directory from the loaded `SKILL.md`, then use absolute script paths.

1. Start collection in a TTY so the app password prompt is hidden:

```bash
python3 "$SKILL_DIR/scripts/collect_gmail.py" \
  --email "USER@gmail.com" \
  --output-dir "/Volumes/Lenovo/develper_mirror/email-archives/user_gmail_com" \
  --mailbox auto
```

For an explicit complete mailbox sweep, replace `--mailbox auto` with `--mailbox all`. To inspect mailbox names without collecting, add `--list-mailboxes`.

When a fresh full-scope proof is required for an existing archive, use `--mailbox all --reset-state` with no `--since` or `--limit`. Collection writes a schema-v2 run lifecycle (`running`, `failed`, or `completed`) and binds the successful baseline to the run ID, account, archive path, mailbox set, database count, server counts, and current memberships.

2. After collection succeeds, verify local completeness:

```bash
python3 "$SKILL_DIR/scripts/verify_archive.py" \
  --archive-dir "/Volumes/Lenovo/develper_mirror/email-archives/user_gmail_com" \
  --expected-scope auto
```

`auto` inherits `all`, `normal`, or `selected` from the latest collector attempt, including a failed attempt. For a legacy archive without `manifests/imap_collection_run.json`, pass the scope that was actually collected explicitly. Never verify a selected-scope archive as `all`.

Require `analysis_allowed: true`. Interpret statuses precisely:

- `complete`: unbounded declared scope with a fresh schema-v2 baseline.
- `bounded_complete`: all matching messages since the declared date; not full history.
- `legacy_local_complete`: local EML/index integrity only; server completeness is unproven.
- `partial`, `stale`, or `failed`: do not analyze automatically.

Gmail API may remain `not_configured`; this must generate a warning but does not block verified IMAP analysis.

3. Build or refresh the derived records only after verification:

```bash
python3 "$SKILL_DIR/scripts/analyze_archive.py" \
  --archive-dir "/Volumes/Lenovo/develper_mirror/email-archives/user_gmail_com"
```

The analyzer must refuse an unverified or failed archive unless the user explicitly authorizes `--allow-partial`. When Gmail API is absent, keep the API-omission notice in the report and response.

The analyzer infers send-as identities from the Sent mailbox. If an alias is known but absent there, repeat `--identity "ALIAS@example.com"` so direction and needs-reply calculations remain accurate.

4. Confirm the derived outputs match the current verification:

```bash
python3 "$SKILL_DIR/scripts/analyze_archive.py" \
  --archive-dir "/Volumes/Lenovo/develper_mirror/email-archives/user_gmail_com" \
  --check-freshness
```

Require `status: fresh` before reading derived files.

5. Read `manifests/completeness_report.md` and `derived/archive_report.md`. Report mailbox scope, time/limit scope, collection status, API status, unique message, conversation, contact, attachment, current/historical label counts, and needs-reply counts plus artifact paths. Never imply full history from `bounded`, `selected`, `normal`, or legacy-local evidence. Treat needs-reply as a review queue, not a guaranteed action list.

If the current collection manifest has `analysis_allowed: false`, do not quote or summarize existing files under `derived/`; they may have been produced from an older verification state.

6. For semantic synthesis, start from the report and use targeted searches. Do not load an unbounded full-body JSONL into context.

## Keep Gmail API Entry Points Ready

Check whether a provider path is configured without importing it or calling Gmail:

```bash
python3 "$SKILL_DIR/scripts/gmail_api_entrypoint.py" status \
  --archive-dir "/Volumes/Lenovo/develper_mirror/email-archives/user_gmail_com"
```

Keep these future modes stable: `snapshot`, `sync`, and `verify`. Load a trusted provider from `GMAIL_API_PROVIDER_MODULE` or explicit `--provider-module`; require `run(mode, archive_dir) -> dict` and the readonly scope. Reject an API-complete claim unless it includes account, snapshot ID, verification time, granted scope, and positive profile/labels/messages/threads/drafts/history evidence. Until a provider exists, return `not_configured`.

## Search The Archive

Search metadata and bodies locally:

```bash
python3 "$SKILL_DIR/scripts/query_archive.py" \
  --archive-dir "/Volumes/Lenovo/develper_mirror/email-archives/user_gmail_com" \
  --query "pricing" \
  --limit 50 \
  --format jsonl
```

Filter with `--from-email`, `--after`, `--before`, or `--mailbox`. Add `--include-body` only when the body content is required. Synthesize conclusions from relevant records and state the filters used.

## Output Contract

Keep these paths stable across incremental runs:

- `archive.sqlite3`: canonical structured index and incremental state.
- `raw/YYYY/MM/*.eml`: complete original messages, including embedded attachments.
- `manifests/imap_collection_run.json`: latest persisted IMAP batch results for new runs.
- `manifests/imap_scope_baseline.json`: latest unbounded full-scan baseline for the exact declared mailbox set.
- `manifests/imap_full_sweep.json`: matching alias when the exact scope is `all`.
- `manifests/collection_manifest.json`: machine-readable completeness decision.
- `manifests/completeness_report.json`: complete verification evidence.
- `manifests/completeness_report.md`: operator-readable verification report and API omissions.
- `manifests/api_collection.json`: reserved Gmail API provider result.
- `derived/messages.jsonl`: normalized records and bodies for the verified analysis scope; a bounded run contains only messages observed by that run.
- `derived/conversations.jsonl`: conversation metadata and message membership.
- `derived/contacts.csv`: contact activity table.
- `derived/mailboxes.csv`: current and historically observed system/human label counts.
- `derived/archive_report.md`: mailbox overview, topics, high-frequency contacts, and review queue.
- `derived/analysis_manifest.json`: binds derived outputs to the current collection-manifest hash and each derived file hash.

Run `verify_archive.py` after every successful collection, then re-run `analyze_archive.py` only when the manifest allows analysis.

## Guardrails

- Keep Gmail access read-only. Never send, delete, label, archive, move, or mark messages as read.
- Use `BODY.PEEK[]` and read-only mailbox selection; preserve these semantics when editing the collector.
- Never commit archives, EML files, SQLite databases, exports, or credentials.
- Do not claim a semantic summary from deterministic keyword counts alone. Use targeted message evidence for conclusions.
- Do not claim Gmail API completeness from an IMAP-only archive.
- Preserve source, date, legacy, and API limitations in every affected analysis report.
- Do not silently bypass the completeness manifest; require explicit `--allow-partial` authorization.
- Do not call every inbound-ending conversation actionable; mailing lists, receipts, and automated mail can appear in the review queue.
- Keep raw EML as the source of truth. Derived JSONL, CSV, and Markdown may be regenerated.
- Stop and report the error if authentication or collection fails. Do not continue analysis on a partial run unless the user explicitly accepts partial results.
