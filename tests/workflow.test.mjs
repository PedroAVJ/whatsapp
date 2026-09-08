import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import test from "node:test";

const root = new URL("..", import.meta.url).pathname;

test("WhatsApp hygiene remains client-neutral and source-read-only", async () => {
  for (const contents of await Promise.all([
    readFile(join(root, "skills", "review-inbox-hygiene", "SKILL.md"), "utf8"),
    readFile(join(root, "references", "attention-policy.md"), "utf8"),
  ])) {
    assert.doesNotMatch(contents, /\bIntake\b|Codex-only|shared sweep|event CLI|event envelope/i);
    assert.match(contents, /never/i);
  }
});

test("WhatsApp retains arrival transcription and bounded semantic reads", async () => {
  const cli = await readFile(join(root, "cli", "whatsapp_cli.py"), "utf8");
  const skill = await readFile(join(root, "skills", "whatsapp", "SKILL.md"), "utf8");
  assert.match(cli, /def command_media_arrival_hook/);
  assert.match(cli, /def command_media_transcribe_pending/);
  assert.match(skill, /previous 24 hours/i);
  assert.match(skill, /semantic processed cursor/i);
});

test("WhatsApp hygiene owns bounded inbound discovery", async () => {
  const skill = await readFile(join(root, "skills", "review-inbox-hygiene", "SKILL.md"), "utf8");
  assert.match(skill, /whatsapp --json messages list --after START/);
  assert.match(skill, /previous 24 hours/i);
  assert.match(skill, /is_from_me/);
  assert.match(skill, /stateless/i);
  assert.match(skill, /Finish keep and\s+no-action items silently/i);
});

test("WhatsApp safely adapts bounded evidence into canonical Writing voice", async () => {
  const skill = await readFile(join(root, "skills", "draft-message", "SKILL.md"), "utf8");
  const metadata = await readFile(
    join(root, "skills", "draft-message", "agents", "openai.yaml"),
    "utf8",
  );

  assert.match(skill, /per-contact sample/i);
  assert.match(skill, /global baseline/i);
  assert.match(skill, /is_from_me: 1/i);
  assert.match(skill, /single\s+message[\s\S]*cannot prove an `always` or\s+`never`\s+rule/i);
  assert.match(skill, /romantic-partner chat defaults to disclosed mode/i);
  assert.match(skill, /Never hard-code a contact/i);
  assert.match(skill, /Create or update a local WhatsApp draft/i);
  assert.match(skill, /writing:impersonating/i);
  assert.match(skill, /Writing\s+is the canonical owner/i);
  assert.match(metadata, /allow_implicit_invocation: true/);
  assert.match(metadata, /Use \$draft-message/);
  assert.match(metadata, /\$draft-user-voice/);
});
