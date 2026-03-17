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
    <a href="README_CN.md">简体中文</a>
  </p>
</div>

---

## The Problem

You have hundreds of papers in Zotero. You _know_ you read something about "the relationship between sleep spindles and memory consolidation" — but Zotero only matches exact keywords.

When you ask AI to help, here's what happens:
- **No access**: AI can't read your Zotero library at all
- **Manual keyword guessing**: Zotero's search needs exact terms — "sleep spindles" won't find "spindle oscillations"
- **Open every PDF**: Finding a specific table or claim means clicking through papers one by one
- **No citation awareness**: Can't ask "who cites this paper?" or "what's the impact?"
- **Tags are painful**: Organizing hundreds of papers by theme is hours of drag-and-drop

## The Solution

ZotPilot is an **AI Agent Skill** that gives your AI assistant full read/write access to your Zotero library — semantic search, citation graph, table extraction, tag management, and more. All through natural language.

```
You: "Find papers about sleep spindles and memory consolidation"
 → Skill triggers → MCP server searches your library by meaning
 → Returns ranked results with passages, page numbers, and citation keys
```

**No copy-paste. No keyword guessing. No opening PDFs.** AI reads your library directly and knows _how to research_ — which tools to use, what parameters matter, how to chain multi-step workflows.

---

## Why ZotPilot, Not Other Approaches?

| Approach | Finds by meaning? | Knows your library? | Organizes for you? | Setup |
|----------|:-:|:-:|:-:|-------|
| **Zotero built-in search** | No | Yes | No | None |
| **Feed PDFs to Claude** | Yes | Partial (token limits) | No | Manual |
| **Generic MCP search tools** | Some | No structure awareness | No | Medium |
| **Local RAG pipeline** | Yes | Yes | No | Hours |
| **ZotPilot** | **Yes** | **Yes — full Zotero access** | **Yes — tags & collections** | **5 min** |

### What makes ZotPilot different?

1. **Semantic search, not keywords** — "memory consolidation during sleep" finds papers about "sleep spindle-dependent replay" even if those exact words don't appear together
2. **Section-aware ranking** — knows if a passage comes from Methods vs Results vs Abstract, weights accordingly
3. **Journal quality weighting** — Q1 journal papers ranked higher using SCImago quartile data
4. **Full read/write access** — not just search: browse collections, add tags, move papers, create folders
5. **Citation graph** — "who cites this?" and "what does this cite?" via OpenAlex
6. **Table and figure search** — find specific data tables and figures across your entire library
7. **Built-in Skill** — doesn't just give AI tools, teaches AI _how to do research_

---

## Quick Start

### Option 1: Auto Install (recommended)

Copy this to your AI agent:

> Install the ZotPilot skill for me: clone https://github.com/xunhe730/ZotPilot.git into my skills directory, set it up, and help me search my Zotero library.

The agent handles everything — cloning, CLI installation, MCP registration, indexing, and tool selection.

### Option 2: Manual Install

```bash
# Claude Code
git clone https://github.com/xunhe730/ZotPilot.git ~/.claude/skills/zotpilot

# OpenCode
git clone https://github.com/xunhe730/ZotPilot.git ~/.config/opencode/skills/zotpilot

# OpenClaw
git clone https://github.com/xunhe730/ZotPilot.git ~/.openclaw/skills/zotpilot
```

Restart your AI agent. Say "search my Zotero for..." — the Skill handles MCP setup, indexing, and tool selection automatically. Zero configuration.

When you first use the skill, it automatically:
- Installs the ZotPilot CLI via `uv tool install`
- Detects your Zotero data directory
- Registers the MCP server with your AI agent
- Indexes your papers (you choose: Gemini embeddings or fully offline local model)

> **Embedding choice:** Gemini (recommended, free tier) or Local (offline, no API key). The Skill asks you during setup.

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
