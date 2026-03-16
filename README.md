<div align="center">

# ZotPilot

**Let AI take over your Zotero.**

Read, search, understand, and organize — all through natural language.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://python.org)
[![MCP](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io)
[![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg)](#)

English | [中文](README_CN.md)

</div>

---

ZotPilot is an MCP server that gives AI assistants **full control over your Zotero library** — not just search, but browsing, organizing, and understanding your entire research collection. It turns Zotero from a passive filing cabinet into an active research partner.

## What makes it different

Most Zotero integrations do one thing: keyword search. ZotPilot does everything:

| | Zotero built-in | Other MCP tools | **ZotPilot** |
|---|:---:|:---:|:---:|
| Keyword search | Yes | Yes | Yes |
| Semantic search (by meaning) | | | **Yes** |
| Search inside tables & figures | | | **Yes** |
| Citation graph exploration | | | **Yes** |
| Section-aware ranking | | | **Yes** |
| Journal quality weighting | | | **Yes** |
| Browse collections & tags | Yes | Some | **Yes** |
| Manage tags & collections | Yes | Some | **Yes** |
| Chinese query support | | | **Yes** |
| Works 100% locally | Yes | | **Yes** |

**One MCP server. Full Zotero access. No plugin required.**

## See it in action

### Search by meaning, not keywords

```
You:    "Find papers about how sleep affects memory formation"
Claude: Found 8 relevant papers. The strongest match is in Smith et al. (2023),
        Results section (p.12): "Sleep spindle density during Stage 2 NREM
        correlated significantly with overnight memory improvement (r=0.67,
        p<0.001)..."
```

### Organize your library through conversation

```
You:    "Tag all papers about deep learning with 'DL' and move them to
        the 'Neural Networks' collection"
Claude: Found 23 papers related to deep learning. Added tag 'DL' to all 23.
        Moved 19 to 'Neural Networks' (4 were already there).
```

### Explore citation networks

```
You:    "What papers cite Wang et al. 2022? Which ones are in Q1 journals?"
Claude: 47 papers cite this work. 12 are from Q1 journals. The most-cited
        (89 citations) is Chen et al. (2023) in Nature Methods...
```

### Find data across all your papers

```
You:    "Show me tables comparing classification accuracy across methods"
Claude: Found 6 tables across 4 papers with accuracy comparisons...
        [Table 3 from Li et al. 2024: CNN 94.2%, Transformer 96.8%, ...]
```

## Quick Start (3 minutes)

```bash
# 1. Install
git clone https://github.com/xunhe730/ZotPilot.git
uv tool install ./ZotPilot

# 2. Setup (auto-detects your Zotero library)
zotpilot setup

# 3. Index your papers
zotpilot index

# 4. Add to your AI client
```

Add to your MCP client config (Claude Code, Cursor, Windsurf, etc.):

```json
{
  "mcpServers": {
    "zotpilot": {
      "command": "uv",
      "args": ["tool", "run", "zotpilot"],
      "env": {
        "GEMINI_API_KEY": "your-key-here"
      }
    }
  }
}
```

That's it. Your AI can now read, search, and organize your entire Zotero library.

## 24 Tools — Full Zotero Coverage

### Search & Discover
| Tool | What it does |
|------|-------------|
| `search_papers` | Semantic search with section/journal weighting, filters by author/year/tag/collection |
| `search_topic` | Find the most relevant papers for a topic, deduplicated by document |
| `search_boolean` | Exact word matching (AND/OR) using Zotero's full-text index |
| `search_tables` | Search table content — headers, cells, captions |
| `search_figures` | Search figure captions and descriptions |
| `get_passage_context` | Expand any result with surrounding paragraphs |

### Browse & Understand
| Tool | What it does |
|------|-------------|
| `get_library_overview` | Paginated list of all papers with indexing status |
| `get_paper_details` | Full metadata: title, authors, year, abstract, DOI, tags, collections |
| `list_collections` | All Zotero folders with hierarchy |
| `get_collection_papers` | Papers in a specific collection |
| `list_tags` | All tags sorted by frequency |
| `get_index_stats` | Index health: documents, chunks, unindexed papers |

### Organize & Write
| Tool | What it does |
|------|-------------|
| `add_item_tags` / `remove_item_tags` | Add or remove tags without affecting existing ones |
| `set_item_tags` | Replace all tags on a paper |
| `add_to_collection` / `remove_from_collection` | Move papers between folders |
| `create_collection` | Create new folders (including nested) |

### Citations & Impact
| Tool | What it does |
|------|-------------|
| `find_citing_papers` | Who cites this paper? (via OpenAlex) |
| `find_references` | What does this paper cite? |
| `get_citation_count` | Citation count and reference count |

### Index & Admin
| Tool | What it does |
|------|-------------|
| `index_library` | Index new/changed papers (incremental) |
| `get_reranking_config` | View and understand ranking weights |
| `get_vision_costs` | Monitor vision API usage for table extraction |

## How It Works

```
┌─────────────┐     ┌──────────┐     ┌─────────────┐     ┌──────────┐
│ Zotero      │────>│ PDF      │────>│ Embeddings  │────>│ ChromaDB │
│ SQLite DB   │     │ Extractor│     │ (Gemini /   │     │ Vector   │
│ (read-only) │     │ + Tables │     │  Local)     │     │ Store    │
│             │     │ + Figures │     └─────────────┘     └────┬─────┘
│             │     │ + OCR    │                               │
│             │     └──────────┘                               │
│             │                                                │
│ Zotero      │     ┌──────────┐     ┌─────────────┐          │
│ Web API     │<────│ Reranker │<────│ Retriever   │<─────────┘
│ (write ops) │     │ section  │     │ semantic    │
│             │     │ +journal │     │ search +    │
└─────────────┘     │ +quality │     │ context     │
                    └──────────┘     └─────────────┘
                          │
                    ┌─────┴─────┐
                    │ AI Client │
                    │ Claude    │
                    │ Cursor    │
                    │ Windsurf  │
                    └───────────┘
```

### Key design choices

- **Local-first** — your papers never leave your machine
- **Read-only SQLite** — safe even while Zotero is running
- **Write via Web API** — tag/collection changes sync back to Zotero
- **Section-aware** — knows if a passage is from Methods, Results, or References
- **Journal quality** — ranks Q1 journal results higher (SCImago data)
- **Chinese support** — auto-translates Chinese queries for bilingual search

## Embedding Options

| Provider | API Key | Speed | Quality | Offline |
|----------|---------|-------|---------|---------|
| **Gemini** `gemini-embedding-001` | Required (free tier available) | Fast | Best (MTEB #1) | No |
| **Local** `all-MiniLM-L6-v2` | None needed | Moderate | Good | Yes |

## Platform Support

| | macOS | Linux | Windows |
|---|:---:|:---:|:---:|
| Core (search, index, organize) | Yes | Yes | Yes |
| Zotero auto-detection | Yes | Yes | Yes |
| PDF + OCR extraction | Yes | Yes | Yes |
| Vision table extraction | Yes | Yes | Yes |
| PaddleOCR (optional) | Yes | Yes | Partial |

## Development

```bash
git clone https://github.com/xunhe730/ZotPilot.git
cd ZotPilot
uv sync --extra dev
uv run pytest              # 106 tests
uv run ruff check src/     # Lint
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for adding embedding providers, tools, or fixes.

## Roadmap

- [x] 24 MCP tools — full Zotero read/write/search coverage
- [x] Semantic search with section-aware reranking
- [x] Table & figure extraction and search
- [x] Citation graph via OpenAlex
- [x] Journal quality weighting (SCImago)
- [x] Chinese query auto-translation
- [x] Cross-platform (macOS, Linux, Windows)
- [ ] OpenAI / Ollama embedding providers
- [ ] PyPI publishing (`pip install zotpilot`)
- [ ] Note generation from search results
- [ ] `zotpilot doctor` diagnostic command

## License

MIT — see [LICENSE](LICENSE).

---

<div align="center">

**One MCP server to rule your entire Zotero library.**

[Report Bug](https://github.com/xunhe730/ZotPilot/issues) · [Request Feature](https://github.com/xunhe730/ZotPilot/issues) · [Discussions](https://github.com/xunhe730/ZotPilot/discussions)

</div>
