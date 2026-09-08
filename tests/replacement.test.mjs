import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

const pluginRoot = path.resolve(import.meta.dirname, "..");

function permissionBits(targetPath) {
  return fs.statSync(targetPath).mode & 0o777;
}

test("legacy MCP config no longer auto-registers during the workaround", () => {
  assert.equal(fs.existsSync(path.join(pluginRoot, ".mcp.json")), false);
});

test("claude metadata no longer registers an MCP server during the workaround", () => {
  const claudeManifest = JSON.parse(
    fs.readFileSync(path.join(pluginRoot, ".claude-plugin", "plugin.json"), "utf8"),
  );

  assert.equal("mcpServers" in claudeManifest, false);
  assert.equal("interface" in claudeManifest, false);
});

test("claude local MCP config no longer auto-registers during the workaround", () => {
  assert.equal(fs.existsSync(path.join(pluginRoot, ".claude-mcp.json")), false);
});

test("codex metadata no longer registers an MCP server during the workaround", () => {
  const codexManifest = JSON.parse(
    fs.readFileSync(path.join(pluginRoot, ".codex-plugin", "plugin.json"), "utf8"),
  );

  assert.equal("mcpServers" in codexManifest, false);
  assert.match(codexManifest.description, /WhatsApp chats/i);
  assert.match(codexManifest.description, /installed whatsapp CLI/i);
});

test("the general skill advertises the installed CLI as the WhatsApp front door", () => {
  const skill = fs.readFileSync(path.join(pluginRoot, "skills", "whatsapp", "SKILL.md"), "utf8");
  const openAiMetadata = fs.readFileSync(
    path.join(pluginRoot, "skills", "whatsapp", "agents", "openai.yaml"),
    "utf8",
  );
  const claudeManifest = JSON.parse(
    fs.readFileSync(path.join(pluginRoot, ".claude-plugin", "plugin.json"), "utf8"),
  );

  assert.match(skill, /description: Use the installed `whatsapp` CLI/i);
  assert.match(skill, /Trigger for any WhatsApp request/i);
  assert.match(skill, /Do not use computer control or a browser/i);
  assert.match(skill, /no native MCP server is registered/i);
  assert.match(openAiMetadata, /allow_implicit_invocation: true/);
  assert.match(openAiMetadata, /Use \$whatsapp to read my latest WhatsApp message/);
  assert.match(claudeManifest.description, /installed whatsapp CLI/i);
});

test("package scripts expose the direct CLI and bridge lifecycle commands", () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(pluginRoot, "package.json"), "utf8"));
  assert.equal(pkg.scripts.start, "/bin/zsh ./scripts/start_bridge.sh");
  assert.equal(pkg.scripts.stop, "/bin/zsh ./scripts/stop_bridge.sh");
  assert.equal(pkg.scripts.status, "/bin/zsh ./scripts/status_bridge.sh");
  assert.equal(pkg.scripts["reset-sync"], "/bin/zsh ./scripts/reset_sync.sh");
  assert.equal(pkg.scripts.cli, "python3 ./cli/whatsapp_cli.py");
  assert.equal(pkg.scripts.backend, "/bin/zsh ./scripts/run_cli.sh");
  assert.equal(pkg.scripts.setup, "/bin/zsh ./scripts/setup.sh");
  assert.equal("setup:bridge" in pkg.scripts, false);
});

test("bridge lifecycle migrates local state to private permissions", () => {
  const stateRoot = fs.mkdtempSync(path.join(os.tmpdir(), "whatsapp-private-state-"));
  const commonEnv = path.join(pluginRoot, "scripts", "common_env.sh");
  const storeDir = path.join(stateRoot, "upstream-store");
  const transcriptDir = path.join(storeDir, "transcripts");
  const existingSession = path.join(storeDir, "whatsapp.db");
  const existingTranscript = path.join(transcriptDir, "memo.transcript.txt");

  try {
    fs.mkdirSync(transcriptDir, { recursive: true, mode: 0o755 });
    fs.writeFileSync(existingSession, "private session state", { mode: 0o644 });
    fs.writeFileSync(existingTranscript, "private transcript", { mode: 0o644 });
    fs.chmodSync(stateRoot, 0o755);
    fs.chmodSync(storeDir, 0o755);
    fs.chmodSync(transcriptDir, 0o755);

    execFileSync(
      "/bin/zsh",
      [
        "-c",
        'source "$1"; printf "new state" > "$STATE_ROOT/new-state.db"',
        "whatsapp-permission-test",
        commonEnv,
      ],
      {
        env: { ...process.env, WHATSAPP_PLUGIN_STATE_ROOT: stateRoot },
        stdio: "pipe",
      },
    );

    for (const privateDir of [
      stateRoot,
      path.join(stateRoot, "logs"),
      storeDir,
      transcriptDir,
      path.join(storeDir, "transcript-locks"),
    ]) {
      assert.equal(permissionBits(privateDir), 0o700, privateDir);
    }
    for (const privateFile of [
      existingSession,
      existingTranscript,
      path.join(stateRoot, "new-state.db"),
    ]) {
      assert.equal(permissionBits(privateFile), 0o600, privateFile);
    }
  } finally {
    fs.rmSync(stateRoot, { recursive: true, force: true });
  }
});

