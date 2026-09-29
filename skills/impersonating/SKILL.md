---
name: impersonating
description: Draft or revise text in the user's own writing style from relevant user-authored evidence. Use for messages, email, SMS, RCS, iMessage, WhatsApp, browser forms, applications, essays, posts, and replies written on the user's behalf. The active assistant writes directly; no other model is required. Do not use for ordinary assistant responses or disclosed agent-authored messages.
---

# Impersonating

Write as the user from evidence. The active assistant authors, reviews, and revises the draft directly, including one-sentence requests. Do not route writing to Claude or another provider by default. A different author requires an explicit user request; preserve that author's attribution and relay contract without silently substituting your own draft.

This skill owns wording. The destination's tools or skill own verified context, recipients, fields, draft storage, and authorized sending or publication. Drafting does not authorize sending, submitting, or publishing. Match the user's voice only when writing on their behalf; keep disclosed agent messages in the agent's voice.

## Choose the relevant voice

First establish the language, audience, relationship, medium, purpose, and requested format. A private text, a professional email, and a public essay may use different registers. Match the relevant register rather than averaging everything the user has ever written.

Use evidence in this order:

1. The user's current wording, intent, explicit preferences, and corrections.
2. Relevant user-authored text already present in the conversation or supplied for this task.
3. A small number of outbound samples from the same audience, recipient, medium, or purpose, read through an available destination tool.
4. Bounded cross-channel samples when close matches are unavailable.

If the user asks to match their style, a bounded read of relevant outgoing examples through an available connector is part of that request. Use already available context first. Do not search unrelated accounts or entire histories. Follow the source tool's access and privacy boundaries. If access is unavailable, work from supplied examples and state the evidence limit outside the draft.

WhatsApp and iMessage are valid style sources, alongside email, SMS, RCS, documents, and other connected services. Use the installed WhatsApp or Messages tools when available. Confirm that samples are outgoing and authored by the user. Exclude quoted replies, forwarded messages, other people's wording, shared documents with uncertain authorship, and known assistant-generated drafts unless the user explicitly endorses them as examples. A message being sent from the user's account is not proof that the user composed it. Never claim a message was typed rather than dictated unless the source establishes that fact.

Read actual prose before claiming to have matched it. Titles, links, and search snippets alone do not establish its cadence or wording. Prefer a few strong matches over a large noisy sample. Track the provenance and limitations in the current task; show at most a few original source links when useful and safe, not raw message dumps.

The user may keep explicit writing preferences privately at `WRITING_PREFERENCES_PATH` or, by default, `~/.config/writing/preferences.md`. Read it only if present; current instructions take precedence. It is optional and is not a generated style cache. Do not create or update preferences unless the user requests that change or explicitly authorizes moving existing private configuration.

## Build a small style brief

Infer patterns in language, contractions, address and formality, casing, punctuation, sentence length, rhythm, openings, sign-offs, humor, and directness. Record only features supported by the samples. One example suggests a possibility; it does not establish an always or never rule.

Keep the style brief in the current task. Do not save raw messages, per-contact profiles, private topics, personal facts, or generated style caches in Git, memory, or plugin files. Do not carry names, promises, sensitive facts, or anecdotes from a sample into an unrelated draft.

Preserve intentional contractions, fragments, bluntness, code-switching, or profanity when supported by the user's wording and suitable for this audience. Do not add any of them as an authenticity prop. Do not invent enthusiasm, warmth, familiarity, commitments, credentials, or factual claims.

When evidence is sparse, use the user's current wording and the shortest factually complete best effort. Mention uncertainty outside the artifact only when it matters. Ask for a concrete correction only if needed to finish the requested work.

## Compose with the requested format

Use the requested structure without replacing the user's voice with a house voice. Do not apply Impersonating automatically to requirements briefs or sample private messages merely to style them. Apply it to a brief only when the user explicitly requests voice imitation for that brief; preserve the brief's structure while following the requested voice. Preserve destination limits such as a subject line or character count.

Produce one finished draft unless the user asks for alternatives. Keep explanation, evidence notes, and recipient metadata outside the draft itself.

## Review the exact draft

Apply this compact Humanizer-informed review without flattening the user's own quirks:

- Preserve every supported claim, qualification, and intended commitment. Do not invent facts or sources.
- Prefer the user's natural words over inflated importance, sales language, vague authority, and stock assistant prose.
- Remove chatbot preambles, offers to help, ceremonial openings, repeated conclusions, and generic positive endings that are not part of the user's voice.
- Do not force groups of three, dramatic fragments, or “not X but Y” constructions merely for rhythm.
- Keep useful uneven rhythm, contractions, self-corrections, and asides when they fit the evidence and audience.
- Remove redundant qualifiers and intensifiers when they contribute no meaning. Do not impose a universal forbidden-word list on every user.
- Use compound modifiers and punctuation only as the grammar and the user's preferences warrant.

When the user's preferences prohibit em dashes, en dashes, and spaced double hyphens, run the bundled checker on the exact candidate via standard input:

```bash
python3 <impersonating-skill-dir>/scripts/check_draft.py
```

This checker implements that optional no-dash preference; it is not a universal definition of good writing. It allows ordinary word hyphens. Pass the candidate through a tool's stdin facility without putting it in command arguments, persistent files, or logs. The checker reports counts only and does not echo or store the text. If an enforced check fails, revise and rerun before returning the draft. If the required checker cannot run, report that narrow limitation instead of claiming the check passed.

Read the draft against the style brief and facts. Make the smallest correction that preserves both. If another author was explicitly requested, send the correction back through that workflow and retain its relay boundaries.

See [the third-party notice](../../THIRD_PARTY_NOTICES.md) for the Humanizer source and MIT attribution.

Show the final draft in full whenever review is needed. Let the destination workflow place it in an authorized native draft and verify the saved result. Never treat a completed draft as evidence that it was sent or published.
