<div align="center">
  <h1>🧭 ZotPilot</h1>
  <h3>Let AI Take Over Your Zotero</h3>
  <p>
    Search by meaning, explore citations, organize with natural language.<br>
    <b>One MCP server. Full Zotero access. No plugin required.</b>
  </p>

  <p>
    <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python">
    <img src="https://img.shields.io/badge/MCP-Compatible-00B265?style=flat-square&logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZD0iTTEyIDJMMiA3bDEwIDUgMTAtNXoiIGZpbGw9IiNmZmYiLz48L3N2Zz4=&logoColor=white" alt="MCP">
    <img src="https://img.shields.io/badge/License-MIT-blue?style=flat-square" alt="License">
  </p>
  <p>
    <img src="https://img.shields.io/badge/macOS-✓-000000?style=flat-square&logo=apple&logoColor=white" alt="macOS">
    <img src="https://img.shields.io/badge/Linux-✓-FCC624?style=flat-square&logo=linux&logoColor=black" alt="Linux">
    <img src="https://img.shields.io/badge/Windows-✓-0078D6?style=flat-square&logo=windows&logoColor=white" alt="Windows">
  </p>
  <p>
    <img src="https://img.shields.io/github/stars/xunhe730/ZotPilot?style=flat-square&logo=github" alt="GitHub stars">
    <img src="https://img.shields.io/github/forks/xunhe730/ZotPilot?style=flat-square&logo=github" alt="GitHub forks">
    <img src="https://img.shields.io/github/v/release/xunhe730/ZotPilot?style=flat-square&logo=github" alt="Latest version">
  </p>
</div>

<p align="center">
  <a href="#-quick-start">Quick Start</a> •
  <a href="#-features">Features</a> •
  <a href="#-24-tools">Tools</a> •
  <a href="#-how-it-works">Architecture</a> •
  <a href="README_CN.md">简体中文</a>
</p>

---

## 👋 Why ZotPilot?

You have hundreds of papers in Zotero. You _know_ you read something about "the relationship between sleep spindles and memory consolidation" — but Zotero only matches exact keywords. You can't search by _meaning_. You can't ask follow-up questions. You can't say "organize these by theme."

**ZotPilot changes that.** It gives your AI assistant full read/write access to your Zotero library — semantic search, citation exploration, table extraction, tag management, and more. All through natural language.

<table>
<tr>
<td width="50%" valign="top">

**Without ZotPilot**
- Manual keyword guessing
- Open each PDF to find data
- Copy-paste tags one by one
- No way to ask "who cites this?"
- Switch between Zotero and AI

</td>
<td width="50%" valign="top">

**With ZotPilot**
- _"Find papers about sleep and memory"_
- _"Show me accuracy comparison tables"_
- _"Tag all DL papers and move to collection"_
- _"Who cites Wang 2022 in Q1 journals?"_
- AI reads your library directly

</td>
</tr>
</table>

---

## ✨ Features

<table>
<tr>
<td width="50%" valign="top">

### 🔍 Semantic Search
Find passages by meaning, not keywords. Results ranked by section relevance and journal quality.

### 📊 Table & Figure Search
Search inside extracted tables and figure captions across your entire library.

### 🌐 Citation Graph
Explore who cites what, find references, check impact — powered by OpenAlex.

</td>
<td width="50%" valign="top">

### 🏷️ Library Management
Add/remove tags, move papers between collections, create folders — all via conversation.

### 🎯 Smart Ranking
Composite scoring: semantic similarity × section weight × journal quality (SCImago).

### 🀄 Chinese Support
Auto-translates Chinese queries for bilingual parallel search.

</td>
</tr>
</table>

### Comparison

| | Zotero Built-in | Other MCP Tools | **ZotPilot** |
|---|:---:|:---:|:---:|
| Keyword search | ✅ | ✅ | ✅ |
| Semantic search (by meaning) | | | ✅ |
| Search tables & figures | | | ✅ |
| Citation graph | | | ✅ |
| Section-aware ranking | | | ✅ |
| Journal quality weighting | | | ✅ |
| Browse collections & tags | ✅ | Partial | ✅ |
| Manage tags & collections | ✅ | Partial | ✅ |
| Chinese query support | | | ✅ |
| 100% local processing | ✅ | | ✅ |

---

## 📥 Quick Start

```bash
# Install
git clone https://github.com/xunhe730/ZotPilot.git
uv tool install ./ZotPilot

# Setup (auto-detects Zotero)
zotpilot setup

# Index your papers
zotpilot index
```

Then add to your MCP client:

<div align="center">
  <table>
    <tr>
      <td align="center"><b>Claude Code</b></td>
      <td align="center"><b>Cursor</b></td>
      <td align="center"><b>Windsurf</b></td>
    </tr>
    <tr>
      <td><code>~/.claude.json</code></td>
      <td><code>.cursor/mcp.json</code></td>
      <td><code>~/.codeium/windsurf/mcp_config.json</code></td>
    </tr>
  </table>
</div>

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

> **Embedding choice:** Gemini (recommended, free tier available) or Local (offline, no API key needed). Choose during `zotpilot setup`.

---

## 🛠️ 24 Tools

### 🔍 Search & Discover

