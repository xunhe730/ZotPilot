---
name: ztp-setup
description: ZotPilot installation, configuration, registration, and first-index workflow
---

# ztp-setup

Use this workflow for fresh installs, reconfiguration, and upgrades.

Requirements:

- Set `ZOTPILOT_TOOL_PROFILE=extended`
- Before restart, rely on CLI commands rather than MCP tools

Workflow:

1. Detect whether ZotPilot is already installed and which install mode is in use.
2. Recommend an embedding provider: `gemini`, `dashscope`, `local`, or `none`.
3. Run `zotpilot setup --non-interactive --provider <choice>` when configuration needs to be written.
4. Run `zotpilot register` to register MCP and deploy packaged skill files.
5. Tell the user to restart the client after registration or upgrade.
6. After restart, use `get_index_stats` to verify readiness.
7. If indexing is needed, run `index_library` until `has_more=false`.

Rules:

- Do not assume MCP tools are available before restart.
- Prefer `zotpilot update` for upgrades.
- If `embedding_provider=none`, explain that semantic search is disabled but metadata workflows still work.
