<div align="center">
  <h1>ZotPilot</h1>
  <h3>Let AI Take Over Your Zotero</h3>
  <p>
    Search by meaning, explore citations, organize with natural language.<br>
    <b>An AI Agent Skill for Zotero. Full library access. No plugin required.</b>
  </p>

  <p>
    <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python">
    <img src="https://img.shields.io/badge/MCP-24_Tools-00B265?style=flat-square" alt="MCP">
    <img src="https://img.shields.io/badge/License-MIT-blue?style=flat-square" alt="License">
  </p>
  <p>
    <img src="https://img.shields.io/badge/macOS-supported-000000?style=flat-square&logo=apple&logoColor=white" alt="macOS">
    <img src="https://img.shields.io/badge/Linux-supported-FCC624?style=flat-square&logo=linux&logoColor=black" alt="Linux">
    <img src="https://img.shields.io/badge/Windows-supported-0078D6?style=flat-square&logo=windows&logoColor=white" alt="Windows">
  </p>

  <p>
    <a href="#-quick-start">Quick Start</a> &bull;
    <a href="#-real-world-examples">Examples</a> &bull;
    <a href="#-how-it-works">Architecture</a> &bull;
    <a href="#-common-commands">Commands</a> &bull;
    <a href="README.md">简体中文</a>
  </p>
</div>

---

## The Problem

You have hundreds of papers in Zotero. While writing a Related Work section, you _remember_ reading about "the relationship between sleep spindles and memory consolidation" — but can't find it in Zotero. Because you remember the _concept_, but Zotero only matches _exact words_.

This is the fundamental limitation of all reference managers:
- **Zotero search is keyword matching** — "memory consolidation during sleep" won't find a paper that says "sleep spindle-dependent replay", even though they describe the same phenomenon
- **No cross-paper queries** — "which papers report N400 effects in their Results section?" requires opening each PDF manually
- **Table data is locked in PDFs** — you know a paper has an accuracy comparison table, but can't search table contents
- **Citation relationships are blind** — "who cites this paper? what do they say about it?" requires manual Google Scholar lookup
- **Organizing is manual labor** — tagging and sorting 200 papers by theme is pure drag-and-drop busywork

## The Solution

ZotPilot builds a **local RAG system** (Retrieval-Augmented Generation) on top of your Zotero library, exposed to AI agents via MCP protocol — letting AI search by meaning, read, and organize your papers directly.

**How it works:**

```
Zotero SQLite ──→ PDF extraction (PyMuPDF) ──→ Chunking + section classification ──→ Embeddings (Gemini/local) ──→ ChromaDB
     │                                              │
     │            ┌──────────────────────────────────┘
     ▼            ▼
  Metadata    Semantic retrieval + reranking
  (title, authors,   (similarity^0.7 × section_weight × journal_quality)
   DOI, tags)              │
     │                     ▼
     └──────→ 24 MCP tools ←── AI Agent (Claude Code / OpenCode / OpenClaw)
                   │
            Zotero Web API (write ops: tags, collections)
```

- **Indexing**: reads metadata from Zotero SQLite (read-only), extracts full text, tables, and figures from PDFs via PyMuPDF, classifies chunks by academic section (Abstract/Methods/Results/…), generates vector embeddings, stores in ChromaDB
- **Retrieval**: query is vectorized, cosine similarity search in ChromaDB, results pass through **section-aware reranking** (Results weighted higher than References) and **journal quality weighting** (SCImago Q1 papers ranked higher)
- **Write operations**: tag and collection management via Zotero's official Web API (Pyzotero), changes sync back to Zotero automatically
- **Citation graph**: forward and backward citation lookup via OpenAlex API

**Key design decisions:**
- Fully local — papers never leave your machine (except Gemini embedding API calls)
- Zotero SQLite read-only — safe even while Zotero is running
- Asymmetric embeddings — documents encoded with `RETRIEVAL_DOCUMENT`, queries with `RETRIEVAL_QUERY`, improving retrieval quality
- Built-in Skill — doesn't just give AI tools, teaches AI _which tool to pick and how to chain them_

---

## Why ZotPilot, Not Other Approaches?

