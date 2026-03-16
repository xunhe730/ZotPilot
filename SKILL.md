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

This auto-installs the ZotPilot CLI if not present. Parse the JSON output:

- Command fails entirely → go to "Prerequisites"
- `errors` is non-empty → report errors to user, consult `references/troubleshooting.md`
- `index_ready` is false or `doc_count` is 0 → go to "Index"
- All green → go to "Research"

## Prerequisites (if run.py fails)

The user needs:
1. **Python 3.10+**: `python3 --version`
2. **uv**: `curl -LsSf https://astral.sh/uv/install.sh | sh`

After installing, retry Step 1.

## Setup (if status shows config errors)

### 1. Configure

```bash
python3 scripts/run.py setup --non-interactive
```

- If `gemini_key_set` is false and user wants Gemini embeddings: ask for GEMINI_API_KEY first
- For offline mode (no API key needed): add `--provider local`
- To specify Zotero path: add `--zotero-dir /path/to/Zotero`

### 2. Register MCP server

**Claude Code:**
```bash
claude mcp add -s user zotpilot -- zotpilot
```
If Gemini embeddings, add the API key:
```bash
claude mcp add -s user -e GEMINI_API_KEY=<key> zotpilot -- zotpilot
```

**OpenCode:**
```bash
opencode mcp add
```
Then enter: name=`zotpilot`, command=`zotpilot`, transport=`stdio`.

**OpenClaw:**
1. Install MCP bridge: `openclaw plugins install @aiwerk/openclaw-mcp-bridge`
2. Edit `~/.openclaw/openclaw.json`, add under `plugins.entries.openclaw-mcp-bridge.config.servers`:
```json
"zotpilot": {
  "transport": "stdio",
  "command": "zotpilot",
  "args": [],
  "env": { "GEMINI_API_KEY": "${GEMINI_API_KEY}" }
}
```

### 3. Restart

Tell user to restart their AI agent (or run `/mcp` in Claude Code to reconnect).

## Index (if doc_count = 0)

```bash
python3 scripts/run.py index --limit 10    # quick test first
python3 scripts/run.py index               # full index
```

After indexing, proceed to the user's original request.

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
| "GEMINI_API_KEY not set" | User must set env var |
| "ZOTERO_API_KEY not set" | Write ops need this — see `references/install-steps.md` |
| "Document has no DOI" | Cannot use citation tools for this paper |
| "No chunks found" | Paper not indexed — run `index_library(item_key="...")` |

For detailed parameter reference and advanced patterns, see `references/tool-guide.md`.
For common issues and fixes, see `references/troubleshooting.md`.
