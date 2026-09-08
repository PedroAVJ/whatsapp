import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { join } from "node:path";
import test from "node:test";

const root = fileURLToPath(new URL("..", import.meta.url));
const expected = {
  "name": "whatsapp",
  "version": "0.11.10",
  "url": "https://github.com/PedroAVJ/whatsapp",
  "dependencies": [
    "contacts@package-manager",
    "elevenlabs@package-manager",
    "writing@package-manager"
  ]
};

async function json(...parts) {
  return JSON.parse(await readFile(join(root, ...parts), "utf8"));
}

test("standalone plugin metadata is synchronized", async () => {
  const codex = await json(".codex-plugin", "plugin.json");
  assert.equal(codex.name, expected.name);
  assert.equal(codex.version, expected.version);
  assert.equal(codex.homepage, expected.url);
  assert.equal(codex.repository, expected.url);
  await access(join(root, "README.md"));
  await access(join(root, "AGENTS.md"));

  if (expected.codexOnly) {
    await assert.rejects(access(join(root, ".claude-plugin", "plugin.json")));
  } else {
    const claude = await json(".claude-plugin", "plugin.json");
    assert.equal(claude.name, codex.name);
    assert.equal(claude.version, codex.version);
    assert.equal(claude.homepage, expected.url);
    assert.equal(claude.repository, expected.url);
    for (const dependency of expected.dependencies) {
      assert.ok((claude.dependencies ?? []).includes(dependency));
    }
  }

  const pkg = await json("package.json");
  assert.equal(pkg.version, expected.version);
  assert.equal(pkg.homepage, expected.url + "#readme");
  assert.equal(pkg.repository.url, "git+" + expected.url + ".git");
});
