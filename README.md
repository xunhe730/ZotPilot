<div align="center">

# ZotPilot

**Your Zotero library, supercharged with AI.**

Ask questions. Find patterns. Organize effortlessly.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://python.org)
[![MCP](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io)
[![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg)](#)

English | [中文](README_CN.md)

</div>

---

ZotPilot connects your Zotero library to AI assistants like Claude, letting you **search by meaning**, not just keywords. It runs locally, reads your Zotero database directly, and gives your AI the power to understand your entire research collection.

## The Problem

You have hundreds of papers in Zotero. You *know* you read something about "the relationship between sleep spindles and memory consolidation" — but Zotero's search only matches exact words. You can't search by *meaning*.

## The Solution

```
You:    "Find papers about how sleep affects memory formation"
Claude: Found 8 relevant papers. The strongest match is in Smith et al. (2023),
        specifically in the Results section (p.12): "Sleep spindle density during
        Stage 2 NREM correlated significantly with overnight memory improvement
        (r=0.67, p<0.001)..."
```

ZotPilot indexes your PDFs into semantic vectors, so AI can find relevant passages even when the exact words don't match your query.

## Quick Start (3 minutes)

```bash
# 1. Install
git clone https://github.com/xunhe730/ZotPilot.git
uv tool install ./ZotPilot

# 2. Setup (auto-detects your Zotero library)
zotpilot setup

# 3. Index your papers
zotpilot index

# 4. Add to Claude Code
```

Add to your MCP client config (Claude Code, Cursor, OpenCode, etc.):

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

That's it. Now ask Claude anything about your papers.

## What Can It Do?

### Search by meaning, not keywords

> "Find studies comparing deep learning and traditional methods for EEG classification"

Returns ranked passages from across your library, with full context, citation keys, and page numbers.

### Search tables and figures

> "Show me tables with classification accuracy results"

Finds data tables across all your papers — extracted from PDFs, not just captions.

### Explore citation networks

> "What papers cite Smith et al. 2023? And what do they reference?"

Uses [OpenAlex](https://openalex.org/) to map the citation graph around any paper in your library.

### Organize your library with AI

> "Tag all papers about transformers with 'deep-learning' and add them to the 'Neural Networks' collection"

Reads and writes tags, collections — all through natural language.

### Smart ranking

Results are ranked by a composite score that considers:
- **Semantic similarity** to your query
- **Section relevance** (results/methods > introduction/references)
- **Journal quality** (Q1 journals weighted higher via SCImago)

## 24 MCP Tools

| Category | Tools | What they do |
|----------|-------|-------------|
| **Search** | `search_papers` `search_topic` `search_boolean` `search_tables` `search_figures` | Find passages, papers, tables, and figures |
| **Context** | `get_passage_context` | Expand any search result with surrounding text |
| **Library** | `list_collections` `get_collection_papers` `list_tags` `get_paper_details` `get_library_overview` | Browse your Zotero library |
| **Index** | `index_library` `get_index_stats` | Build and monitor the search index |
| **Citations** | `find_citing_papers` `find_references` `get_citation_count` | Explore citation graphs via OpenAlex |
| **Write** | `set_item_tags` `add_item_tags` `remove_item_tags` `add_to_collection` `remove_from_collection` `create_collection` | Organize your library |
| **Admin** | `get_reranking_config` `get_vision_costs` | Configuration and monitoring |

## How It Works

```
┌─────────────┐     ┌──────────┐     ┌─────────────┐     ┌──────────┐
│ Zotero      │────>│ PDF      │────>│ Embeddings  │────>│ ChromaDB │
│ SQLite DB   │     │ Extractor│     │ (Gemini /   │     │ Vector   │
│ (read-only) │     │ + OCR    │     │  Local)     │     │ Store    │
└─────────────┘     └──────────┘     └─────────────┘     └──────────┘
                                                               │
┌─────────────┐     ┌──────────┐     ┌─────────────┐          │
│ AI Client   │<────│ Reranker │<────│ Retriever   │<─────────┘
│ (Claude,    │     │ (section │     │ (semantic   │
│  Cursor...) │     │  +journal│     │  search)    │
└─────────────┘     │  weights)│     └─────────────┘
                    └──────────┘
```

**Key design choices:**
- **Local-first** — your papers never leave your machine
- **Read-only SQLite** — safe even while Zotero is running
- **Asymmetric embeddings** — separate encodings for documents vs queries (Gemini)
- **Section-aware** — knows if a passage is from Methods, Results, or References

## Embedding Options

| Provider | API Key | Speed | Quality | Offline |
|----------|---------|-------|---------|---------|
| **Gemini** `gemini-embedding-001` | Required (free tier available) | Fast | Best (MTEB #1) | No |
| **Local** `all-MiniLM-L6-v2` | None needed | Moderate | Good | Yes |

## Platform Support

| | macOS | Linux | Windows |
|---|:---:|:---:|:---:|
| Core search | Yes | Yes | Yes |
| Zotero detection | Yes | Yes | Yes |
| PDF extraction | Yes | Yes | Yes |
| PaddleOCR (optional) | Yes | Yes | Partial |

## Development

```bash
git clone https://github.com/xunhe730/ZotPilot.git
cd ZotPilot
uv sync --extra dev
uv run pytest              # 106 tests
uv run ruff check src/     # Lint
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to add new embedding providers, MCP tools, or fix bugs.

## Roadmap

- [x] Gemini + local embedding providers
- [x] 24 MCP tools (search, index, citations, write)
- [x] Section-aware reranking with journal quality
- [x] Cross-platform support (macOS, Linux, Windows)
- [ ] OpenAI embedding provider
- [ ] Ollama embedding provider (fully local LLM)
- [ ] `zotpilot doctor` diagnostic command
- [ ] PyPI publishing (`pip install zotpilot`)
- [ ] Embedding model comparison guide

## Star History

If ZotPilot helps your research, consider giving it a star — it helps others find it too.

## License

MIT — see [LICENSE](LICENSE).

---

<div align="center">

**Built for researchers who want AI to actually understand their papers.**

[Report Bug](https://github.com/xunhe730/ZotPilot/issues) | [Request Feature](https://github.com/xunhe730/ZotPilot/issues) | [Discussions](https://github.com/xunhe730/ZotPilot/discussions)

</div>
