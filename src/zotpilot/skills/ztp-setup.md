---
name: ztp-setup
description: >
  Use for setting up, updating, or repairing ZotPilot.
  Trigger on: "安装ZotPilot", "配置嵌入模型", "注册MCP", "ZotPilot无法启动", "升级ZotPilot",
  "install zotpilot", "setup zotpilot", "configure embedding provider",
  "register MCP", "zotpilot not found", "zotpilot doctor", "update zotpilot", "zotpilot upgrade",
  or when the user is setting up for the first time, after an upgrade, or when commands are broken.
  Covers: setup → provider selection → API key config → MCP/skill registration → initial index → health check, plus upgrade.
---
# Setup Workflow

## Steps
1. Check installation: `python scripts/run.py status --json` or `uv run zotpilot status`
2. If not installed: `pip install zotpilot` or `uv tool install zotpilot`
3. If installed but needing updates (Upgrade workflow): run `zotpilot upgrade`
4. **Provider Selection**: Determine the user's preferred embedding platform.
   - **gemini**: Requires Google API key. Paid, but provides high-quality embeddings.
   - **dashscope**: Aliyun service. Preferred for Chinese users.
   - **local**: No API key required, completely private, but indexing runs slowly.
   - **none**: Not accepted by `zotpilot setup --provider`; use `zotpilot config set embedding_provider none` only when intentionally disabling vector indexing.
5. **API Key Setup**: Prefer interactive `zotpilot setup` on shared machines. API keys are stored in `~/.config/zotpilot/config.json`; do not paste or commit that file.
6. Configure: `zotpilot setup --non-interactive --provider [gemini|dashscope|local]`
7. MCP registration and skill deployment are included in `zotpilot setup`. Advanced repair only: `zotpilot install` (alias: `zotpilot register`).
8. Initial Index: `zotpilot index --limit 20` (first-time quick index)
9. Verify health: `zotpilot doctor`

## Troubleshooting
- If Zotero is not natively detected at standard paths during setup, instruct the user to explicitly define it via the flag: `--zotero-dir /path/to/zotero/data`
