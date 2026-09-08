---
name: whatsapp
description: Use the installed `whatsapp` CLI, Apple Contacts identity resolution, and local bridge to read, inspect, search, or summarize WhatsApp chats, contacts, messages, voice-note transcripts, and media; prepare guarded drafts; or handle disclosed delegated conversations. Trigger for any WhatsApp request, including the latest message, a named contact or chat, drafting, sending, or follow-up.
---

# WhatsApp

Use the installed `whatsapp --json ...` CLI directly for WhatsApp work. This
plugin is the product shell and operating manual around that CLI and its local
bridge. Do not use computer control or a browser, and do not hunt for a separate
WhatsApp tool merely because no native MCP server is registered.

## Start

Check health first when bridge status matters:

```bash
whatsapp --json doctor
whatsapp --json bridge status
```

Start or relink only when needed:

```bash
whatsapp --json bridge start
whatsapp bridge relink --confirm
```

Use WhatsApp's QR pairing flow only when the user can display the QR on one
screen and scan it with their phone. When the user is operating remotely from
that same phone, or otherwise cannot scan the displayed QR, use manual
phone-number pairing immediately:

```bash
WHATSAPP_USE_PHONE_PAIRING=1 whatsapp bridge relink --confirm
```

Tell the user to open WhatsApp -> Linked devices -> Link a device -> Link with
phone number instead, then enter the code shown by the command. The relink
preflights the pairing phone before deleting sync state and refuses to fall
back to QR if the phone cannot be resolved. For QR pairing, tell the user to
open WhatsApp -> Linked devices -> Link a device and scan the QR shown or
written to `~/.local/share/codex-whatsapp/upstream-qr.png`.

Fresh pairing requests the maximum linked-device history WhatsApp permits and
waits for its full-history stream before starting the durable bridge. Do not
describe that as an account-complete backup: WhatsApp bounds linked-device
history, and messages outside that window may remain available only on the
primary phone.

## Reading

- For a broad source-processing request, determine a half-open window over the
  WhatsApp message timestamp. Use an explicit caller-supplied span, or default
  an omitted span to the previous 24 hours ending at invocation time. Apply it
  with `messages list --after START --before END`; never default to all message
  history. An exact message ID or explicitly bounded context read is already
  bounded and needs no additional window. Re-running a window may intentionally
  revisit the same messages; do not maintain a semantic processed cursor.
- Discover chats with metadata first. For a read-only request about a recent or
  latest message from a described person or role, use recency, chat metadata,
  and the minimum bounded message context needed to identify the intended
  conversation and answer. A missing Contacts relationship label must not
  block reading.
- Use Contacts when it cheaply disambiguates a named person, but do not require
  exact Contacts identity before reading. If minimal bounded context still
  leaves multiple plausible chats, ask which one. Never browse unbounded
  history merely to resolve identity.
- Use message context when replies or nearby discussion matter.
- Download media only when the media itself is needed for the task.
- Audio transcription is background infrastructure, not an agent decision. The
  bridge transcribes a voice note when it arrives and reconciles pending audio
  whenever it starts or reconnects, so a transcript normally already exists.
  **Read it**:
  `whatsapp --json media transcripts show MESSAGE_ID --chat-jid "CHAT_JID"`.
- Call `media transcribe MESSAGE_ID "CHAT_JID"` only when a specific message
  has no cached transcript yet and the task needs it now. Use `--refresh`
  only when the human asks to retranscribe or the cached transcript is
  clearly bad.
- WhatsApp expires media from its CDN after roughly two to three weeks.
  Audio older than that cannot be downloaded or transcribed at all — a
  missing transcript on an old message is permanent, not a task to retry.
  This is why bridge reconnect reconciliation is automatic.

Useful fallback commands:

```bash
whatsapp --json chats list --limit 20 --no-last-message
whatsapp --json chats list --query "project name" --limit 20 --no-last-message
whatsapp --json messages list --chat-jid "CHAT_JID" --limit 30
whatsapp --json messages list --after "START" --before "END" --limit 100
whatsapp --json messages context MESSAGE_ID --before 5 --after 5
whatsapp --json media download MESSAGE_ID "CHAT_JID"
whatsapp --json media transcripts show MESSAGE_ID --chat-jid "CHAT_JID"
whatsapp --json media transcribe MESSAGE_ID "CHAT_JID"
whatsapp --json media transcribe-pending --dry-run
```

Message JSON may include `reactions`, `receipts`, and `seen_by` when the bridge
has observed reaction or read-receipt events. Treat `seen_by` as read-receipt
data only; it is not last-active or online-presence data.

A message carries `edited_at` when the sender revised it after sending. Then
`content` is the current text and `timestamp` is still when the message was
first sent; the text originally sent is not retained. Senders correct times,
names, amounts, and decisions this way, so when a message is load-bearing for
something you are about to act on, check `edited_at` before treating its text as
what the sender meant. A senders-only WhatsApp edit window means a revision
usually lands within fifteen minutes of the original.