test("bridge environment applies a private umask before starting Go", () => {
  const commonEnv = fs.readFileSync(path.join(pluginRoot, "scripts", "common_env.sh"), "utf8");
  const startBridge = fs.readFileSync(path.join(pluginRoot, "scripts", "start_bridge.sh"), "utf8");

  assert.match(commonEnv, /set -euo pipefail[\s\S]*umask 077/);
  assert.match(startBridge, /source .*common_env\.sh/);
  assert.match(startBridge, /go", "run", "main\.go"/);
});

test("phone pairing normalizes WhatsApp's legacy Mexican mobile prefix", () => {
  const stateRoot = fs.mkdtempSync(path.join(os.tmpdir(), "whatsapp-pair-phone-"));
  const commonEnv = path.join(pluginRoot, "scripts", "common_env.sh");

  try {
    const output = execFileSync(
      "/bin/zsh",
      [
        "-c",
        'source "$1"; resolve_pair_phone; printf "%s" "$WHATSAPP_MCP_PAIR_PHONE"',
        "whatsapp-pair-phone-test",
        commonEnv,
      ],
      {
        env: {
          ...process.env,
          WHATSAPP_PLUGIN_STATE_ROOT: stateRoot,
          WHATSAPP_MCP_PAIR_PHONE: "5215551234567",
        },
        encoding: "utf8",
        stdio: ["ignore", "pipe", "pipe"],
      },
    );

    assert.equal(output, "525551234567");
  } finally {
    fs.rmSync(stateRoot, { recursive: true, force: true });
  }
});

test("bridge media hooks resolve through the stable whatsapp front door", () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "whatsapp-stable-hook-"));
  const stateRoot = path.join(tempRoot, "state", "runtime");
  const binDir = path.join(tempRoot, "bin");
  const frontDoor = path.join(binDir, "whatsapp");
  const commonEnv = path.join(pluginRoot, "scripts", "common_env.sh");

  try {
    fs.mkdirSync(binDir, { recursive: true });
    fs.writeFileSync(frontDoor, "#!/bin/sh\nexit 0\n", { mode: 0o755 });
    const output = execFileSync(
      "/bin/zsh",
      [
        "-c",
        'source "$1"; printf "%s|%s" "$WHATSAPP_MEDIA_ARRIVAL_HOOK" "$WHATSAPP_MEDIA_RECONCILE_HOOK"',
        "whatsapp-stable-hook-test",
        commonEnv,
      ],
      {
        env: {
          ...process.env,
          PATH: `${binDir}:${process.env.PATH}`,
          WHATSAPP_PLUGIN_STATE_ROOT: stateRoot,
          WHATSAPP_MEDIA_ARRIVAL_HOOK: "",
          WHATSAPP_MEDIA_RECONCILE_HOOK: "",
        },
        encoding: "utf8",
        stdio: ["ignore", "pipe", "pipe"],
      },
    );

    assert.equal(output, `${frontDoor}|${frontDoor}`);
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true });
  }
});

test("bridge admin and direct CLI scripts exist", () => {
  for (const scriptName of [
    "start_bridge.sh",
    "stop_bridge.sh",
    "status_bridge.sh",
    "reset_sync.sh",
    "run_cli.sh",
    "whatsapp_cli.py",
  ]) {
    assert.equal(fs.existsSync(path.join(pluginRoot, "scripts", scriptName)), true);
  }
});

test("vendored upstream MCP exposes only read-only tools", () => {
  const mainPy = fs.readFileSync(
    path.join(pluginRoot, "vendor", "lharries-whatsapp-mcp", "whatsapp-mcp-server", "main.py"),
    "utf8",
  );

  assert.match(mainPy, /def search_contacts/);
  assert.match(mainPy, /def list_messages/);
  assert.match(mainPy, /def list_chats/);
  assert.match(mainPy, /def get_message_context/);
  assert.match(mainPy, /def download_media/);

  assert.doesNotMatch(mainPy, /def send_message/);
  assert.doesNotMatch(mainPy, /def send_file/);
  assert.doesNotMatch(mainPy, /def send_audio_message/);
});

test("upstream backend is patched for local state env vars", () => {
  const whatsappPy = fs.readFileSync(
    path.join(pluginRoot, "vendor", "lharries-whatsapp-mcp", "whatsapp-mcp-server", "whatsapp.py"),
    "utf8",
  );
  const bridgeGo = fs.readFileSync(
    path.join(pluginRoot, "vendor", "lharries-whatsapp-mcp", "whatsapp-bridge", "main.go"),
    "utf8",
  );

  assert.match(whatsappPy, /WHATSAPP_MCP_MESSAGES_DB_PATH/);
  assert.match(whatsappPy, /WHATSAPP_MCP_API_BASE_URL/);
  assert.match(bridgeGo, /WHATSAPP_MCP_STORE_DIR/);
  assert.match(bridgeGo, /WHATSAPP_MCP_HTTP_PORT/);
  assert.match(bridgeGo, /api\/health/);
});

