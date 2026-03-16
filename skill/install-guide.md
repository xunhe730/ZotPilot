---
name: zotpilot-install
description: Use when the user wants to install, set up, or configure ZotPilot, or when ZotPilot MCP tools are not available in the current session but the user is asking about Zotero, papers, or their research library. Also trigger when the user says "set up zotpilot", "install zotpilot", "configure zotero", or asks why zotpilot tools aren't working.
---

# ZotPilot Installation Guide

Guide the user through installation step by step. Do not dump all instructions at once — check each prerequisite, handle errors, and confirm each step succeeded before moving on.

## Step 1: Check prerequisites

Run these checks and report results:

```bash
python3 --version    # Need 3.10+
uv --version         # Need uv installed
```

If Python < 3.10: tell user to upgrade. Link: https://python.org
If uv missing: tell user to install. One-liner: `curl -LsSf https://astral.sh/uv/install.sh | sh`

## Step 2: Install ZotPilot

```bash
git clone https://github.com/xunhe730/ZotPilot.git
uv tool install ./ZotPilot
```

Verify: `zotpilot status` should print config info (may show errors about missing API key — that's OK at this stage).

## Step 3: Run setup wizard

```bash
zotpilot setup
```

This auto-detects the Zotero data directory, lets the user choose embedding provider, and writes config.

If the user prefers manual setup, they need:
1. Find their Zotero data directory (contains `zotero.sqlite`)
   - macOS: `~/Zotero` or check Zotero → Preferences → Advanced → Data Directory
   - Linux: `~/Zotero`
   - Windows: `C:\Users\<name>\Zotero`
2. Create config at `~/.config/zotpilot/config.json` (macOS/Linux) or `%APPDATA%\zotpilot\config.json` (Windows):
```json
{
  "zotero_data_dir": "/path/to/Zotero",
  "embedding_provider": "gemini"
}
```
3. Set `GEMINI_API_KEY` environment variable (get from https://aistudio.google.com/apikey)

## Step 4: Index the library

```bash
zotpilot index
```

This takes a while on first run (processes all PDFs). For testing, use `zotpilot index --limit 10`.

Verify: `zotpilot status` should show "Documents: N" with N > 0.

## Step 5: Configure MCP client

Determine which client the user has and write the appropriate config:

**Claude Code** — add to `~/.claude.json` under `projects.<cwd>.mcpServers`:
```json
{
  "zotpilot": {
    "type": "stdio",
    "command": "zotpilot",
    "args": [],
    "env": {
      "GEMINI_API_KEY": "the-users-key"
    }
  }
}
```

**Cursor** — add to `.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "zotpilot": {
      "command": "uv",
      "args": ["tool", "run", "zotpilot"],
      "env": { "GEMINI_API_KEY": "..." }
    }
  }
}
```

**Windsurf** — add to `~/.codeium/windsurf/mcp_config.json` (same format as Cursor).

After configuring, the user needs to restart their AI client for the MCP server to connect.

## Step 6: Verify

After restart, test by asking: "How many papers are in my Zotero library?"

If the AI can answer using `get_index_stats` or `get_library_overview`, installation is complete.

## Optional: Install the Skill

For Claude Code users, install the agent skill for guided research workflows:

```bash
cp -r ZotPilot/skill/ ~/.claude/skills/zotpilot/
```

## Optional: Enable write operations

For tag/collection management, the user needs a Zotero Web API key:
1. Go to https://www.zotero.org/settings/keys
2. Create a new key with "Allow library access" and "Allow write access"
3. Add to MCP config env: `"ZOTERO_API_KEY": "...", "ZOTERO_USER_ID": "..."`

User ID is the number shown at https://www.zotero.org/settings/keys (not the username).
