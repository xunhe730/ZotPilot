# ZotPilot Installation Steps

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

## Step 5: Register MCP server

Set the API key as an environment variable first (recommended):

```bash
export GEMINI_API_KEY=<the-users-key>
python3 scripts/run.py register
```

Alternative (key appears in shell history):

```bash
python3 scripts/run.py register --gemini-key <the-users-key>
```

This auto-detects the AI agent platform and registers the MCP server. Supports Claude Code, Codex CLI, OpenCode, Gemini CLI, Cursor, Windsurf, Cline, and Roo Code.

If auto-detection fails, specify explicitly: `python3 scripts/run.py register --platform claude-code`

After registration, the user needs to restart their AI agent for the MCP server to connect.

## Step 6: Verify

After restart, test by asking: "How many papers are in my Zotero library?"

If the AI can answer using `get_index_stats` or `get_library_overview`, installation is complete.

## Optional: Enable write operations

For tag/collection management, the user needs a Zotero Web API key:
1. Go to https://www.zotero.org/settings/keys
2. Create a new key with "Allow library access" and "Allow write access"
3. Add to MCP config env: `"ZOTERO_API_KEY": "...", "ZOTERO_USER_ID": "..."`

User ID is the number shown at https://www.zotero.org/settings/keys (not the username).
