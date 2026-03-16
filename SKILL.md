---
name: zotpilot
description: Use when user mentions Zotero, academic papers, citations, literature reviews, research libraries, or wants to search/organize their paper collection. Also triggers on "find papers about...", "what's in my library", "organize my papers", "who cites...", "tag these papers". Always use this skill for Zotero-related tasks.
---

# ZotPilot

## Step 1: Check readiness

Run: `zotpilot status --json`

If command not found → ZotPilot is not installed. Go to "Setup" section.
If command succeeds → parse the JSON output and branch:
- If `errors` is non-empty → report errors to user
- If `index_ready` is false or `doc_count` is 0 → go to "Index" section
- If all green → go to "Ready" section

## Setup (if not installed)

1. Install the MCP server:
   ```bash
   uv tool install ~/.claude/skills/zotpilot
   ```
   (The skill directory contains pyproject.toml)

2. Configure:
   ```bash
   zotpilot setup --non-interactive
   ```
   - If user needs Gemini embeddings (recommended), ask them to set `GEMINI_API_KEY` env var first
   - If they prefer offline embeddings, use: `zotpilot setup --non-interactive --provider local`
   - To specify a custom Zotero path: `--zotero-dir /path/to/Zotero`

3. Tell the user to add this MCP config to their Claude Code settings (`~/.claude.json`):
   ```json
   {
     "mcpServers": {
       "zotpilot": {
         "command": "zotpilot",
         "args": [],
         "env": {
           "GEMINI_API_KEY": "their-key-here"
         }
       }
     }
   }
   ```

4. Tell user to restart Claude Code for MCP tools to become available.

## Index (if installed but doc_count = 0)

Run: `zotpilot index`

For a quick test first: `zotpilot index --limit 10`

After indexing completes, proceed to the user's original request.

## Ready (if index_ready = true)

Read and follow `references/tool-guide.md` in the skill directory for all research tasks.
It contains the complete tool selection decision table, parameter guidance, workflow recipes, and error handling.
