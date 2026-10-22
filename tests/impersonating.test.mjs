import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { access, readFile, stat } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { join } from "node:path";
import test from "node:test";

const root = fileURLToPath(new URL("..", import.meta.url));
const skillDir = join(root, "skills", "impersonating");

test("impersonating moved into WhatsApp with its checker and implicit invocation", async () => {
  const skill = await readFile(join(skillDir, "SKILL.md"), "utf8");
  const metadata = await readFile(join(skillDir, "agents", "openai.yaml"), "utf8");
  assert.ok(skill.startsWith("---\nname: impersonating\n"));
  assert.match(skill, /WhatsApp message or reply/);
  assert.match(skill, /`draft-message`/);
  assert.match(skill, /`~\/\.config\/writing\/preferences\.md`/);
  assert.match(skill, /`toolchain:brief` owns briefs and their independent style/);
  assert.match(skill, /Do not apply Impersonating automatically to requirements briefs/);
  assert.doesNotMatch(skill, /writing:impersonating|<writing-skill-dir>/);
  assert.match(metadata, /Use \$impersonating /);
  assert.match(metadata, /allow_implicit_invocation: true/);
  await access(join(root, "THIRD_PARTY_NOTICES.md"));
  assert.ok((await stat(join(skillDir, "scripts", "check_draft.py"))).mode & 0o100);
});

test("the checker rejects prohibited dash punctuation without echoing drafts", () => {
  const checker = join(skillDir, "scripts", "check_draft.py");
  const run = (input) => spawnSync("python3", [checker], { encoding: "utf8", input });

  for (const [input, violation] of [
    ["private-alpha — private-omega", "em_dash"],
    ["private-alpha – private-omega", "en_dash"],
    ["private-alpha -- private-omega", "spaced_double_hyphen"],
  ]) {
    const result = run(input);
    assert.equal(result.status, 1);
    assert.equal(JSON.parse(result.stdout).ok, false);
    assert.equal(JSON.parse(result.stdout).violations[violation], 1);
    assert.equal(result.stdout.includes(input), false);
    assert.equal(result.stderr.includes(input), false);
  }

  const clean = run("A voice-first assistant can monitor long-running work through full-duplex audio.");
  assert.equal(clean.status, 0);
  assert.deepEqual(JSON.parse(clean.stdout), { ok: true, violations: {} });
});

test("the Humanizer adaptation keeps exact source provenance and license", async () => {
  const notice = await readFile(join(root, "THIRD_PARTY_NOTICES.md"), "utf8");
  assert.match(notice, /Humanizer 2\.11\.2/);
  assert.match(notice, /e2e92e7b4b8229253ed5c8e81dc65463fdeddda5/);
  assert.match(notice, /Copyright \(c\) 2025 Siqi Chen/);
  assert.match(notice, /MIT/);
});
