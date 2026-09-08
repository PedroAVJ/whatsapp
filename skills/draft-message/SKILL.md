---
name: draft-message
description: Prepare a WhatsApp user-voice message or reply by collecting live, bounded outbound examples from the target chat and a safe global baseline, then use writing:impersonating for the wording. Use when the user asks to reply as themselves, write in their own voice, or correct a WhatsApp draft that does not sound like them. Do not use to imitate the user in a disclosed agent-authored conversation.
---

# Prepare a WhatsApp User-Voice Draft

Use this adapter together with the bundled `whatsapp` skill and
`writing:impersonating`. WhatsApp owns chat resolution, relationship
boundaries, source context, reviewable drafts, approval, and live sends. Writing
is the canonical owner of the user's voice and final wording. This adapter only
collects the WhatsApp-specific evidence Writing needs.

Default to a local reviewable draft. Never treat a request to draft as
permission to send.

## Choose the communication mode first

- Use this workflow only for a user-voice message presented as if the user wrote it.
- If the `whatsapp` skill requires disclosed delegation for the target, or the user
  explicitly wants an agent-authored conversation, do not imitate the user. Follow
  that skill's disclosure, marker, and authorization rules instead.
- A romantic-partner chat defaults to disclosed mode. Enter user-voice mode only
  when the `whatsapp` skill's explicit, objective-scoped opt-in is present.

## Collect bounded WhatsApp evidence

Recompute the sample for each drafting objective. Never hard-code a contact,
persist a per-contact profile, or copy personal messages into the plugin, source
control, or memory.

1. Resolve the target chat through `whatsapp` and read only the immediate inbound
   context needed to understand the reply.
2. Build the **per-contact sample** from a bounded recent listing. Retain up to
   30 of the user's newest nonempty outbound text messages (`is_from_me: 1`). Ignore
   media-only messages and quoted reply text written by someone else.
3. Build the **global baseline** from an explicit bounded window only when it
   adds useful evidence. Start with the previous 30 days and retain no more than
   30 representative outbound texts from direct human chats. Exclude target-chat
   duplicates, groups, broadcasts, self-chat, businesses, bots, automated
   services, and romantic-partner messages used for a different recipient.
4. Exclude examples containing credentials, one-time codes, account or document
   identifiers, intimate content, medical or legal details, or another person's
   secrets. Prefer a locally inferred compact brief over exporting raw examples
   when privacy is material.

Treat fewer than five usable per-contact messages as sparse evidence. A single
message can demonstrate a possibility but cannot prove an `always` or `never`
rule.

## Draft with Writing

The active assistant drafts directly using `writing:impersonating`; no Claude
or other model delegation is required. Use only:

- the user's exact objective, current wording, corrections, and verified facts;
- the immediate inbound message being answered;
- a compact five-to-ten-point brief of repeated WhatsApp patterns;
- three to eight representative per-contact examples when available; and
- three to eight nonsensitive global examples only when they materially help.

Do not hand over an entire chat history. Target-chat evidence overrides the
global baseline for that recipient. Writing owns the evidence order, style
inference, cross-channel fallback, and final check for assistant-like wording.
Factual correctness and the user's current intent override imitation.

If the user explicitly asks Claude to write the message, follow Claude's relay
contract: preserve the user's latest request verbatim and append only the compact style
context under `[Context from Codex]`. Review the result through
`writing:impersonating` before presenting it.

## Preserve reviewability

Create or update a local WhatsApp draft for the resolved target. Prefer updating
the existing unsent draft for the same objective instead of accumulating
duplicates. Show the exact recipient, full text verbatim, and draft ID. A live
send still requires the authorization defined by the `whatsapp` skill.
