# Repository guidance

- This repository is the canonical source for the `whatsapp` plugin.
- Keep the Codex and Claude manifests synchronized when both are present. The Claude plugin is intentionally absent for Codex-only plugins.
- Marketplace catalogs reference this repository; do not duplicate runtime behavior back into a marketplace repository.
- Keep credentials and personal data out of Git. Preserve stable command names, service labels, and credential identifiers across releases.
- `whatsapp:impersonating` (merged from the retired `writing` plugin) is the cross-channel owner of user-voice wording. The active assistant writes directly; do not add automatic author delegation. Keep private messages, style profiles, and generated style caches out of Git, and preserve the Humanizer provenance in `THIRD_PARTY_NOTICES.md`.
- Bump the plugin version for released behavior changes and run `npm test` before publishing.
