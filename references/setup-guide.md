# ZotPilot Setup Guide

Complete setup, configuration, and update instructions for ZotPilot.

## Prerequisites

The user needs:
1. **Python 3.10+**: `python3 --version` (Linux/macOS) or `python --version` (Windows)
2. **uv** (package manager):
   - Linux/macOS: `curl -LsSf https://astral.sh/uv/install.sh | sh`
   - Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
   - Any platform: `pip install uv`
   - After installing on Windows, if `uv` is still not in PATH, that's OK — `run.py` will detect it automatically via `python -m uv`

After installing, retry `python3 scripts/run.py status --json`.

## Verify Zotero

Before setup, check if Zotero is installed on this machine:

- **macOS**: check if `/Applications/Zotero.app` exists, or if `~/Zotero` or `~/Documents/Zotero` contains a `zotero.sqlite` file
- **Windows**: check if `C:\Users\<username>\Zotero\zotero.sqlite` exists, or ask the user
- **Linux**: check if `~/Zotero/zotero.sqlite` exists

**If Zotero is NOT installed:** Tell the user: "ZotPilot requires Zotero to be installed and have been run at least once. Please download Zotero from https://www.zotero.org/download/, install it, add some papers, then come back." Stop here.

## First-Time Setup

This section runs once. After setup, the user must restart their AI agent before MCP tools become available.

**Python command:** Use `python3` on Linux/macOS. On Windows, use `python` (Windows typically does not have `python3` in PATH).

### 1. Configure Zotero + embedding provider

Ask the user: "Which embedding provider do you prefer? Gemini (recommended), DashScope/Bailian (recommended for China), or fully offline (local)?"

**With Gemini (recommended, higher quality):**
```bash
python3 scripts/run.py setup --non-interactive --provider gemini
```
User needs `GEMINI_API_KEY` — get one free at https://aistudio.google.com/apikey

**With DashScope / Bailian (recommended for China):**
```bash
python3 scripts/run.py setup --non-interactive --provider dashscope
```
User needs `DASHSCOPE_API_KEY` — get one at https://bailian.console.aliyun.com/

**Without API key (fully offline):**
```bash
python3 scripts/run.py setup --non-interactive --provider local
```

If auto-detection of Zotero fails, add `--zotero-dir /path/to/Zotero`.

### 2. Configure Zotero Web API (for write operations)

Interactive `zotpilot setup` now asks this directly. This section is the explicit fallback path for scripted/non-interactive installs.

Ask the user: "Do you want to be able to tag and organize papers from AI? If yes, you'll need a Zotero API key."

If yes:
1. Go to **https://www.zotero.org/settings/keys**
2. **User ID**: The numeric ID shown at the top of the page (e.g. `12345678`). This is NOT your username — it's a number.
3. Click **"Create new private key"**, check "Allow library access" + "Allow write access", save
4. Copy the generated key

Store the shared, non-secret part in ZotPilot config:
```bash
zotpilot config set zotero_user_id 12345678
```

Store the secret in ZotPilot's secure credential store, then sync detected clients:
```bash
zotpilot config set zotero_api_key <key>
python3 scripts/run.py register
```

Only for migration/automation compatibility, you can still pass it directly at registration:
```bash
python3 scripts/run.py register --zotero-api-key <key> --zotero-user-id 12345678
```

If no, skip — search/read tools will still work without it.

### 3. Register MCP server

**Preferred:** configure via `setup` or `config set`, then register:

```bash
python3 scripts/run.py setup --non-interactive --provider gemini
# or afterwards:
zotpilot config set gemini_api_key <key>
zotpilot config set zotero_api_key <key>
zotpilot config set zotero_user_id <numeric-id>
```

Then register:

```bash
python3 scripts/run.py register
```

**Compatibility path:** pass keys as CLI flags (supported for automation / migration, not recommended for interactive use):

```bash
python3 scripts/run.py register \
  --gemini-key <key> \
  --zotero-api-key <key> \
  --zotero-user-id <numeric-id>
```

This auto-detects the user's AI agent platform(s) and registers accordingly. Currently supported: Claude Code, Codex CLI, and OpenCode.

**Important:** registration no longer writes embedded secrets into client config. The client only stores the ZotPilot MCP command; ZotPilot reads secrets from its secure store at runtime.

**Specify platform explicitly:** `python3 scripts/run.py register --platform claude-code`

### 4. Restart

Tell the user: "Setup complete! Please restart your AI agent to activate ZotPilot's tools. After restarting, ask me again and I'll index your papers."

**IMPORTANT:** Stop here. Do NOT attempt to use MCP tools (search_papers, etc.) until the user restarts. The MCP server is not available until after restart.

## Index

MCP tools are now available. Index the user's papers:

```bash
python3 scripts/run.py index
```

Indexing takes ~2-5 seconds per paper. Documents longer than 40 pages are automatically skipped (configurable via `--max-pages`).

For testing with a small subset: `python3 scripts/run.py index --limit 10`

Verify: `zotpilot status` should show "Documents: N" with N > 0.

### Long document handling

After indexing completes, check the output for "Skipped N long documents". If long documents were skipped:

1. Show the user the list of skipped documents (titles and page counts from the output)
2. Ask: "The following long documents (over 40 pages) were skipped. Would you like to index any of them?"
3. If user wants specific papers: `python3 scripts/run.py index --item-key KEY`
4. If user wants all of them: `python3 scripts/run.py index --max-pages 0`

## Update

### v0.3.0+ (recommended)

```bash
zotpilot update
```

Updates both CLI and skill files. Use `--check` to see if an update is available without installing.

### Older versions (manual)

**pip install:**
```bash
pip install --upgrade zotpilot
```

**Source checkout:**
```bash
cd <skill-directory>
git pull
```

### Verify after update

After restart, test by asking: "How many papers are in my Zotero library?"

If the AI can answer using `get_index_stats` or `get_library_overview`, the update is complete.
