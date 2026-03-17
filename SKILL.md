---
name: zotpilot
description: Use when user mentions Zotero, academic papers, citations,
  literature reviews, research libraries, or wants to search/organize their
  paper collection. Also triggers on "find papers about...", "what's in my
  library", "organize my papers", "who cites...", "tag these papers".
  Always use this skill for Zotero-related tasks.
---

# ZotPilot

> All script paths are relative to this skill's directory.

## Step 1: Check readiness

Run: `python3 scripts/run.py status --json`

This auto-installs the ZotPilot CLI if not present. Parse the JSON output and follow the FIRST matching branch:

1. Command fails entirely → go to **Prerequisites**

If any errors or unexpected behavior: run `python3 scripts/run.py doctor` for detailed diagnostics.
2. `config_exists` is false → go to **First-Time Setup**
3. `errors` is non-empty → go to **First-Time Setup** (likely missing API key or invalid Zotero path)
4. `index_ready` is false or `doc_count` is 0 → go to **Index**
5. All green → go to **Research**

## Prerequisites (if run.py fails)

The user needs:
1. **Python 3.10+**: `python3 --version`
2. **uv**: `curl -LsSf https://astral.sh/uv/install.sh | sh`

After installing, retry Step 1.

## First-Time Setup

This section runs once. After setup, the user must restart their AI agent before MCP tools become available.

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

### 2. Register MCP server

**Claude Code:**
```bash
claude mcp add -s user zotpilot -- zotpilot
```
If using Gemini, pass the key:
```bash
claude mcp add -s user -e GEMINI_API_KEY=<key> zotpilot -- zotpilot
```

**OpenCode:**
```bash
opencode mcp add
```
Enter: name=`zotpilot`, command=`zotpilot`, transport=`stdio`.

**OpenClaw:**
1. `openclaw plugins install @aiwerk/openclaw-mcp-bridge`
2. Edit `~/.openclaw/openclaw.json`, add under `plugins.entries.openclaw-mcp-bridge.config.servers`:
```json
"zotpilot": {
  "transport": "stdio",
  "command": "zotpilot",
  "args": [],
  "env": { "GEMINI_API_KEY": "${GEMINI_API_KEY}" }
}
```

### 3. Tell user to restart

Say: "Setup complete! Please restart your AI agent (or run `/mcp` in Claude Code) to activate ZotPilot's 24 tools. After restarting, ask me again and I'll index your papers and start searching."

**IMPORTANT:** Stop here. Do NOT attempt to use MCP tools (search_papers, etc.) until the user restarts. The MCP server is not available until after restart.

## Index (if doc_count = 0)

MCP tools are now available. Index the user's papers:

```bash
python3 scripts/run.py index
```

Indexing takes ~2-5 seconds per paper. Documents longer than 40 pages are automatically skipped (configurable via `--max-pages`).

### Long document handling

After indexing completes, check the output for "Skipped N long documents". If long documents were skipped:

1. Show the user the list of skipped documents (titles and page counts from the output)
2. Ask: "The following long documents (over 40 pages) were skipped. Would you like to index any of them?"
3. If user wants specific papers: `python3 scripts/run.py index --item-key KEY`
4. If user wants all of them: `python3 scripts/run.py index --max-pages 0`

After completion, proceed to the user's original request.

## Research (daily use)

### Tool selection — pick the RIGHT tool first

| User intent | Tool | Key params |
|---|---|---|
| Find specific passages or evidence | `search_papers` | `query`, `top_k=10`, `section_weights`, `required_terms` |
| Survey a topic / "what do I have on X" | `search_topic` | `query`, `num_papers=10` |
| Find a known paper by name/author | `search_boolean` | `query`, `operator="AND"` |
| Find data tables | `search_tables` | `query` |
| Find figures | `search_figures` | `query` |
| Read more context around a result | `get_passage_context` | `doc_id`, `chunk_index`, `window=3` |
| See all papers | `get_library_overview` | `limit=100`, `offset=0` |
| Paper details | `get_paper_details` | `item_key` |
| Who cites this? | `find_citing_papers` | `doc_id` |
| Tag/organize papers | `add_item_tags`, `add_to_collection` | `item_key` |

### Workflow chains

**Literature review:**
search_topic → get_paper_details (top 5) → find_references → search_papers with section_weights

**"What do I have on X?":**
search_topic(num_papers=20) → report count, year range, key authors, top passages

**Organize by theme:**
search_topic → create_collection → add_to_collection for each match → add_item_tags

**Find specific paper:**
search_boolean first (exact terms) → fallback to search_papers (semantic) → get_paper_details

### Output formatting

- Lead with paper title, authors, year, citation key
- Quote the relevant passage directly
- Include page number and section name
- Group results by paper, not by chunk
- Render table content as markdown tables
- NEVER dump raw JSON to the user

### Error recovery

| Error | Fix |
|---|---|
| Empty results | Try broader query, or `search_boolean` for exact terms. Check `get_index_stats` |
| "GEMINI_API_KEY not set" | User must set env var, or switch to dashscope/local |
| "DASHSCOPE_API_KEY not set" | User must set env var |
| "ZOTERO_API_KEY not set" | Write ops need Zotero Web API credentials — see below |
| "Document has no DOI" | Cannot use citation tools for this paper |
| "No chunks found" | Paper not indexed — run `index_library(item_key="...")` |

### Write operations (tags, collections)

Write tools (`add_item_tags`, `set_item_tags`, `add_to_collection`, `remove_from_collection`, `create_collection`) require Zotero Web API credentials. If user gets "ZOTERO_API_KEY not set":

1. Go to https://www.zotero.org/settings/keys
2. Click "Create new private key"
3. Check "Allow library access" and "Allow write access"
4. Copy the key and note the User ID (number on the same page)
5. Re-register MCP with the credentials:
   ```bash
   claude mcp remove zotpilot
   claude mcp add -s user -e GEMINI_API_KEY=<key> -e ZOTERO_API_KEY=<key> -e ZOTERO_USER_ID=<id> zotpilot -- zotpilot
   ```
6. Restart AI agent

For detailed parameter reference, see `references/tool-guide.md`.
For common issues and fixes, see `references/troubleshooting.md`.
