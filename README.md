# WhatsApp

An agent-first WhatsApp plugin and CLI for Codex and Claude Code. The
installed `whatsapp` CLI is the stable front door for every WhatsApp request;
agents should use it directly instead of computer control, browser automation,
or searching for a separate native tool. The plugin combines the live
mechanism, automatic voice-note transcription, guarded user-voice drafts,
approval-gated disclosed messages, and source-hygiene review.

This is not a thin MCP wrapper. It packages a local WhatsApp bridge, a SQLite-backed message store, a stable `whatsapp --json ...` CLI, local reviewable drafts, and guarded live sends. Person-scoped outbound work resolves identity through live Apple Contacts and exact phone matching before a live send. Read-only lookup may identify the relevant conversation from bounded recent chat metadata and message context. User-voice sends require approval of the exact recipient and text. A clear one-message instruction to send, tell, ask, reply, or let a recipient know something authorizes one agent-composed disclosed message even when the user leaves the exact wording to the agent. Draft-only requests and broad continuing delegation do not authorize live sends or future follow-ups. Any direct chat the user identifies as their girlfriend, wife, spouse, or romantic partner defaults to the disclosed agent template and marker; user-voice mode requires an explicit objective-scoped opt-in, and the template default never permits automatic sending.

The bridge is based on a patched vendored copy of [`lharries/whatsapp-mcp`](https://github.com/lharries/whatsapp-mcp). Native MCP registration is intentionally disabled by default because direct CLI calls are more reliable for coding agents and avoid tool-routing collisions.

This project is unofficial and is not affiliated with WhatsApp or Meta.

## What You Get

- Local WhatsApp linked-device bridge.
- SQLite-backed reads over contacts, chats, messages, reactions, read receipts,
  context, and media.
- A composable JSON CLI designed for agents: `whatsapp --json ...`.
- Arrival-triggered ElevenLabs transcription of incoming audio with a local SQLite
  transcript cache, so voice notes are already readable before an agent asks.
- Local draft records before sending.
- Live-send guardrails: `--dry-run` or explicit `--confirm` is required.
- Codex and Claude Code plugin metadata.

## Requirements

- macOS or Linux
- Go
- Python 3
- Node.js and pnpm
- `uv` for the vendored Python MCP backend
- an installed `elevenlabs` CLI for arrival-triggered audio transcription
- the `contacts@package-manager` Apple Contacts plugin for person and
  relationship identity resolution
- the `writing@package-manager` plugin for canonical user-voice wording

## Install

```bash
git clone https://github.com/PedroAVJ/whatsapp.git
cd whatsapp
pnpm install
pnpm test
```

Run the CLI from the repo:

```bash
./bin/whatsapp --json doctor
```

Optional: put the CLI on your PATH with your package manager or a symlink:

```bash
ln -sf "$PWD/bin/whatsapp" "$HOME/.local/bin/whatsapp"
whatsapp --json doctor
```

## Link WhatsApp

First-time setup uses WhatsApp's linked-device flow.

```bash
pnpm setup
```

Open WhatsApp on your phone, then use Linked devices -> Link a device and scan the QR code printed by the setup command. The QR is also written under the local state directory:

```text
~/.local/share/codex-whatsapp/upstream-qr.png
```

Start the bridge after a successful link:

```bash
pnpm start
whatsapp --json bridge status
```

When the user is remote on the same phone that must authorize the link, use
phone-number pairing immediately instead of asking them to scan a QR displayed
on that phone:

```bash
WHATSAPP_USE_PHONE_PAIRING=1 WHATSAPP_MCP_PAIR_PHONE=15551234567 pnpm setup
```

`whatsapp bridge relink --confirm` can resolve the current account number from
the existing private linked-device store before reset, so the normal remote
relink command is:

```bash
WHATSAPP_USE_PHONE_PAIRING=1 whatsapp bridge relink --confirm
```

It refuses to delete the existing sync state if the phone cannot be resolved,
and an explicitly requested phone pairing never silently falls back to QR.

Fresh setup requests the maximum linked-device history WhatsApp permits and
keeps the companion connected until the full-history stream completes, or for
ten minutes if WhatsApp never sends a completion marker. This is not an
account-complete archive: WhatsApp sends linked devices only a bounded recent
history, and older messages may remain available only on the primary phone.

## Use With Codex

Install the user's unified catalog as a Codex marketplace, then install the `whatsapp` plugin from the Plugins screen.

```bash
codex plugin marketplace add PedroAVJ/package-manager --ref main
codex plugin marketplace upgrade
```

In the Codex app, the equivalent Add marketplace values are:

```text
Source: PedroAVJ/package-manager
Git ref: main
Sparse paths: (leave blank)
```

After installation, use the `whatsapp` skill. The skill tells agents to prefer metadata-only discovery first, then read targeted message context only when needed.

## Identity Resolution

Apple Contacts is the identity and relationship authority for direct-person
outbound WhatsApp work. Before a live send, the agent reads My Card for self references, reads directional
relationship labels for terms such as partner or boss, and reads an exact card
for named people. It then requires an exact normalized full-phone match to one
direct WhatsApp chat. Explicit international values remain authoritative. For
an unprefixed national number, the Contacts plugin may provide a full `e164`
candidate marked `default_country`; WhatsApp accepts that candidate only when
exactly one direct chat matches the resulting country code and national number.
Duplicate contact fields that normalize to the same `e164` value count as one
candidate.
Display names, nicknames, partial phone suffixes, and search order are never
sufficient to authorize a live send.

Read-only requests do not require the Contacts identity gate. For requests
about a latest or recent message from a described person or role, the agent
uses recent chat metadata and the minimum bounded message context needed to
identify the relevant conversation. A missing relationship label does not
block read-only inspection; the agent asks only when multiple plausible chats
remain after that bounded inspection.

Boss, manager, supervisor, and jefe map to Apple's standard `manager` label.
Multiple manager relationships are valid; repository or task context may choose
only among contacts already carrying that label. The agent asks when more than
one labeled manager still fits rather than inventing a relationship. Canonical
phone comparison also recognizes WhatsApp's historical Mexican `+521` mobile
form as the same current `+52` number only when all ten national digits match.
Agents never invent a country code from a suffix themselves; inferred values
must carry the Contacts plugin's `default_country` provenance.

Self-chat sends additionally verify the target against the bridge's registered
account JID after removing only a linked-device suffix. If My Card has no phone,
the bridge account may identify the exact self-chat, but the agent must report
that Apple Contacts could not corroborate the number and must never fall back
to a chat named the user, You, or Self. Missing, unmatched, or ambiguous relationship
labels fail closed for outbound delivery instead of being inferred from familiar
or recent chats; they do not block read-only inspection.

The plugin keeps personal mappings out of source and re-resolves live Contacts
and WhatsApp metadata for each outbound objective.

Useful commands:

```bash
whatsapp --json chats list --limit 20 --no-last-message
whatsapp --json chats list --query "Alice" --limit 10 --no-last-message
whatsapp --json messages list --chat-jid "15551234567@s.whatsapp.net" --limit 30
whatsapp --json messages list --after "2026-08-11T12:00:00Z" --before "2026-08-12T12:00:00Z"
whatsapp --json messages context MESSAGE_ID --before 5 --after 5
whatsapp --json media download MESSAGE_ID "15551234567@s.whatsapp.net"
whatsapp --json media transcribe MESSAGE_ID "15551234567@s.whatsapp.net" --language es
whatsapp --json media transcripts show MESSAGE_ID --chat-jid "15551234567@s.whatsapp.net"
```

`messages list` and `messages context` include reaction and receipt metadata on
each message when the bridge has observed it. Reactions are exposed as
`reactions`; receipts are exposed as `receipts`, with `seen_by` as a convenience
list for `read` receipts. This is message receipt data, not online presence or
last-active tracking.

Broad semantic review is bounded by message timestamp. If the caller omits a
time span, the WhatsApp skill uses the previous 24 hours ending at invocation
time; explicit `--after` and `--before` values allow replay or wider review.
Exact message IDs and explicitly bounded context reads do not need a separate
window. No semantic processed cursor suppresses revisiting a message.

Audio transcription is arrival-triggered and cached. Reads never transcribe as
a side effect: `messages list` and `messages context` leave audio alone.

The bridge fires `WHATSAPP_MEDIA_ARRIVAL_HOOK` when media lands. A voice note
is therefore transcribed seconds after it arrives, on the message event
itself, with no polling in the path. `WHATSAPP_MEDIA_ARRIVAL_HOOK_TYPES`
controls which media types fire it (default `audio`); unset the hook to disable
it entirely.

The same bridge runs `media transcribe-pending --drain` when it starts or
reconnects. That reconciles audio stored during downtime or history sync in
bounded sequential batches, without a second daemon polling beside the event
listener or silently stopping after the first batch. History-sync and
bridge-originated media are covered by reconciliation and the arrival hook,
respectively. Reconciliation is limited to the last 14 days by default,
matching the useful CDN window rather than repeatedly touching permanently
expired history.

Both bridge hooks resolve the installed `whatsapp` front door from `PATH`
before falling back to the current source checkout. This keeps a running bridge
valid across plugin upgrades instead of retaining a deleted versioned-cache
path.

Agents read the cache with `media transcripts show`; `media transcribe`
remains available for a single message that has not been handled yet, and
repeated calls return the cached transcript unless `--refresh` is passed.

Both bridge-owned paths exist because WhatsApp expires media from its CDN after
roughly two to three weeks. Audio not transcribed inside that window cannot be
recovered, so live events handle the normal case and reconnect reconciliation
covers downtime.

Because expired media never comes back, a CDN 404 or 410 leaves automatic
reconciliation immediately. Other failures remain durable and automatically
eligible on later bridge reconciliation runs regardless of attempt count, so a
temporary bridge, API, metadata, network, or key outage cannot strand a voice
note. Pass `--retry-failed` only to explicitly retry an item previously
classified as permanently unavailable.

Transcription uses an installed ElevenLabs CLI at runtime. Claude installs that
dependency as `elevenlabs@package-manager`, while the CLI still resolves it from
`PATH` without reaching into a versioned plugin cache. It requires
`ELEVENLABS_API_KEY` only on cache misses. A per-message process lock prevents
duplicate delivery paths from spending twice, and transient arrival failures
receive bounded retries.

## Use With Claude Code

Claude Code can load the same plugin root locally:

```bash
claude --plugin-dir .
claude plugins validate .
```

The Claude manifest does not auto-register a native MCP server. Use the CLI path by default.

## Drafts And Sends

Drafts are local review artifacts stored in SQLite. They do not create WhatsApp's native green draft label.

The bundled `draft-message` skill collects bounded WhatsApp evidence for a
user-voice draft, giving the target chat the strongest weight and excluding
sensitive examples. It passes that evidence to
`writing:impersonating`, the canonical cross-channel owner of the user's
wording. The adapter does not change the romantic-partner or
disclosed-delegation boundaries below.

```bash
whatsapp --json drafts create --chat-jid "15551234567@s.whatsapp.net" --text "Thanks, received."
whatsapp --json drafts list
whatsapp --json drafts send DRAFT_ID --dry-run
whatsapp --json drafts send DRAFT_ID --confirm
```

Direct sends are guarded:

```bash
whatsapp --json messages send --chat-jid "15551234567@s.whatsapp.net" --text "Thanks" --dry-run
whatsapp --json messages send --chat-jid "15551234567@s.whatsapp.net" --text "Thanks" --confirm
```

`--confirm` marks a deliberate live send. Every message needs current one-message send authorization and an exactly resolved recipient. The user may supply the full text and say to send it, or may clearly instruct the agent to send, tell, ask, reply, or let the recipient know something for a stated objective. The latter authorizes the agent to compose and send one disclosed message without stopping for exact-text approval. A request only to draft, write, or prepare does not authorize a send, and broad continuing delegation or earlier authorization does not authorize follow-ups. Every agent-authored delegated message starts with `⌘` for Codex or `✳️` for Claude; the first message also states the agent's identity in words. Sensitive disclosures, payments, schedule commitments, legal or medical decisions, and scope changes require a separate human decision before drafting.

For the user's romantic partner, default to disclosed delegation. Resolve the
current partner through Apple Contacts' exact relationship match and live
WhatsApp phone metadata rather than a hard-coded identity. Use user-voice mode
only when the user explicitly opts in for the current objective. This mode choice
changes the template and disclosure, not whether a send was requested: an
explicit one-message send command authorizes one disclosed message, while a
draft request or a template default does not.

## State

Runtime state is stored outside the repo by default:

```text
~/.local/share/codex-whatsapp/
```

That directory contains local SQLite databases, bridge logs, QR files, and linked-device state. It is intentionally not part of the repository. Lifecycle commands migrate the state root and every state subdirectory to mode `0700`, migrate regular state files to `0600`, and start the bridge under umask `077`. The direct CLI applies the same protection to draft and transcript state, including custom database paths.

Important environment variables:

- `WHATSAPP_PLUGIN_STATE_ROOT`: override the local state root.
- `WHATSAPP_SOURCE_ROOT`: point the CLI at a different plugin checkout.
- `WHATSAPP_DRAFTS_DB_PATH`: override the local drafts database path.
- `WHATSAPP_TRANSCRIPTS_DB_PATH`: override the local audio transcript cache database path.
- `ELEVENLABS_TRANSCRIBE_SCRIPT`: override the ElevenLabs Scribe helper path used by `media transcribe`.
- `WHATSAPP_MCP_HTTP_PORT`: override the local bridge port.
- `WHATSAPP_MCP_PAIR_PHONE`: explicit phone-number pairing fallback.
- `WHATSAPP_MCP_REQUEST_FULL_HISTORY`: request WhatsApp's maximum linked-device history during fresh pairing (setup defaults to `1`).
- `WHATSAPP_MCP_HISTORY_SYNC_TIMEOUT_SECS`: maximum setup wait for the full-history stream (default `600`).

## Legacy MCP

The vendored MCP server is still present for manual recovery and compatibility, but it is disabled by default.

```bash
WHATSAPP_ALLOW_NATIVE_MCP=1 pnpm mcp
```

For day-to-day agent work, prefer the CLI.

## Attribution

This project vendors and patches `lharries/whatsapp-mcp`, licensed under MIT. See `NOTICE.md` and `vendor/lharries-whatsapp-mcp/LICENSE`.