test("setup supports fail-closed phone-number pairing for remote mobile use", () => {
  const commonEnv = fs.readFileSync(path.join(pluginRoot, "scripts", "common_env.sh"), "utf8");
  const setupScript = fs.readFileSync(path.join(pluginRoot, "scripts", "setup.sh"), "utf8");
  const resetScript = fs.readFileSync(path.join(pluginRoot, "scripts", "reset_sync.sh"), "utf8");
  const bridgeGo = fs.readFileSync(
    path.join(pluginRoot, "vendor", "lharries-whatsapp-mcp", "whatsapp-bridge", "main.go"),
    "utf8",
  );

  assert.match(commonEnv, /resolve_pair_phone/);
  assert.doesNotMatch(commonEnv, /Developer\/me/);
  assert.match(commonEnv, /WHATSAPP_MCP_PAIR_PHONE_SOURCE_FILE/);
  assert.match(commonEnv, /whatsmeow_device/);
  assert.match(setupScript, /WHATSAPP_USE_PHONE_PAIRING/);
  assert.match(setupScript, /resolve_pair_phone/);
  assert.match(setupScript, /Refusing to fall back to QR pairing/);
  assert.match(resetScript, /resolve_pair_phone/);
  assert.match(resetScript, /Existing WhatsApp sync state was not changed/);
  assert.match(setupScript, /QR pairing enabled/);
  assert.match(setupScript, /Link a device/);
  assert.match(setupScript, /Link with phone number instead/);
  assert.match(setupScript, /WHATSAPP_MCP_REQUEST_FULL_HISTORY/);
  assert.match(setupScript, /WHATSAPP_MCP_HISTORY_SYNC_TIMEOUT_SECS/);
  assert.doesNotMatch(setupScript, /WHATSAPP_MCP_EXIT_AFTER_AUTH_WAIT_SECS/);
  assert.match(bridgeGo, /WHATSAPP_MCP_PAIR_PHONE/);
  assert.match(bridgeGo, /PairPhone/);
  assert.match(bridgeGo, /Pairing code:/);
  assert.match(bridgeGo, /RequireFullSync/);
  assert.match(bridgeGo, /HistorySync_FULL/);
  assert.doesNotMatch(bridgeGo, /Pairing code for %s/);
});

test("reply metadata is persisted and exposed by the vendored backend", () => {
  const whatsappPy = fs.readFileSync(
    path.join(pluginRoot, "vendor", "lharries-whatsapp-mcp", "whatsapp-mcp-server", "whatsapp.py"),
    "utf8",
  );
  const bridgeGo = fs.readFileSync(
    path.join(pluginRoot, "vendor", "lharries-whatsapp-mcp", "whatsapp-bridge", "main.go"),
    "utf8",
  );

  assert.match(bridgeGo, /reply_to_message_id TEXT/);
  assert.match(bridgeGo, /reply_to_sender TEXT/);
  assert.match(bridgeGo, /reply_to_content TEXT/);
  assert.match(bridgeGo, /reply_to_media_type TEXT/);
  assert.match(bridgeGo, /extractReplyMetadata/);

  assert.match(whatsappPy, /reply_to_message_id: Optional\[str\] = None/);
  assert.match(whatsappPy, /reply_to_sender: Optional\[str\] = None/);
  assert.match(whatsappPy, /reply_preview: Optional\[str\] = None/);
  assert.match(whatsappPy, /reply_media_type: Optional\[str\] = None/);
  assert.match(whatsappPy, /LEFT JOIN messages AS reply_target/);
});

test("bridge send endpoint supports outbound quoted replies", () => {
  const bridgeGo = fs.readFileSync(
    path.join(pluginRoot, "vendor", "lharries-whatsapp-mcp", "whatsapp-bridge", "main.go"),
    "utf8",
  );

  assert.match(bridgeGo, /ReplyToMessageID string `json:"reply_to_message_id,omitempty"`/);
  assert.match(bridgeGo, /func buildReplyContext/);
  assert.match(bridgeGo, /StanzaID:\s+proto\.String\(reply\.MessageID\)/);
  assert.match(bridgeGo, /QuotedMessage: quotedMessageFromReply\(reply\)/);
  assert.match(bridgeGo, /ExtendedTextMessage/);
  assert.match(bridgeGo, /applyReplyContext/);
});