| Approach | Semantic search | Knows paper structure | Organizes for you | Citation graph | Setup |
|----------|:-:|:-:|:-:|:-:|-------|
| **Zotero built-in search** | No | No | No | No | None |
| **Feed PDFs to Claude** | Yes | No (loses section info) | No | No | Manual, token-limited |
| **Build your own RAG** | Yes | Depends | No | No | Hours of setup |
| **ZotPilot** | **Yes** | **Yes (section+journal+tables)** | **Yes** | **Yes (OpenAlex)** | **5 min** |

ZotPilot's core advantage over DIY RAG: **it doesn't just "find relevant passages" — it knows whether a passage comes from Results or Methods, from a Q1 journal or a workshop paper, and ranks accordingly.** Combined with table/figure search, citation graph, and Zotero write operations, it forms a complete research workflow.

---

## Quick Start

### Option 1: Auto Install (recommended)

Copy this to your AI agent:

> Install the ZotPilot skill for me: clone https://github.com/xunhe730/ZotPilot.git into my skills directory, then help me set up my Zotero library.

The agent clones the repo, installs the CLI, configures Zotero, and registers the MCP server. You restart once, then you're ready to search.

### Option 2: Manual Install

```bash
# Claude Code
git clone https://github.com/xunhe730/ZotPilot.git ~/.claude/skills/zotpilot

# OpenCode
git clone https://github.com/xunhe730/ZotPilot.git ~/.config/opencode/skills/zotpilot

# OpenClaw
git clone https://github.com/xunhe730/ZotPilot.git ~/.openclaw/skills/zotpilot
```

Restart your AI agent.

### What happens on first use

When you say "search my Zotero for..." the first time, the Skill walks you through setup:

1. **Auto-installs CLI** — `scripts/run.py` detects missing `zotpilot` command and installs it via `uv tool install`
2. **Configures Zotero** — auto-detects your Zotero data directory, asks you to choose embedding provider (Gemini or offline local)
3. **Registers MCP server** — runs `claude mcp add` (or equivalent for OpenCode/OpenClaw)
4. **You restart once** — MCP tools become available after restart
5. **Indexes your papers** — on second launch, indexes your library (~2-5s per paper)
6. **Ready to search** — from here on, just ask naturally