## Apple Contacts Send Identity Gate

Apple Contacts is the durable identity and relationship index for person-scoped
outbound WhatsApp work. Before every live send to a direct chat, use the
installed Apple Contacts skill to resolve the target. Reading and local drafting
do not require this gate. Start with `me` for
"me", "my number", or "myself"; start with `relationships` for terms such as
partner, wife, mother, or boss; and use `search` plus an exact `read` for a
named person or organization. Use the stable `contacts --json ...` front door
when available; otherwise invoke the bundled script from the installed Apple
Contacts skill. Never guess an installed cache path.

Treat Apple relationship entries as directional labels from My Card. Accept a
relationship only when `match_status` is `exact` and it resolves to exactly one
contact card. If a requested role is absent, unmatched, or ambiguous, stop
before sending unless the user identifies the person explicitly for this task.
That uncertainty does not block read-only inspection: use recent metadata and
minimum bounded context, then ask only if multiple plausible chats remain. An
instruction such as "my boss" is not permission to promote a recent or familiar
chat into that role for an outbound send.

Map the words boss, manager, supervisor, and jefe to Apple's standard
`manager` relationship label. My Card may contain more than one manager; that
is valid. Resolve all exact manager relationships first, then use the current
task or repository context to select among those already-labeled contacts. If
the context still fits multiple managers, ask which one. Never require a
literal custom `boss` label and never infer an unlabeled person into the set.

Match the resolved card to WhatsApp using the Contacts plugin's full `e164`
phone candidates. Prefer `e164_source: explicit` or
`explicit_legacy_mexico`; also accept `e164_source: default_country`, which expands an unprefixed national number
with this Mac's Contacts default country while preserving that provenance.
Deduplicate identical `e164` values that appear under multiple labels, then
require exactly one direct chat whose `phone_number` matches exactly one unique
full candidate after removing formatting and comparing country code plus national
number before sending. A matching display name, nickname, push name, partial
phone suffix, or search rank is never identity proof for an outbound send. Do
not send when the card has no usable candidate, multiple cards or chats remain
plausible, or the full numbers disagree. Canonical phone comparison may normalize a region-specific
historical transport form only when it preserves the same full country code
and national number. In particular, WhatsApp's legacy Mexican mobile prefix
`+521` and current Mexican E.164 `+52` are equivalent only when the same ten
national digits follow. Never construct a match from a bare suffix yourself;
the default-country expansion must come from the Contacts plugin.

For a self-chat, read Apple Contacts My Card first, then verify the target
against the WhatsApp bridge's registered account JID identity. The registered
account JID, normalized only by removing a device suffix, is authoritative for
the WhatsApp number; a chat named the user, You, Self, or similar is not. If My
Card exposes a phone, it must also match the registered account number. If My
Card has no phone, report that Contacts could not corroborate the number and
use only the exact registered-account self-chat—never a name-search fallback.

Keep raw contact IDs, phone numbers, JIDs, and LIDs private. Report the resolved
person and verification result, not the identifiers. Re-run this gate from live
Contacts and WhatsApp metadata for each outbound objective; do not retain a
hard-coded personal map in plugin source, memory, drafts, or logs.

## Drafts And Sends

Drafts are local review artifacts. They do not create WhatsApp's native green Draft label.

Choose the send mode before writing:

- **User-voice message:** the message is presented as if the user personally wrote it. Read recent messages in the target chat and match the user's prior outbound style for that person or group. Create a reviewable draft and obtain approval of the exact recipient and exact text before the live send.
- **Disclosed delegated conversation:** the user wants an agent-authored message to a named or clearly resolved recipient for a stated objective. Start the first message with a natural disclosure such as "Hola, soy Codex, el asistente de la persona que me pidió escribirte" and write in the agent's own voice; never imply that the user personally typed it. A current one-message instruction to send, tell, ask, reply, or let a clear recipient know something authorizes the agent to compose and send that one disclosed message; the exact wording need not be preapproved. Use a local draft and dry-run as internal safeguards when useful, but do not stop at the draft merely because the user left the wording to the agent. A request only to draft, write, or prepare a message does not authorize a live send. Broad continuing delegation such as "handle the conversation" or "keep going" does not authorize future messages.

For a user-voice draft, use the bundled `draft-message` adapter together with
`writing:impersonating`. The adapter gathers bounded WhatsApp evidence and
Writing owns the wording; neither weakens the relationship, approval, or send
rules here.

### Romantic-partner boundary

For any direct chat the user identifies as their girlfriend, wife, spouse, or romantic
partner, default to a disclosed delegated conversation and the agent-authored
template. User-voice mode is allowed only when the user explicitly asks for the
current outbound objective to be written as him or in his voice; a general
request to draft, reply, send, or handle the conversation does not count. That
opt-in is scoped to the current objective and does not carry forward.