test("run_mcp no longer auto-starts the bridge", () => {
  const runScript = fs.readFileSync(path.join(pluginRoot, "scripts", "run_mcp.sh"), "utf8");

  assert.doesNotMatch(runScript, /start_bridge\(/);
  assert.match(runScript, /WhatsApp bridge is not running/);
  assert.match(runScript, /pnpm start/);
});

test("run_mcp requires explicit opt-in for the legacy native MCP path", () => {
  const runScript = fs.readFileSync(path.join(pluginRoot, "scripts", "run_mcp.sh"), "utf8");

  assert.match(runScript, /WHATSAPP_ALLOW_NATIVE_MCP/);
  assert.match(runScript, /disabled by default/i);
  assert.match(runScript, /pnpm cli -- --json chats list/i);
});

test("skill documents direct phone pairing for remote mobile relinks", () => {
  const skill = fs.readFileSync(path.join(pluginRoot, "skills", "whatsapp", "SKILL.md"), "utf8");

  assert.match(skill, /QR pairing flow/i);
  assert.match(skill, /Link a device/i);
  assert.match(skill, /WHATSAPP_USE_PHONE_PAIRING=1/);
  assert.match(skill, /operating remotely from[\s\S]*same phone/i);
  assert.match(skill, /refuses to fall[\s\S]*back to QR/i);
  assert.match(skill, /whatsapp --json chats list/i);
});

test("skill separates message voice from one-message send authorization", () => {
  const skill = fs.readFileSync(path.join(pluginRoot, "skills", "whatsapp", "SKILL.md"), "utf8");
  const readme = fs.readFileSync(path.join(pluginRoot, "README.md"), "utf8");

  assert.match(skill, /User-voice message/i);
  assert.match(skill, /Disclosed delegated conversation/i);
  assert.match(skill, /Hola, soy Codex, el asistente de la persona que me pidió escribirte/i);
  assert.match(skill, /instruction to send, tell, ask, reply, or let a clear recipient know/i);
  assert.match(skill, /exact wording need not be preapproved/i);
  assert.match(skill, /request only to draft, write, or prepare[\s\S]*does not authorize a live send/i);
  assert.match(skill, /"handle the conversation" or "keep going" does not authorize future messages/i);
  assert.match(skill, /never imply that the user personally typed it/i);
  assert.match(skill, /Every outbound message in a disclosed delegated conversation/i);
  assert.match(skill, /Codex: `⌘`/);
  assert.match(skill, /Claude: `✳️`/);
  assert.match(skill, /Never add either marker to a user-voice message/i);
  assert.match(skill, /Do not echo private data back to the user/i);
  assert.match(skill, /commit money or schedule/i);
  assert.match(skill, /Romantic-partner boundary/i);
  assert.match(skill, /default to a disclosed delegated conversation/i);
  assert.match(skill, /User-voice mode is allowed only when the user explicitly asks/i);
  assert.match(skill, /girlfriend, wife, spouse, or romantic\s+partner/i);
  assert.match(skill, /Never treat the romantic-partner default as permission to\s+auto-send/i);
  assert.match(skill, /one-message send authorization never carries forward to another/i);
  assert.match(skill, /Do not hard-code a name, phone number, JID, or relationship\s+status/i);
  assert.match(readme, /clear one-message instruction to send, tell, ask, reply, or let a recipient know/i);
  assert.match(readme, /Draft-only requests and broad continuing delegation do not authorize live sends/i);
  assert.match(readme, /defaults to the disclosed agent template and marker/i);
  assert.match(readme, /For the user's romantic partner, default to disclosed delegation/i);
  assert.match(readme, /mode choice\s+changes the template and disclosure, not whether a send was requested/i);
});

test("skill requires exact Apple Contacts identity resolution before person-scoped sends", () => {
  const skill = fs.readFileSync(path.join(pluginRoot, "skills", "whatsapp", "SKILL.md"), "utf8");
  const readme = fs.readFileSync(path.join(pluginRoot, "README.md"), "utf8");

  assert.match(skill, /Apple Contacts Send Identity Gate/i);
  assert.match(skill, /before every live send to a direct chat/i);
  assert.match(skill, /Reading and local drafting\s+do not require this gate/i);
  assert.match(skill, /missing Contacts relationship label must not\s+block reading/i);
  assert.match(skill, /match_status` is `exact`/i);
  assert.match(skill, /full `e164`[\s\S]*phone candidates/i);
  assert.match(skill, /matching display name[\s\S]*is never identity proof for an outbound send/i);
  assert.match(skill, /registered account JID/i);
  assert.match(skill, /never a name-search fallback/i);
  assert.match(skill, /do not retain a[\s\S]*personal map/i);
  assert.match(skill, /boss, manager, supervisor, and jefe[\s\S]*`manager` relationship label/i);
  assert.match(skill, /more than one manager[\s\S]*valid/i);
  assert.match(skill, /legacy Mexican mobile prefix\s+`\+521`[\s\S]*same ten\s+national digits/i);
  assert.match(skill, /`e164_source: default_country`/i);
  assert.match(skill, /`explicit_legacy_mexico`/i);
  assert.match(skill, /Deduplicate identical `e164` values/i);
  assert.match(skill, /default-country expansion must come from the Contacts plugin/i);
  assert.match(readme, /exactly one direct chat matches/i);
  assert.match(readme, /Display names, nicknames, partial phone suffixes[\s\S]*never\s+sufficient to authorize a live send/i);
  assert.match(readme, /missing relationship label does not\s+block read-only inspection/i);
  assert.match(readme, /Missing, unmatched, or ambiguous relationship[\s\S]*fail closed for outbound delivery/i);
  assert.doesNotMatch(skill, /Before reading a chat selected by/i);
  assert.doesNotMatch(skill, /Do not read or send/i);
  assert.doesNotMatch(readme, /sufficient to read or send/i);
});

test("whatsapp plugin versions stay synchronized", () => {
  const codexManifest = JSON.parse(
    fs.readFileSync(path.join(pluginRoot, ".codex-plugin", "plugin.json"), "utf8"),
  );
  const claudeManifest = JSON.parse(
    fs.readFileSync(path.join(pluginRoot, ".claude-plugin", "plugin.json"), "utf8"),
  );
  const pkg = JSON.parse(fs.readFileSync(path.join(pluginRoot, "package.json"), "utf8"));

  assert.equal(codexManifest.version, "0.11.10");
  assert.equal(claudeManifest.version, codexManifest.version);
  assert.equal(pkg.version, codexManifest.version);
});

test("audio transcription is arrival-triggered and cached through the CLI", () => {
  const cli = fs.readFileSync(path.join(pluginRoot, "cli", "whatsapp_cli.py"), "utf8");
  const skill = fs.readFileSync(path.join(pluginRoot, "skills", "whatsapp", "SKILL.md"), "utf8");
  const readme = fs.readFileSync(path.join(pluginRoot, "README.md"), "utf8");

  assert.match(cli, /media_transcripts/);
  assert.match(cli, /media_transcribe = media_sub\.add_parser/);
  assert.match(cli, /TRANSCRIPT_PROVIDER = "elevenlabs"/);
  assert.match(cli, /WHATSAPP_TRANSCRIPTS_DB_PATH/);
  assert.match(cli, /--refresh/);
  assert.match(skill, /media transcribe MESSAGE_ID/);
  assert.match(skill, /Do not loop `media transcribe` over a listing/i);
  assert.match(readme, /Audio transcription is arrival-triggered and cached/i);
});

test("pending transcription reconciles through the bridge without a second daemon", () => {
  const cli = fs.readFileSync(path.join(pluginRoot, "cli", "whatsapp_cli.py"), "utf8");
  const bridge = fs.readFileSync(
    path.join(pluginRoot, "vendor", "lharries-whatsapp-mcp", "whatsapp-bridge", "main.go"),
    "utf8",
  );
  const env = fs.readFileSync(path.join(pluginRoot, "scripts", "common_env.sh"), "utf8");
  const skill = fs.readFileSync(path.join(pluginRoot, "skills", "whatsapp", "SKILL.md"), "utf8");
  const readme = fs.readFileSync(path.join(pluginRoot, "README.md"), "utf8");

  // Backfill selects only audio with no cached transcript and drains in bounded batches.
  assert.match(cli, /media_sub\.add_parser\(\s*"transcribe-pending"/);
  assert.match(cli, /def pending_audio_messages/);
  assert.match(cli, /def command_media_transcribe_pending/);
  assert.match(cli, /"--dry-run"/);
  assert.match(cli, /"--drain"/);

  // Reconciliation belongs to the long-lived bridge lifecycle, not launchd.
  assert.match(bridge, /func runMediaReconcileHook/);
  assert.match(bridge, /"media", "transcribe-pending",[\s\S]*"--since", since,[\s\S]*"--limit"[\s\S]*"--drain"/);
  assert.match(env, /WHATSAPP_MEDIA_RECONCILE_LOOKBACK_DAYS/);
  assert.match(env, /WHATSAPP_MEDIA_RECONCILE_TIMEOUT_MINUTES/);
  assert.doesNotMatch(cli, /AUTOTRANSCRIBE_LABEL/);
  assert.doesNotMatch(cli, /media_sub\.add_parser\(\s*"autotranscribe"/);

  // The CDN expiry window is why automatic reconciliation must remain documented.
  assert.match(skill, /expires media from\s+its CDN/i);
  assert.match(readme, /expires media from\s+its CDN/i);
});

test("audio transcription is triggered by message arrival, not only by a sweep", () => {
  const bridge = fs.readFileSync(
    path.join(pluginRoot, "vendor", "lharries-whatsapp-mcp", "whatsapp-bridge", "main.go"),
    "utf8",
  );
  const env = fs.readFileSync(path.join(pluginRoot, "scripts", "common_env.sh"), "utf8");
  const cli = fs.readFileSync(path.join(pluginRoot, "cli", "whatsapp_cli.py"), "utf8");
  const readme = fs.readFileSync(path.join(pluginRoot, "README.md"), "utf8");

  // The bridge fires a hook on the message event itself.
  assert.match(bridge, /func runMediaArrivalHook/);
  assert.match(bridge, /runMediaArrivalHook\(mediaType, messageID, chatJID, logger\)/);
  assert.match(bridge, /WHATSAPP_MEDIA_ARRIVAL_HOOK_TYPES/);
  // Detached, so a slow hook cannot stall the event loop.
  assert.match(bridge, /go func\(\) \{[\s\S]*exec\.CommandContext/);
  assert.match(bridge, /"media", "arrival-hook", mediaType, messageID, chatJID/);
  assert.match(bridge, /handleHistorySync[\s\S]*runMediaReconcileHook\(logger\)/);
  assert.match(bridge, /storeSentMessage[\s\S]*runMediaArrivalHook\(mediaType, resp\.ID, recipientJID\.String\(\), logger\)/);

  // The plugin wires the hook to a stable front door rather than a versioned cache path.
  assert.match(env, /resolve_whatsapp_cli_front_door/);
  assert.match(env, /command -v whatsapp/);
  assert.match(env, /WHATSAPP_MEDIA_ARRIVAL_HOOK="\$\{WHATSAPP_MEDIA_ARRIVAL_HOOK:-\$whatsapp_cli_front_door\}"/);
  assert.match(env, /WHATSAPP_MEDIA_RECONCILE_HOOK/);
  assert.match(cli, /media_sub\.add_parser\(\s*"arrival-hook"/);
  assert.match(cli, /def command_media_arrival_hook/);
  // Re-firing on an already transcribed message must not re-spend.
  assert.match(cli, /"already_cached"/);
  assert.match(cli, /transcript_message_lock/);
  assert.match(cli, /WHATSAPP_TRANSCRIBE_RETRY_DELAYS/);

  assert.match(readme, /arrival-triggered and cached/i);
});

test("only permanently unavailable audio leaves automatic reconciliation", () => {
  const cli = fs.readFileSync(path.join(pluginRoot, "cli", "whatsapp_cli.py"), "utf8");
  const readme = fs.readFileSync(path.join(pluginRoot, "README.md"), "utf8");

  // Failures are durable and counted, not just logged.
  assert.match(cli, /CREATE TABLE IF NOT EXISTS media_transcript_failures/);
  assert.match(cli, /def record_transcribe_failure/);
  assert.match(cli, /MAX_TRANSCRIBE_ATTEMPTS = \d+/);
  assert.match(cli, /PERMANENT_TRANSCRIBE_FAILURE_CODES/);
  assert.match(cli, /"media_expired"/);

  // Classification, not a transient attempt threshold, controls exclusion.
  assert.match(cli, /last_code.*PERMANENT_TRANSCRIBE_FAILURE_CODES/s);
  assert.match(cli, /retry_failed or .*not in exhausted/s);
  assert.doesNotMatch(cli, /WHERE attempts >= \?/);
  assert.match(cli, /"--retry-failed"/);

  assert.match(readme, /automatically[\s\S]*regardless of attempt count/i);
  assert.match(readme, /--retry-failed/);
});

test("list_messages returns structured reply metadata", () => {
  const mcpServerDir = path.join(
    pluginRoot,
    "vendor",
    "lharries-whatsapp-mcp",
    "whatsapp-mcp-server",
  );
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "whatsapp-mcp-test-"));
  const tempDbPath = path.join(tempDir, "messages.db");
  const pythonScript = `
import json
import os
import sqlite3
from whatsapp import list_messages

db_path = os.environ["WHATSAPP_MCP_MESSAGES_DB_PATH"]
conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute("CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TEXT)")
cursor.execute("""
CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    chat_jid TEXT NOT NULL,
    sender TEXT NOT NULL,
    content TEXT,
    timestamp TEXT NOT NULL,
    is_from_me INTEGER NOT NULL,
    media_type TEXT,
    reply_to_message_id TEXT,
    reply_to_sender TEXT,
    reply_to_content TEXT,
    reply_to_media_type TEXT
)
""")
cursor.execute(
    "INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
    ("chat@g.us", "Example Launch", "2026-04-09T12:50:14-05:00"),
)
cursor.executemany(
    """
    INSERT INTO messages (
        id,
        chat_jid,
        sender,
        content,
        timestamp,
        is_from_me,
        media_type,
        reply_to_message_id,
        reply_to_sender,
        reply_to_content,
        reply_to_media_type
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    [
        (
            "orig-1",
            "chat@g.us",
            "99900123456789",
            "The uploaded image is getting cropped",
            "2026-04-09T12:09:15-05:00",
            0,
            None,
            None,
            None,
            None,
            None,
        ),
        (
            "reply-1",
            "chat@g.us",
            "99900987654321",
            "I'll check that.",
            "2026-04-09T12:15:18-05:00",
            1,
            None,
            "orig-1",
            "99900123456789",
            "The uploaded image is getting cropped",
            None,
        ),
    ],
)
conn.commit()
conn.close()

messages = list_messages(chat_jid="chat@g.us", include_context=False, limit=10, page=0)
print(json.dumps({
    "type": type(messages).__name__,
    "items": [message.__dict__ for message in messages],
}, default=str))
`;

  const output = execFileSync(
    "uv",
    ["run", "python", "-c", pythonScript],
    {
      cwd: mcpServerDir,
      encoding: "utf8",
      env: {
        ...process.env,
        WHATSAPP_MCP_MESSAGES_DB_PATH: tempDbPath,
      },
    },
  );

  const parsed = JSON.parse(output.trim());
  assert.equal(parsed.type, "list");
  assert.equal(Array.isArray(parsed.items), true);
  assert.equal(parsed.items[0].reply_to_message_id, "orig-1");
  assert.equal(
    parsed.items[0].reply_preview,
    "The uploaded image is getting cropped",
  );
});

test("list_messages returns reactions and read receipts as structured data", () => {
  const mcpServerDir = path.join(
    pluginRoot,
    "vendor",
    "lharries-whatsapp-mcp",
    "whatsapp-mcp-server",
  );
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "whatsapp-mcp-reactions-test-"));
  const tempDbPath = path.join(tempDir, "messages.db");
  const pythonScript = `
import json
import os
import sqlite3
from dataclasses import asdict
from whatsapp import list_messages

db_path = os.environ["WHATSAPP_MCP_MESSAGES_DB_PATH"]
conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute("CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TEXT)")
cursor.execute("""
CREATE TABLE messages (
    id TEXT,
    chat_jid TEXT NOT NULL,
    sender TEXT NOT NULL,
    content TEXT,
    timestamp TEXT NOT NULL,
    is_from_me INTEGER NOT NULL,
    media_type TEXT,
    reply_to_message_id TEXT,
    reply_to_sender TEXT,
    reply_to_content TEXT,
    reply_to_media_type TEXT,
    PRIMARY KEY (id, chat_jid)
)
""")
cursor.execute("""
CREATE TABLE message_reactions (
    chat_jid TEXT NOT NULL,
    target_message_id TEXT NOT NULL,
    target_sender TEXT NOT NULL DEFAULT '',
    reaction_sender TEXT NOT NULL,
    emoji TEXT NOT NULL,
    reaction_message_id TEXT,
    grouping_key TEXT,
    sender_timestamp_ms INTEGER,
    timestamp TEXT,
    is_from_me INTEGER,
    PRIMARY KEY (chat_jid, target_message_id, reaction_sender)
)
""")
cursor.execute("""
CREATE TABLE message_receipts (
    message_id TEXT NOT NULL,
    chat_jid TEXT NOT NULL,
    receipt_type TEXT NOT NULL,
    receipt_sender TEXT NOT NULL,
    message_sender TEXT NOT NULL DEFAULT '',
    timestamp TEXT,
    PRIMARY KEY (message_id, chat_jid, receipt_type, receipt_sender, message_sender)
)
""")
cursor.execute(
    "INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
    ("chat@s.whatsapp.net", "Example", "2026-04-09T12:50:14-05:00"),
)
cursor.execute(
    """
    INSERT INTO messages (
        id, chat_jid, sender, content, timestamp, is_from_me, media_type,
        reply_to_message_id, reply_to_sender, reply_to_content, reply_to_media_type
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    ("out-1", "chat@s.whatsapp.net", "me", "Please review this", "2026-04-09T12:15:18-05:00", 1, None, None, None, None, None),
)
cursor.execute(
    """
    INSERT INTO message_reactions (
        chat_jid, target_message_id, target_sender, reaction_sender, emoji,
        reaction_message_id, grouping_key, sender_timestamp_ms, timestamp, is_from_me
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    ("chat@s.whatsapp.net", "out-1", "me", "15551230001@s.whatsapp.net", "+1", "react-1", "grp", 1775747718000, "2026-04-09T12:16:18-05:00", 0),
)
cursor.execute(
    """
    INSERT INTO message_receipts (
        message_id, chat_jid, receipt_type, receipt_sender, message_sender, timestamp
    ) VALUES (?, ?, ?, ?, ?, ?)
    """,
    ("out-1", "chat@s.whatsapp.net", "read", "15551230001@s.whatsapp.net", "me", "2026-04-09T12:17:18-05:00"),
)
conn.commit()
conn.close()

messages = list_messages(chat_jid="chat@s.whatsapp.net", include_context=False, limit=10, page=0)
print(json.dumps({
    "items": [asdict(message) for message in messages],
}, default=str))
`;

  const output = execFileSync(
    "uv",
    ["run", "python", "-c", pythonScript],
    {
      cwd: mcpServerDir,
      encoding: "utf8",
      env: {
        ...process.env,
        WHATSAPP_MCP_MESSAGES_DB_PATH: tempDbPath,
      },
    },
  );

  const parsed = JSON.parse(output.trim());
  assert.equal(parsed.items[0].reactions.length, 1);
  assert.equal(parsed.items[0].reactions[0].emoji, "+1");
  assert.equal(parsed.items[0].reactions[0].sender, "15551230001@s.whatsapp.net");
  assert.equal(parsed.items[0].receipts.length, 1);
  assert.equal(parsed.items[0].receipts[0].type, "read");
  assert.equal(parsed.items[0].seen_by.length, 1);
  assert.equal(parsed.items[0].seen_by[0].sender, "15551230001@s.whatsapp.net");
});

test("list_messages merges phone and LID histories for one contact by default", () => {
  const mcpServerDir = path.join(
    pluginRoot,
    "vendor",
    "lharries-whatsapp-mcp",
    "whatsapp-mcp-server",
  );
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "whatsapp-mcp-identity-test-"));
  const tempDbPath = path.join(tempDir, "messages.db");
  const pythonScript = `
import json
import os
import sqlite3
from whatsapp import list_messages

messages_db_path = os.environ["WHATSAPP_MCP_MESSAGES_DB_PATH"]
whatsapp_db_path = os.path.join(os.path.dirname(messages_db_path), "whatsapp.db")

conn = sqlite3.connect(messages_db_path)
cursor = conn.cursor()
cursor.execute("CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TEXT)")
cursor.execute("""
CREATE TABLE messages (
    id TEXT,
    chat_jid TEXT NOT NULL,
    sender TEXT NOT NULL,
    content TEXT,
    timestamp TEXT NOT NULL,
    is_from_me INTEGER NOT NULL,
    media_type TEXT,
    reply_to_message_id TEXT,
    reply_to_sender TEXT,
    reply_to_content TEXT,
    reply_to_media_type TEXT,
    PRIMARY KEY (id, chat_jid)
)
""")
cursor.executemany(
    "INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
    [
        ("15551230001@s.whatsapp.net", "Acme Ops", "2026-05-14T18:14:57-05:00"),
        ("99900123456789@lid", "unresolved-lid", "2026-05-20T21:58:17-05:00"),
    ],
)
cursor.executemany(
    """
    INSERT INTO messages (
        id, chat_jid, sender, content, timestamp, is_from_me, media_type,
        reply_to_message_id, reply_to_sender, reply_to_content, reply_to_media_type
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    [
        ("old-1", "15551230001@s.whatsapp.net", "15551230001", "old one", "2026-05-14T18:14:57-05:00", 0, None, None, None, None, None),
        ("old-2", "15551230001@s.whatsapp.net", "me", "old two", "2026-05-14T18:15:57-05:00", 1, None, None, None, None, None),
        ("new-1", "99900123456789@lid", "99900123456789", "new one", "2026-05-20T21:58:17-05:00", 0, None, None, None, None, None),
    ],
)
conn.commit()
conn.close()

conn = sqlite3.connect(whatsapp_db_path)
cursor = conn.cursor()
cursor.execute("CREATE TABLE whatsmeow_lid_map (lid TEXT PRIMARY KEY, pn TEXT UNIQUE NOT NULL)")
cursor.execute("""
CREATE TABLE whatsmeow_contacts (
    our_jid TEXT,
    their_jid TEXT,
    first_name TEXT,
    full_name TEXT,
    push_name TEXT,
    business_name TEXT,
    redacted_phone TEXT,
    PRIMARY KEY (our_jid, their_jid)
)
""")
cursor.execute("INSERT INTO whatsmeow_lid_map (lid, pn) VALUES (?, ?)", ("99900123456789", "15551230001"))
cursor.executemany(
    "INSERT INTO whatsmeow_contacts (our_jid, their_jid, first_name, full_name, push_name, business_name, redacted_phone) VALUES (?, ?, ?, ?, ?, ?, ?)",
    [
        ("me@s.whatsapp.net", "15551230001@s.whatsapp.net", None, "Acme Ops", "Acme Ops", None, None),
        ("me@s.whatsapp.net", "99900123456789@lid", None, None, "Acme Ops", None, None),
    ],
)
conn.commit()
conn.close()

merged = list_messages(chat_jid="99900123456789@lid", include_context=False, limit=10, page=0)
merged_from_phone = list_messages(chat_jid="15551230001@s.whatsapp.net", include_context=False, limit=10, page=0)
exact = list_messages(chat_jid="99900123456789@lid", include_context=False, limit=10, page=0, expand_identity=False)
print(json.dumps({
    "merged": [message.__dict__ for message in merged],
    "merged_from_phone": [message.__dict__ for message in merged_from_phone],
    "exact": [message.__dict__ for message in exact],
}, default=str))
`;

  const output = execFileSync(
    "uv",
    ["run", "python", "-c", pythonScript],
    {
      cwd: mcpServerDir,
      encoding: "utf8",
      env: {
        ...process.env,
        WHATSAPP_MCP_MESSAGES_DB_PATH: tempDbPath,
      },
    },
  );

  const parsed = JSON.parse(output.trim());
  assert.deepEqual(parsed.merged.map((message) => message.id), ["new-1", "old-2", "old-1"]);
  assert.deepEqual(parsed.merged_from_phone.map((message) => message.id), ["new-1", "old-2", "old-1"]);
  assert.deepEqual(parsed.merged.map((message) => message.chat_name), ["Acme Ops", "Acme Ops", "Acme Ops"]);
  assert.deepEqual(parsed.exact.map((message) => message.id), ["new-1"]);
});

test("CLI exposes direct code-backed access without MCP registration", () => {
  const output = execFileSync(
    "python3",
    [path.join(pluginRoot, "cli", "whatsapp_cli.py"), "--help"],
    {
      encoding: "utf8",
      env: process.env,
    },
  );

  assert.match(output, /Read WhatsApp chats/i);
  assert.match(output, /chats/);
  assert.match(output, /media/);
  assert.match(output, /drafts/);
});

test("list_messages surfaces edited_at so a revised message is not read as original", () => {
  const mcpServerDir = path.join(
    pluginRoot,
    "vendor",
    "lharries-whatsapp-mcp",
    "whatsapp-mcp-server",
  );
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "whatsapp-mcp-edited-test-"));
  const tempDbPath = path.join(tempDir, "messages.db");
  const pythonScript = `
import json
import os
import sqlite3
from dataclasses import asdict
from whatsapp import list_messages

db_path = os.environ["WHATSAPP_MCP_MESSAGES_DB_PATH"]
conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute("CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TEXT)")
cursor.execute("""
CREATE TABLE messages (
    id TEXT,
    chat_jid TEXT NOT NULL,
    sender TEXT NOT NULL,
    content TEXT,
    timestamp TEXT NOT NULL,
    is_from_me INTEGER NOT NULL,
    media_type TEXT,
    reply_to_message_id TEXT,
    reply_to_sender TEXT,
    reply_to_content TEXT,
    reply_to_media_type TEXT,
    edited_at TEXT,
    PRIMARY KEY (id, chat_jid)
)
""")
cursor.execute(
    "INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
    ("chat@s.whatsapp.net", "Example", "2026-04-09T12:50:14-05:00"),
)
cursor.executemany(
    """
    INSERT INTO messages (
        id, chat_jid, sender, content, timestamp, is_from_me, media_type,
        reply_to_message_id, reply_to_sender, reply_to_content, reply_to_media_type, edited_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    [
        ("revised-1", "chat@s.whatsapp.net", "15551230001@s.whatsapp.net", "the corrected text",
         "2026-04-09T12:15:18-05:00", 0, None, None, None, None, None, "2026-04-09T12:16:51-05:00"),
        ("untouched-1", "chat@s.whatsapp.net", "15551230001@s.whatsapp.net", "never edited",
         "2026-04-09T12:20:18-05:00", 0, None, None, None, None, None, None),
    ],
)
conn.commit()
conn.close()

messages = list_messages(chat_jid="chat@s.whatsapp.net", include_context=False, limit=10, page=0)
print(json.dumps({
    "items": {message.id: asdict(message) for message in messages},
}, default=str))
`;

  const output = execFileSync(
    "uv",
    ["run", "python", "-c", pythonScript],
    {
      cwd: mcpServerDir,
      encoding: "utf8",
      env: {
        ...process.env,
        WHATSAPP_MCP_MESSAGES_DB_PATH: tempDbPath,
      },
    },
  );

  const parsed = JSON.parse(output.trim());
  assert.equal(parsed.items["revised-1"].content, "the corrected text");
  assert.ok(
    parsed.items["revised-1"].edited_at,
    "an edited message must report edited_at so its text is not read as originally sent",
  );
  assert.equal(parsed.items["untouched-1"].edited_at, null);
});
