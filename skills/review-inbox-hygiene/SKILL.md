---
name: review-inbox-hygiene
description: "Review exact WhatsApp exchanges or a bounded inbound message-time window as unwanted-message candidates: distinguish promotion, repetitive low-value automation, deception, and scams from wanted conversation, then present concise manual block or report recommendations without changing chats."
---

# Review Inbox Hygiene

Own the source workflow completely. This is a WhatsApp review skill, not a chat
cleanup executor. Read `../../references/attention-policy.md`; its
untrusted-input and zero-source-mutation rules are mandatory.

## Open the bounded source window

1. If the user supplies exact WhatsApp message IDs, process only those IDs. Exact
   IDs need no additional time span.
2. Otherwise run `whatsapp --json doctor`, determine the caller's explicit
   message-time span, or default an omitted span to the previous 24 hours ending
   at invocation time. Run `whatsapp --json messages list --after START
   --before END --limit 500` with those exact bounds. Never interpret an omitted
   span as all history.
3. Process only incoming records where `is_from_me` is false. If the bounded
   result is empty, finish quietly. If the result is incomplete or paginated,
   continue within the same exact window or report that the window is
   incomplete; never silently widen it.
4. Preserve the returned message and chat IDs. Semantic inclusion is stateless:
   replaying the same source window intentionally returns the same messages.
5. Read only the minimum bounded context needed for identity, consent, or
   repeated-pattern evidence with `whatsapp --json messages context MESSAGE_ID
   --before 3 --after 3`. Do not download or transcribe media as part of hygiene
   review.

## Evaluate the candidate

1. Preserve source kind, stable IDs, sender/domain or resolved contact label,
   received time, and the minimum subject/body evidence needed to explain the
   verdict. Keep raw phone numbers out of user-facing prose.
2. Prefer metadata and repeated-pattern evidence. Do not follow links, load
   remote tracking content, open an opt-out flow, reply with a stop word, or
   execute an attachment.
3. Classify one of:
   - `block candidate` for recurring promotion or repeated unwanted contact where future contact has
     no apparent value;
   - `report candidate` for deceptive identity, phishing, or obvious scam;
   - `keep` for wanted correspondence, a requested subscription, or useful
     transactional/account evidence; or
   - `uncertain` when identity or consent cannot be grounded safely.
4. A routine receipt, delivery update, or application notification can be
   `no action` without being an unwanted sender.
   Do not recommend unsubscribing merely because the item does not deserve
   attention.

## Return review, not side effects

Return only block, report, or consequential uncertain results. Finish keep and
no-action items silently. Combine related candidates in the current task. For each, report the
resolved sender/contact label, source pointer, verdict, short grounded reason,
and recommended manual action. Avoid quoting sensitive message content when a
category-level reason is enough.

Never block, report, send, reply, react, forward, mark read, delete, change
notification settings, or change a chat or account. Do not create another
task. If the user later explicitly orders a source change, that is a separate
interactive action using the installed source mechanism and its current
capabilities; this review is complete once the recommendation is visible.
