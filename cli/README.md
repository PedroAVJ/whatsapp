# whatsapp CLI

Composable WhatsApp CLI for Codex, Claude Code, and other coding agents.

The CLI defaults to the plugin checkout that contains it. Override with `WHATSAPP_SOURCE_ROOT` only when you want the CLI to operate against another checkout.

## Install

From the repository root:

```bash
./bin/whatsapp --json doctor
ln -sf "$PWD/bin/whatsapp" "$HOME/.local/bin/whatsapp"
whatsapp --json doctor
```

## JSON Policy

Use `--json` for agent-readable output.

Success:

```json
{
  "ok": true,
  "data": {}
}
```

Error:

```json
{
  "ok": false,
  "error": {
    "code": "backend_failed",
    "message": "WhatsApp backend command failed.",
    "details": {}
  }
}
```

Chat results include both machine fields and user-facing fields:

- `display_name`: best user-facing name when available.
- `phone_number`: present for direct WhatsApp chats when the phone identity is available, including LID-backed chats resolved through WhatsApp's local LID map.
- `chat_type`: user-facing conversation type: `direct`, `group`, `broadcast`, or `unknown`.
- `identifier_type`: backing WhatsApp identifier type: `phone`, `lid`, `group`, `broadcast`, or `unknown`.
- `name_quality`: whether the name came from a resolved contact, LID map, phone-number fallback, group name, or unresolved LID.

`chats list` collapses direct phone/LID identity duplicates by `phone_number` by default. The returned `jid` remains the newest backing chat identity for follow-up reads. Use `--include-identity-duplicates` only when debugging raw WhatsApp identity rows.

Use `display_name`, `phone_number`, and `chat_type` in summaries. Keep raw `jid` values for follow-up CLI calls, not user-facing explanations.

Message results may include `reactions`, `receipts`, and `seen_by` arrays when
the bridge has observed those events. `seen_by` is derived only from `read`
receipts; it does not mean online presence or last-active state.

## Common Commands

```bash
whatsapp --json doctor
whatsapp --json bridge status
whatsapp --json bridge start
whatsapp bridge relink --confirm
whatsapp --json chats list --limit 20 --no-last-message
whatsapp --json chats list --limit 20 --no-last-message --include-identity-duplicates
whatsapp --json chats list --query "project name" --no-last-message
whatsapp --json contacts search "Alice"
whatsapp --json chats get "15551234567@s.whatsapp.net"
whatsapp --json messages list --chat-jid "15551234567@s.whatsapp.net" --limit 30
whatsapp --json messages context MESSAGE_ID --before 5 --after 5
whatsapp --json media download MESSAGE_ID "15551234567@s.whatsapp.net"
whatsapp --json media transcribe MESSAGE_ID "15551234567@s.whatsapp.net" --language es
whatsapp --json media transcripts list --chat-jid "15551234567@s.whatsapp.net"
whatsapp --json media transcripts show MESSAGE_ID --chat-jid "15551234567@s.whatsapp.net"
whatsapp --json drafts create --chat-jid "15551234567@s.whatsapp.net" --text "Thanks, received."
printf 'Line 1\n\nLine 2\n' | whatsapp --json drafts create --chat-jid "15551234567@s.whatsapp.net" --text-stdin
whatsapp --json drafts create --chat-jid "15551234567@s.whatsapp.net" --text-file ./reply.txt
whatsapp --json drafts list
whatsapp --json drafts show DRAFT_ID
whatsapp --json drafts update DRAFT_ID --text "Thanks, received. I sent it by email."
printf 'Thanks, received.\nI sent it by email.\n' | whatsapp --json drafts update DRAFT_ID --text-stdin
whatsapp --json drafts send DRAFT_ID --dry-run
whatsapp --json drafts send DRAFT_ID --confirm
whatsapp --json messages send --chat-jid "15551234567@s.whatsapp.net" --text "Thanks" --dry-run
printf 'Thanks\n\nI checked it.\n' | whatsapp --json messages send --chat-jid "15551234567@s.whatsapp.net" --text-stdin --dry-run
whatsapp --json messages send --chat-jid "15551234567@s.whatsapp.net" --text "Thanks" --confirm
whatsapp --json messages send --chat-jid "15551234567@s.whatsapp.net" --reply-to-message-id MESSAGE_ID --text "Thanks" --dry-run
```

## Drafts And Sending

Drafts are local CLI drafts, not native WhatsApp UI drafts. They are stored in `drafts.db` next to the bridge's message database, or at `WHATSAPP_DRAFTS_DB_PATH` when that environment variable is set. A local draft does not create the green `Draft:` label inside WhatsApp itself.

Live sends are guarded:

- `messages send` and `drafts send` require either `--dry-run` or `--confirm`.
- Use `--dry-run` before live sends in automation or agent workflows.
- `--confirm` sends through the existing local bridge `/api/send` endpoint.
- `--reply-to-message-id` sends a WhatsApp quoted reply. The CLI validates that the quoted message exists in the same chat before dry-run or live send.
- WhatsApp Status/Broadcast targets are rejected.
- For multiline text, use `--text-stdin` or `--text-file`. Literal `\n` or `\r` sequences in `--text` are preserved as literal characters, surfaced as warnings, and blocked on live sends unless `--allow-literal-escapes` is passed.

## Audio Transcripts

`media transcribe` is opt-in for one requested audio message. It downloads that
message's media, calls the ElevenLabs Scribe helper only on a cache miss, and
stores the transcript in `transcripts.db` next to the bridge message database
or at `WHATSAPP_TRANSCRIPTS_DB_PATH` when set.

Local state is private by construction and on migration: state and transcript
directories use mode `0700`, while databases, SQLite sidecars, lock files, and
transcript files use `0600`.

Use `--refresh` to force a new ElevenLabs call. Do not use `messages list` as a
transcription trigger; listings only reveal which messages have audio.

## Guardrails

- Reads and local drafts are safe by default.
- Live WhatsApp sends require an exact chat JID and `--confirm`.
- Live sends refuse text containing literal backslash escape sequences like `\n` by default. Use `--text-stdin` or `--text-file` for real newlines.
- Bridge setup and reset are available for recovery only.
- `bridge reset-sync` requires `--confirm` because it deletes local sync state.
- `bridge relink --confirm` resets local state, runs linked-device pairing setup, and starts the durable bridge after successful pairing.
- Fresh pairing requests the maximum linked-device history WhatsApp permits and waits for its full-history stream before starting the durable bridge. WhatsApp still bounds linked-device history, so this is not an account-complete archive.
- Use QR only when it can be shown on a separate screen. When the user is remote on the same phone that authorizes the link, run `WHATSAPP_USE_PHONE_PAIRING=1 whatsapp bridge relink --confirm`, then use WhatsApp -> Linked devices -> Link a device -> Link with phone number instead. Phone pairing is preflighted before reset and never silently falls back to QR.
- Use `messages send --confirm` or `drafts send --confirm` only for a message whose exact recipient and exact text the user approved. Delegation selects an agent-disclosed template; it does not authorize automatic sends or follow-ups. Prefix every agent-authored delegated message with `⌘` for Codex or `✳️` for Claude, while keeping user-voice messages unmarked. Never use delegated mode to impersonate the user or cross a sensitive, financial, scheduling, legal, medical, or scope-changing gate.