| Tool | Description |
|------|-------------|
| `search_papers` | Semantic search with section/journal weighting and filters |
| `search_topic` | Topic-level paper discovery, deduplicated by document |
| `search_boolean` | Exact word matching (AND/OR) via Zotero's full-text index |
| `search_tables` | Search table headers, cells, and captions |
| `search_figures` | Search figure captions and descriptions |
| `get_passage_context` | Expand any result with surrounding paragraphs |

### 📚 Browse & Understand

| Tool | Description |
|------|-------------|
| `get_library_overview` | Paginated list of all papers with index status |
| `get_paper_details` | Full metadata: title, authors, abstract, DOI, tags |
| `list_collections` | All Zotero folders with hierarchy |
| `get_collection_papers` | Papers in a specific collection |
| `list_tags` | All tags sorted by frequency |
| `get_index_stats` | Index health: documents, chunks, unindexed papers |

### 🏷️ Organize & Write

| Tool | Description |
|------|-------------|
| `add_item_tags` / `remove_item_tags` | Add or remove tags (non-destructive) |
| `set_item_tags` | Replace all tags on a paper |
| `add_to_collection` / `remove_from_collection` | Move papers between folders |
| `create_collection` | Create new folders (supports nesting) |

### 📈 Citations & Impact

| Tool | Description |
|------|-------------|
| `find_citing_papers` | Who cites this paper? (OpenAlex) |
| `find_references` | What does this paper cite? |
| `get_citation_count` | Citation and reference counts |

### ⚙️ Admin

| Tool | Description |
|------|-------------|
| `index_library` | Index new/changed papers (incremental) |
| `get_reranking_config` | View ranking weights |
| `get_vision_costs` | Monitor vision API usage |

---

## 🏗️ How It Works

```
┌─────────────┐     ┌──────────┐     ┌─────────────┐     ┌──────────┐
│   Zotero     │────▶│   PDF    │────▶│ Embeddings  │────▶│ ChromaDB │
│  SQLite DB   │     │ Extract  │     │  Gemini /   │     │  Vector  │
│ (read-only)  │     │ +Tables  │     │   Local     │     │  Store   │
│              │     │ +Figures │     └─────────────┘     └────┬─────┘
│              │     │ +OCR     │                              │
│  Zotero      │     └──────────┘                              │
│  Web API ◀───┼─────────────────────────────────────┐         │
│ (write ops)  │     ┌──────────┐     ┌──────────┐   │         │
└──────────────┘     │ Reranker │◀────│Retriever │◀──┘─────────┘
                     │ section  │     │ semantic │
                     │ +journal │     │ search   │
                     └────┬─────┘     └──────────┘
                          │
                   ┌──────┴──────┐
                   │  AI Client  │
                   │ Claude Code │
                   │   Cursor    │
                   │  Windsurf   │
                   └─────────────┘
```

<details>
<summary><b>Key Design Decisions</b></summary>

- **Local-first** — your papers never leave your machine
- **Read-only SQLite** — safe even while Zotero is running
- **Write via Web API** — tag/collection changes sync back through Zotero's official API
- **Asymmetric embeddings** — separate encodings for documents vs queries (Gemini)
- **Section-aware** — knows if a passage comes from Methods, Results, or References
- **Journal quality** — Q1 journals ranked higher using SCImago quartile data

</details>

---

## 📦 Embedding Options

| Provider | API Key | Dimensions | Quality | Offline |
|----------|---------|------------|---------|---------|
| **Gemini** `gemini-embedding-001` | Required (free tier) | 768 | 🥇 MTEB #1 | No |
| **Local** `all-MiniLM-L6-v2` | Not needed | 384 | Good | ✅ Yes |

---

## 🗺️ Roadmap

- [x] 24 MCP tools — full Zotero read/write/search
- [x] Semantic search + section-aware reranking
- [x] Table & figure extraction and search
- [x] Citation graph via OpenAlex
- [x] Journal quality weighting (SCImago)
- [x] Chinese query auto-translation
- [x] Cross-platform (macOS, Linux, Windows)
- [ ] OpenAI / Ollama embedding providers
- [ ] PyPI publishing (`pip install zotpilot`)
- [ ] Literature review generation from search results
- [ ] `zotpilot doctor` diagnostic command

---

## 🤝 Contributing

<details>
<summary><b>Development Setup</b></summary>

```bash
git clone https://github.com/xunhe730/ZotPilot.git
cd ZotPilot
uv sync --extra dev

# Run tests
uv run pytest              # 106 tests

# Lint
uv run ruff check src/
```

</details>

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on adding embedding providers, MCP tools, or bug fixes.

---

<div align="center">
  <a href="https://www.star-history.com/#xunhe730/ZotPilot&type=Date">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=xunhe730/ZotPilot&type=Date&theme=dark" />
      <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=xunhe730/ZotPilot&type=Date" />
      <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=xunhe730/ZotPilot&type=Date" width="600" />
    </picture>
  </a>
</div>

<div align="center">
  <br>
  <p>
    <a href="https://github.com/xunhe730/ZotPilot/issues">Report Bug</a> ·
    <a href="https://github.com/xunhe730/ZotPilot/issues">Request Feature</a> ·
    <a href="https://github.com/xunhe730/ZotPilot/discussions">Discussions</a>
  </p>
  <sub>MIT License © 2026 Xiaodong Zhuang</sub>
</div>