> **Embedding choice:** Gemini (recommended, free tier at https://aistudio.google.com/apikey) or Local (offline, no API key). The Skill asks during setup.

---

## Real-World Examples

### Example 1: Literature Survey

**You:** "What do I have on transformer architectures for EEG classification?"

**AI's internal process (guided by Skill):**
```
→ Checks index readiness (get_index_stats)
→ Picks search_topic (not search_papers — this is a survey task)
→ Returns 12 papers, sorted by relevance
→ Reports: year range 2019–2024, key authors, best passages
```

**Result:** Structured overview with paper titles, authors, and key findings — no PDF opened.

### Example 2: Finding Specific Evidence

**You:** "Find evidence that N400 amplitude correlates with prediction error"

**AI's internal process:**
```
→ Picks search_papers (specific claim, not survey)
→ Uses required_terms=["N400"] to force exact match
→ Sets section_weights={"results": 1.0, "discussion": 0.8}
→ Returns passages with page numbers and citation keys
```

**Result:** Direct quotes from 3 papers with `[Author2022, p.12]` citations.

### Example 3: Organize by Theme

**You:** "Tag all deep learning papers and move them to a 'DL Methods' collection"

**AI's internal process:**
```
→ search_topic("deep learning") → finds 28 matching papers
→ create_collection("DL Methods") → creates Zotero folder
→ For each paper: add_to_collection + add_item_tags(["deep-learning"])
→ Confirms with user before modifying more than 5 papers
```

**Result:** 28 papers tagged and organized. Changes sync to Zotero via Web API.

> **Note:** Write operations (tags, collections) require Zotero Web API credentials. See [Enable Write Operations](#enable-write-operations) below.

### Example 4: Citation Exploration

**You:** "Who cites Wang 2022 and what do they say about the limitations?"

**AI's internal process:**
```
→ search_boolean("Wang 2022") → finds the paper, gets doc_id
→ find_citing_papers(doc_id) → 15 citing papers via OpenAlex
→ search_papers("limitations of Wang 2022 approach") in those papers
→ Returns specific critique passages
```

---

## Common Commands

| What you say | What happens |
|---|---|
| *"Search my papers for X"* | Semantic search across all indexed papers |
| *"What do I have on X?"* | Topic survey — returns papers grouped by relevance |
| *"Find the paper by Author about Y"* | Boolean search + paper details |
| *"Show me tables comparing X"* | Searches extracted table content |
| *"Who cites this paper?"* | Citation lookup via OpenAlex |
| *"Tag these papers as X"* | Adds tags via Zotero Web API |
| *"Create a collection called X"* | Creates Zotero folder |
| *"How many papers are indexed?"* | Index health check |

---

## 24 MCP Tools

### Search & Discover

| Tool | Description |
|------|-------------|
| `search_papers` | Semantic search with section/journal weighting and filters |
| `search_topic` | Topic-level paper discovery, deduplicated by document |
| `search_boolean` | Exact word matching (AND/OR) via Zotero's full-text index |
| `search_tables` | Search table headers, cells, and captions |
| `search_figures` | Search figure captions and descriptions |
| `get_passage_context` | Expand any result with surrounding paragraphs |

### Browse & Understand

| Tool | Description |
|------|-------------|
| `get_library_overview` | Paginated list of all papers with index status |
| `get_paper_details` | Full metadata: title, authors, abstract, DOI, tags |
| `list_collections` | All Zotero folders with hierarchy |
| `get_collection_papers` | Papers in a specific collection |
| `list_tags` | All tags sorted by frequency |
| `get_index_stats` | Index health: documents, chunks, unindexed papers |

### Organize & Write

| Tool | Description |
|------|-------------|
| `add_item_tags` / `remove_item_tags` | Add or remove tags (non-destructive) |
| `set_item_tags` | Replace all tags on a paper |
| `add_to_collection` / `remove_from_collection` | Move papers between folders |
| `create_collection` | Create new folders (supports nesting) |

### Citations & Impact

| Tool | Description |
|------|-------------|
| `find_citing_papers` | Who cites this paper? (OpenAlex) |
| `find_references` | What does this paper cite? |
| `get_citation_count` | Citation and reference counts |

### Admin

| Tool | Description |
|------|-------------|
| `index_library` | Index new/changed papers (incremental) |
| `get_reranking_config` | View ranking weights |
| `get_vision_costs` | Monitor vision API usage |

---

## How It Works

This is an **AI Agent Skill** — a repository containing instructions ([SKILL.md](SKILL.md)) and a bootstrap script ([scripts/run.py](scripts/run.py)) that your AI agent loads automatically. The Skill triggers an MCP server with 24 tools for full Zotero access.

### Architecture

```
~/.claude/skills/zotpilot/          (or OpenCode/OpenClaw equivalent)
├── SKILL.md                        # Decision tree: setup → index → research
├── scripts/run.py                  # Bootstrap: auto-installs CLI + delegates
├── references/                     # Deep reference docs
│   ├── tool-guide.md               # Detailed parameter guide
│   ├── troubleshooting.md          # Common issues + fixes
│   └── install-steps.md            # Manual install reference
└── src/zotpilot/                   # MCP server source (24 tools)
```

When you mention Zotero or papers, the AI:
1. Loads `SKILL.md` → runs `scripts/run.py status --json`
2. If not installed → auto-installs CLI, configures Zotero, registers MCP
3. If not indexed → indexes your papers (Gemini or local embeddings)
4. If ready → picks the right tool, sets optimal parameters, formats results

### Key Design Decisions

- **Local-first** — your papers never leave your machine. Zotero SQLite is read-only
- **Write via Web API** — tag/collection changes sync through Zotero's official API
- **Section-aware ranking** — composite score = similarity^0.7 x section_weight x journal_quality
- **Asymmetric embeddings** — separate encodings for documents vs queries (Gemini)
- **Skill, not just tools** — SKILL.md teaches AI _which_ tool to pick and _how_ to chain them

### Embedding Options

| Provider | API Key | Quality | Offline |
|----------|---------|---------|---------|
| **Gemini** `gemini-embedding-001` | Required (free tier) | MTEB #1 | No |
| **Local** `all-MiniLM-L6-v2` | Not needed | Good | Yes |

### Data Storage

```
~/.config/zotpilot/config.json      # Configuration (Zotero path, provider)
~/.local/share/zotpilot/chroma/     # ChromaDB vector index
```

Your Zotero data is read directly from its SQLite database. The index is local. No data leaves your machine (except embedding API calls if using Gemini).

---

## Enable Write Operations

Search and citation tools work out of the box. To **organize your library** (add tags, move papers, create collections), you need a Zotero Web API key.

### Get credentials

1. Go to [zotero.org/settings/keys](https://www.zotero.org/settings/keys)
2. Click **"Create new private key"**
3. Check **"Allow library access"** and **"Allow write access"**
4. Save — copy the key
5. Note your **User ID** (the number shown on the same page — not your username)

### Option 1: Let your Agent configure it (recommended)

Once you have the key and User ID, just tell your AI agent:

> Enable ZotPilot write operations. My Zotero API Key is `xxxxx` and my User ID is `12345`.

The agent will run `claude mcp remove` + `claude mcp add` and prompt you to restart.

### Option 2: Manual configuration

```bash
claude mcp remove zotpilot
claude mcp add -s user \
  -e GEMINI_API_KEY=<your-gemini-key> \
  -e ZOTERO_API_KEY=<your-zotero-key> \
  -e ZOTERO_USER_ID=<your-user-id> \
  zotpilot -- zotpilot
```

Restart your AI agent.

> Without these credentials, all read/search operations still work. You only need this for tag and collection management.

---

## FAQ

**Does this modify my Zotero database?**
No. ZotPilot reads the SQLite database in read-only mode. Write operations (tags, collections) go through Zotero's official Web API and sync back normally.

**What if I add new papers to Zotero?**
Run `zotpilot index` again — it's incremental, only processes new/changed papers.

**Can I use this without an API key?**
Yes. Choose `--provider local` during setup to use the offline embedding model (all-MiniLM-L6-v2). No API key needed, everything runs locally.

**How long does indexing take?**
About 2-5 seconds per paper. For 300 papers, expect ~10-15 minutes. Use `--limit 10` to test first.

**What AI agents are supported?**
Claude Code, OpenCode, and OpenClaw. Any agent that supports the Skill + MCP protocol pattern.

**Is it safe to run while Zotero is open?**
Yes. ZotPilot opens the SQLite database in read-only mode and never writes to it.

---

## Troubleshooting

See [references/troubleshooting.md](references/troubleshooting.md) for detailed solutions. Quick fixes:

| Problem | Fix |
|---------|-----|
| Skill not found after install | Check path: `ls ~/.claude/skills/zotpilot/SKILL.md` |
| `zotpilot: command not found` | Run `python3 scripts/run.py status` (auto-installs) |
| MCP tools not available | `claude mcp add -s user zotpilot -- zotpilot` then restart |
| Empty search results | Run `zotpilot index` first, or try broader query |
| `GEMINI_API_KEY not set` | Set env var, or switch to local: `zotpilot setup --non-interactive --provider local` |

---

## Contributing

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

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

---

## The Bottom Line

**Without ZotPilot:** Keyword guessing in Zotero → open each PDF → copy-paste to AI → repeat

**With ZotPilot:** Tell your AI what you need → it searches by meaning, finds evidence, explores citations, organizes papers — all in one conversation.

```bash
# Get started in 30 seconds
git clone https://github.com/xunhe730/ZotPilot.git ~/.claude/skills/zotpilot
# Restart Claude Code, then: "search my Zotero for..."
```

---

<div align="center">
  <p>
    <a href="https://github.com/xunhe730/ZotPilot/issues">Report Bug</a> &middot;
    <a href="https://github.com/xunhe730/ZotPilot/issues">Request Feature</a> &middot;
    <a href="https://github.com/xunhe730/ZotPilot/discussions">Discussions</a>
  </p>
  <sub>MIT License &copy; 2026 Xiaodong Zhuang</sub>
</div>