The romantic-partner default chooses the disclosed template; it does not itself
authorize a send. It also does not cancel a current one-message command: when
the user explicitly says to send, tell, ask, reply, or let the resolved partner
know something for a clear objective, compose and send one disclosed message.
For user-voice mode, create a reviewable local draft, show the user the exact
recipient and full text, and obtain approval before the live send. When no
current send command exists, disclosed mode also stops at a reviewable draft.
Use the required client marker and first-message identity statement for every
disclosed message. Never treat the romantic-partner default as permission to
auto-send.

Resolve the current romantic-partner chat through the Apple Contacts identity
gate above and live WhatsApp metadata.
Do not hard-code a name, phone number, JID, or relationship status in the plugin.

Every outbound message in a disclosed delegated conversation, including every
follow-up, must begin with the client-specific identity marker:

- Codex: `⌘`
- Claude: `✳️`

The marker is a compact signature, not a substitute for the first-message
disclosure. For example, Codex begins the first message with
`⌘ Hola, soy Codex, el asistente de la persona que me pidió escribirte.` and begins later follow-ups with
`⌘ ` before the message text. Claude follows the same pattern with `✳️`.
Never add either marker to a user-voice message, because that message is being
presented as the user's own words.

Do not produce a generic standalone draft when a target chat can be resolved.

User-voice drafts must sound like the user actually writes in that chat. Mirror the user's normal language, casing, punctuation, brevity, greetings, names, and directness from recent outbound messages. Do not default to English, formal phrasing, or assistant-like wording unless the user's recent messages to that chat use that style.

Disclosed delegated messages should match the recipient's language and be concise, but they must remain recognizably agent-authored. After an authorized send, collect only the recipient-provided information needed for the stated task. Do not send a follow-up unless the user gives another current one-message send instruction; one-message send authorization never carries forward to another. Do not echo private data back to the user merely to prove that it was collected.

When there is no outbound history to mirror, resolve the recipient's language from private contact context; use the current request's language when no more specific preference applies.

When the target chat is known or can be resolved cheaply, create a reviewable local draft with `whatsapp --json drafts create ...` and report the draft id for a draft-only request or a message that still needs wording approval. For a disclosed message already authorized by a current one-message send command, a local draft may still be used internally, but do not stop to present it for another approval; dry-run and send it. If the target chat is genuinely unclear, ask for the recipient instead of guessing.

For either mode, show the user the full draft text verbatim in chat, quoted per recipient, together with its draft id whenever approval is still needed. A draft id alone is not reviewable; never ask the user to approve a draft they have not seen word for word in the conversation.

Disclosed delegation never waives current per-message send authorization. In addition, do not even prepare a message that would commit money or schedule, make a legal or medical decision, disclose the user's secrets or sensitive data, contact an unresolved or unexpected recipient, broaden the stated objective, or otherwise create a consequential external commitment without first resolving that decision with the user. If the message would speak as the user rather than as the disclosed agent, switch to user-voice mode and retain the exact recipient-and-text approval gate.

Pace multi-recipient sends like a human. Never fire bridge sends back-to-back: wait at least 60 seconds between recipients, and several minutes per send when the recipients have no prior chat history or the messages contain links, attachments, or credentials. Burst sends from a linked device match WhatsApp's bot-spam signature and can get the linked device deregistered, killing the bridge session until the user relinks through WhatsApp's linked-device flow.

Fallback commands:

```bash
whatsapp --json drafts create --chat-jid "CHAT_JID" --text "Gracias, recibido"
whatsapp --json drafts send DRAFT_ID --dry-run
whatsapp --json drafts send DRAFT_ID --confirm
```

## Rules

- Reading and local drafts are safe by default.
- Do not loop `media transcribe` over a listing to catch up a backlog. The
  bridge owns reconciliation; an agent transcribes at most the one message it
  needs right now and otherwise reads the cache.
- Use live-send `--confirm` only after the user has authorized that specific message. Exact supplied text plus "send" qualifies; so does a current instruction to send, tell, ask, reply, or let a clear recipient know something in disclosed mode. A draft request, broad continuing objective, or authorization of an earlier message is not authorization of a follow-up.
- In disclosed delegated mode, prefix every outbound message with the correct client-specific identity marker, identify the agent in the first outbound message of the exchange, and never impersonate the user.
- Never send to multiple recipients without spacing the sends; see the pacing rule above. Batch approval is not batch dispatch.
- Use `--json` whenever reading command output for analysis.
- Prefer metadata-only discovery with `--no-last-message` before reading message contents.
- Do not show raw JIDs or LIDs unless debugging internals.
- Treat `chat_type: "broadcast"` as WhatsApp Status/Broadcast, not a normal person or project chat.
- Treat `identifier_type: "lid"` as a direct chat backed by WhatsApp's LID identity system.
- Use Apple Contacts first and `whatsapp --json contacts search ...` only as
  WhatsApp-side corroboration. A WhatsApp contact or display name alone never
  satisfies the identity gate for a person-scoped send.
