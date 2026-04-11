---
name: ztp-setup
description: ZotPilot installation, configuration, and registration
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
   - **none**: Disables vector indexing. Only metadata/SQL search remains available.
5. **API Key Setup**: `zotpilot register --gemini-key KEY` (replace flag based on provider), or instruct user to set environment variables.
6. Configure: `zotpilot setup --non-interactive --provider [selected_provider]`
7. Register MCP: `zotpilot register`
8. Initial Index: `zotpilot index --limit 20` (first-time quick index)
9. Verify health: `zotpilot doctor`

## Troubleshooting
- If Zotero is not natively detected at standard paths during setup or registration, instruct the user to explicitly define it via the flag: `--zotero-dir /path/to/zotero/data`
