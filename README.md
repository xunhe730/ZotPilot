# ZotPilot

English | [中文](README_CN.md)

AI-powered semantic search for your Zotero research library. ZotPilot is an MCP server that lets AI assistants search, analyze, and organize your academic papers.

## Why ZotPilot?

Zotero's built-in search is keyword-only. ZotPilot adds:

- **Semantic search** — find papers by meaning, not just keywords
- **Table & figure search** — find data across your entire library
- **Citation graphs** — discover who cites what via OpenAlex
- **Section-aware ranking** — prioritize results from methods, results, or conclusions
- **Journal quality weighting** — boost results from higher-impact journals
- **Library management** — organize papers with tags and collections via AI

## Quick Start

### For Researchers

```bash
# Install
git clone https://github.com/xunhe730/ZotPilot.git
uv tool install ./ZotPilot

# Setup (auto-detects Zotero, configures embeddings)
zotpilot setup

# Index your library
zotpilot index

# Add to Claude Code
# Edit ~/.claude/settings.json — see below
```

### For Developers

```bash
git clone https://github.com/xunhe730/ZotPilot.git
cd ZotPilot
uv sync --extra dev
uv run pytest
```

## MCP Client Configuration

### Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "zotpilot": {
      "command": "uv",
      "args": ["tool", "run", "zotpilot"]
    }
  }
}
```

### OpenCode / OpenClaw

Same configuration format — add the `zotpilot` MCP server to your client's config.

## Features

### 24 MCP Tools

| Category | Tools |
|----------|-------|
| **Search** | `search_papers`, `search_topic`, `search_boolean`, `search_tables`, `search_figures` |
| **Context** | `get_passage_context` |
| **Library** | `list_collections`, `get_collection_papers`, `list_tags`, `get_paper_details`, `get_library_overview` |
| **Indexing** | `index_library`, `get_index_stats` |
| **Citations** | `find_citing_papers`, `find_references`, `get_citation_count` |
| **Write** | `set_item_tags`, `add_item_tags`, `remove_item_tags`, `add_to_collection`, `remove_from_collection`, `create_collection` |
| **Admin** | `get_reranking_config`, `get_vision_costs` |

### Embedding Providers

| Provider | API Key | Dimensions | Quality |
|----------|---------|------------|---------|
| Gemini (`gemini-embedding-001`) | Required | 768 | Best (MTEB #1) |
| Local (`all-MiniLM-L6-v2`) | None | 384 | Good (offline) |

### Agent Skill

Install the `skill/` directory for guided workflows:
- Automatic search strategy selection
- Literature review templates
- Library organization workflows

## Architecture

```
Zotero SQLite → zotero_client → indexer → pdf/ → embeddings → vector_store (ChromaDB)
                                                                      ↓
query → embeddings → vector_store → reranker → response
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for details.

## License

MIT — see [LICENSE](LICENSE).
