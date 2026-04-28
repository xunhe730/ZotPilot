<div align="center">
  <h2>🧭 ZotPilot</h2>
  <img src="assets/banner.jpg" alt="ZotPilot" width="100%">

  <p>
    <a href="https://www.zotero.org/">
      <img src="https://img.shields.io/badge/Zotero-CC2936?style=for-the-badge&logo=zotero&logoColor=white" alt="Zotero">
    </a>
    <a href="https://claude.ai/code">
      <img src="https://img.shields.io/badge/Claude_Code-6849C3?style=for-the-badge&logo=anthropic&logoColor=white" alt="Claude Code">
    </a>
    <a href="https://github.com/openai/codex">
      <img src="https://img.shields.io/badge/Codex-74AA9C?style=for-the-badge&logo=openai&logoColor=white" alt="Codex">
    </a>
    <a href="https://modelcontextprotocol.io/">
      <img src="https://img.shields.io/badge/MCP-0175C2?style=for-the-badge&logoColor=white" alt="MCP">
    </a>
    <a href="https://pypi.org/project/zotpilot/">
      <img src="https://img.shields.io/pypi/v/zotpilot?style=for-the-badge&logo=pypi&logoColor=white" alt="PyPI">
    </a>
  </p>
  <p>
    <img src="https://img.shields.io/badge/macOS-000000?style=flat-square&logo=apple&logoColor=white" alt="macOS">
    <img src="https://img.shields.io/badge/Linux-FCC624?style=flat-square&logo=linux&logoColor=black" alt="Linux">
    <img src="https://img.shields.io/badge/Windows-0078D6?style=flat-square&logo=windows&logoColor=white" alt="Windows">
  </p>

  <p><b>Give an AI agent your Zotero library. Your papers stay on your machine.</b></p>

  <p>
    <a href="#quick-start">Quick Start</a> &bull;
    <a href="#what-it-does">What it does</a> &bull;
    <a href="#usage-patterns-and-examples">Usage patterns and examples</a> &bull;
    <a href="#how-it-works">How it works</a> &bull;
    <a href="#update">Update</a> &bull;
    <a href="#faq">FAQ</a> &bull;
    <a href="README.md">简体中文</a>
  </p>
</div>

---

## Quick Start

```bash
pip install zotpilot
zotpilot setup                 # interactive config + skill deploy + MCP register
# Restart your AI client
```

Then tell your agent "search my library for X" or "survey recent papers on Y". It will chain 18 MCP tools and 4 packaged skills to complete the job.

**Prerequisites**: [Zotero 8](https://www.zotero.org/download/) installed and launched at least once · Python 3.10+ · a supported AI agent client (Claude Code / Codex / OpenCode). The ingestion workflow also needs the [Connector browser extension](#install-details).

ZotPilot deploys Codex packaged skills to `~/.agents/skills`. The old `$CODEX_HOME/skills` location, usually `~/.codex/skills` when `CODEX_HOME` is unset, is only a Codex compatibility path and is not ZotPilot's deployment target.

---

## What it does

ZotPilot has three parts:

| Component | Role |
|------|------|
| **MCP Server** | 18 atomic tools for semantic search, citation graph, ingestion, and library management |
| **Connector** | Chrome extension; the agent saves papers through your real browser session and keeps institution-access PDFs |
| **Agent Skills** | Chain the tools into complete research workflows instead of isolated calls |

### Four skills covering the research flow

| Skill | What it does |
|-------|--------------|
| `ztp-research` | Local library + OpenAlex search → candidate confirmation → Connector ingest → auto tag & collection → per-paper report |
| `ztp-review` | Review, cluster, compare, and draft from papers already in your library |
| `ztp-profile` | Profile your library structure: topics, venue tiers, time span, tag usage |
| `ztp-setup` | Guides the agent to call `zotpilot setup` / `upgrade` / `doctor` for installation, updates, and troubleshooting. It is not a CLI command |

### Five core capabilities

| Capability | What is special |
|------|------|
| **Semantic search** | Search by meaning, not just keywords; results are localized to section level |
| **One-step ingestion** | Mixed DOI / arXiv / URL input → Connector save → validation → fallback when needed |
| **Citation graph** | OpenAlex-powered citation lookup, including viewpoint search in citing papers |
| **Batch organization** | Semantic match → tag, file, annotate, and sync back to Zotero |
| **Academic discovery** | Full OpenAlex filter surface that feeds directly into ingestion |

---

## How it compares

| | Semantic search | Section-aware | Ingest + organize | Citation graph | Install |
|------|:-:|:-:|:-:|:-:|--------|
| Native Zotero | ✗ | ✗ | ✗ | ✗ | — |
| Feed PDFs to AI | ✓ | ✗ | ✗ | ✗ | Manual |
| Roll-your-own RAG | ✓ | Depends | ✗ | ✗ | Hours |
| [zotero-mcp](https://github.com/54yyyu/zotero-mcp) | ✓ | ✗ | Partial | ✗ | ~5 min |
| **ZotPilot** | ✓ | ✓ | ✓ (Connector) | ✓ | ~5 min |

What is different: ingestion uses a real browser session and Zotero translators, so institutional PDFs come along with the metadata. Citation data comes from OpenAlex. Ranking details live in the architecture section below.

---

## Install details

<details>
<summary><b>Embedding provider selection</b></summary>

| Provider | Experience | Offline | Get API key |
|----------|------|:---:|-------------|
| Gemini | High-quality default | ✗ | [Google AI Studio](https://aistudio.google.com/apikey) |
| DashScope | Better fit for China networks | ✗ | [Alibaba Bailian](https://bailian.console.aliyun.com/) |
| Local | Good enough baseline | ✓ | Not required |

> Avoid switching after the first index. Dimensions differ, so a switch requires `zotpilot index --force`.
> Selecting `local` only switches ZotPilot into local-embedding mode. The local model is downloaded on the first real embedding call, not during `setup`.

Non-interactive (agent-driven):

```bash
zotpilot setup --non-interactive --provider gemini   # or dashscope / local
```

</details>

<details>
<summary><b>API keys and environment</b></summary>

There are two layers:

- `zotpilot setup` writes shared local config to `~/.config/zotpilot/config.json` on macOS / Linux, or `%APPDATA%\zotpilot\config.json` on Windows, and automatically deploys skills / registers MCP
- `zotpilot config set` manages shared config; API keys are stored in the same `config.json`
- `zotpilot upgrade` upgrades the CLI and refreshes packaged skills / MCP runtime
- API keys are not embedded in Claude / Codex / OpenCode client config

Environment variables remain available as temporary overrides and take precedence over `config.json`:

```bash
export GEMINI_API_KEY=<your-key>           # or DASHSCOPE_API_KEY
export ANTHROPIC_API_KEY=<your-key>        # optional: complex-table vision extraction
```

`config.json` may contain API keys. Do not commit it, paste it publicly, or sync it to untrusted locations. On shared machines, prefer interactive `zotpilot setup` so keys are not left in shell history.

Recommended order:

```bash
zotpilot setup                         # interactive: asks for embedding key and optional Zotero User ID / API key
# or
zotpilot setup --non-interactive --provider gemini
```

To change configuration later:

```bash
zotpilot config set gemini_api_key <key>
zotpilot config set zotero_user_id <id>
zotpilot config set zotero_api_key <key>
zotpilot setup
```

Optional: `openalex_email` is not a secret. It is just a contact email for OpenAlex polite-pool access. With it, OpenAlex-backed search / citation tools can usually run at about 10 req/s instead of 1 req/s:

```bash
zotpilot config set openalex_email you@example.com
```

</details>

<details>
<summary><b>Connector browser extension</b></summary>

This is how ingestion actually works. The default instructions only cover Chrome.

1. Open the [latest release](https://github.com/xunhe730/ZotPilot/releases/latest), download `zotpilot-connector-v*.zip`, and extract it
2. In Chrome, open `chrome://extensions/`
3. Enable **Developer mode**
4. Click **Load unpacked**
5. Select the directory that contains `manifest.json`
6. Confirm the Zotero icon appears in the toolbar

> ZotPilot Connector is a fork of the official Zotero Connector. The two can coexist: the official extension handles manual saves, the fork handles agent-driven saves.

Connector upgrades:

1. Download the latest release zip again
2. Open `chrome://extensions/`
3. Click refresh on the unpacked ZotPilot Connector entry

</details>

<details>
<summary><b>Enable write operations (tags / collections / notes)</b></summary>

Search and citation lookup work without credentials. Write operations need a Zotero Web API key:

1. Open [zotero.org/settings/keys](https://www.zotero.org/settings/keys)
2. Note the numeric **User ID**
3. Create a private key with "Allow library access" + "Allow write access"

```bash
zotpilot config set zotero_user_id 12345678
zotpilot config set zotero_api_key YOUR_KEY
zotpilot setup
zotpilot doctor
```

To migrate legacy client-embedded secrets:

```bash
zotpilot config migrate-secrets
```

</details>

<details>
<summary><b>Verify</b></summary>

```bash
zotpilot doctor
zotpilot status
```

MCP tools or skills missing? Run `zotpilot setup` again and restart the agent. Advanced users can run `zotpilot install` to refresh only agent integration.

</details>

---

## Usage patterns and examples

### Direct natural-language interaction

Use this for simple, single-step tasks with a clear target:

- "Search my library for papers about X"
- "Which papers mention Y in the Results section?"
- "Who cites this paper?"
- "How many papers have been indexed?"

These usually map to one MCP tool or a small set of tools.

Typical examples:

| You say | Agent does |
|---------|------------|
| "Search my papers on X" | Semantic search over your indexed library |
| "Which papers mention Y in Results?" | Section + keyword passage lookup |
| "Find tables comparing model accuracy" | Search extracted PDF tables |
| "Who cited this paper, and what did they say?" | OpenAlex lookup → search citing passages |
| "How many papers are indexed?" | Index status check |

### Explicit `ztp-*` workflows

Use this for multi-step tasks that are easy to derail and need the agent to follow a full workflow:

- `ztp-research`
  - "/ztp-research survey the most important recent papers on X and ingest the worthwhile ones into Zotero"
- `ztp-review`
  - "/ztp-review draft a review outline about X from papers already in my library"
- `ztp-profile`
  - "/ztp-profile show what this library is mostly about before reorganizing it"
- `ztp-setup`
  - "/ztp-setup check my ZotPilot configuration"

These tasks are better expressed as explicit skill workflows because they usually span discovery, filtering, ingestion, organization, and reporting.

Typical examples:

| You say | Agent does |
|---------|------------|
| "/ztp-research find recent papers on Z" | OpenAlex search → candidate confirmation → Connector ingest → organize |
| "/ztp-review draft a review outline about X from papers already in my library" | Cluster, compare, extract findings, and draft a review outline |
| "/ztp-profile show what this library is mostly about before reorganizing it" | Analyze themes, venue tiers, time span, and tag structure |

---

## How it works

```text
Indexing (one-off)
Zotero SQLite ──→ PDF extraction ──→ chunking + section classification ──→ embeddings ──→ ChromaDB

Queries (every call)
Agent ──→ MCP tools ───┬── semantic search ──→ ChromaDB ──→ section-aware reranking
                       ├── citation graph  ──→ OpenAlex
                       ├── library browse  ──→ Zotero SQLite (read-only)
                       ├── write ops       ──→ Zotero Web API ──→ sync back to Zotero
                       └── ingestion       ──→ Bridge + Connector ──→ Zotero Desktop
```

- **Indexing**: SQLite is opened read-only with `mode=ro&immutable=1`; PyMuPDF extracts text, tables, and figures; chunks are labeled by academic section; embeddings are stored in ChromaDB. Incremental indexing skips previously indexed items.
- **Retrieval**: query vector → ChromaDB cosine similarity → section-aware reranking + journal-quality weighting.
- **Ingestion**: agent → local bridge (127.0.0.1:2619) → Chrome Connector → Zotero Desktop.
- **Write ops**: tags / collections / notes go through Zotero's official Web API and sync back to the desktop client.

<details>
<summary><b>MCP tool list (18)</b></summary>

| Category | Tools |
|------|------|
| Search | `search_papers`, `search_topic`, `search_boolean`, `advanced_search` |
| Read | `get_passage_context`, `get_paper_details`, `get_notes`, `get_annotations`, `browse_library`, `profile_library` |
| Discover | `search_academic_databases` |
| Ingest | `ingest_by_identifiers` |
| Organize | `manage_tags`, `manage_collections`, `create_note` |
| Citations | `get_citations` |
| Index | `index_library`, `get_index_stats` |

`search_papers` supports `section_type` for tables / figures. `ingest_by_identifiers` accepts mixed DOI / arXiv ID / URL input.

</details>

<details>
<summary><b>File layout & data locations</b></summary>

```text
Installed zotpilot (wheel ships skills + references)
├── src/zotpilot/skills/
├── references/
└── connector/

# Config / index
# macOS / Linux
~/.config/zotpilot/config.json
~/.local/share/zotpilot/chroma/

# Windows
%APPDATA%\zotpilot\config.json
%APPDATA%\zotpilot\chroma\
```

</details>

---

## Update

```bash
zotpilot upgrade
```

Upgrades the active ZotPilot CLI, refreshes skill files, and reconciles MCP runtime state.

<details>
<summary><b>Common update commands</b></summary>

| Command / flag | Purpose |
|------|---------|
| `upgrade` or `update` (no flags) | Upgrade the CLI, refresh skills, and reconcile runtime state |
| `--check` | Check only (always exit 0) |
| `--dry-run` | Preview runtime drift and update actions |
| `--cli-only` | Upgrade only the CLI package |
| `--skill-only` | Refresh only skills and runtime registration |
| `--re-register` | Force client registration refresh even if no drift is detected |
| `--migrate-secrets` | Migrate legacy client-embedded secrets before reconciling runtime |

> In editable/dev installs, `upgrade` reminds you to `git pull` for source updates but still reconciles runtime state.

</details>

---

## FAQ

<details>
<summary><b>Does this modify my Zotero database directly?</b></summary>

No. SQLite is opened with `mode=ro&immutable=1`, so writes are physically impossible. Tags / collections / notes go through Zotero's official Web API and sync back normally.

</details>

<details>
<summary><b>Can Zotero stay open while ZotPilot is running?</b></summary>

Yes. Read-only access does not conflict with the running Zotero client.

</details>

<details>
<summary><b>Which agents are supported?</b></summary>

Claude Code, Codex, and OpenCode. These are the three officially supported clients. Skill deployment, MCP registration, and upgrade reconciliation are designed for them.

</details>

<details>
<summary><b>How much does indexing cost?</b></summary>

Gemini's free tier covers a few hundred papers for many users. DashScope also has a low-cost / free-tier path. Local mode is fully offline and free.

</details>

<details>
<summary><b>How long does indexing take?</b></summary>

About 2–5 seconds per paper, or roughly 15 minutes for 300 papers. Try `zotpilot index --limit 10` first.

</details>

<details>
<summary><b>Scanned PDFs / long documents?</b></summary>

- Scanned PDFs fall back to OCR if Tesseract is installed
- Documents over 40 pages are skipped by default (`--max-pages` to override, `--item-key` to target)
- Optional Claude Haiku repair can help with complex tables

</details>

<details>
<summary><b>Can I run it fully offline?</b></summary>

Yes. Pick `--provider local`, skip write-operation keys, and search / browse / index all stay local.

</details>

<details>
<summary><b>Where does citation data come from?</b></summary>

[OpenAlex](https://openalex.org/). Papers without DOIs cannot get citation data, but semantic search and library management still work.

</details>

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Skills not showing up | `zotpilot setup`, then restart the agent |
| `zotpilot: command not found` | Install first: `pip install zotpilot` |
| MCP tools missing | `zotpilot setup`, then restart the agent |
| Search returns empty | Run `zotpilot index` first |
| `GEMINI_API_KEY not set` | `export GEMINI_API_KEY=<key>` or switch to `setup --provider local` |
| Unsure what failed | `zotpilot doctor` |

Deeper guidance lives in [troubleshooting.md](references/troubleshooting.md).

---

<details>
<summary><b>Development / contributing</b></summary>

```bash
git clone https://github.com/xunhe730/ZotPilot.git
cd ZotPilot
pip install -e ".[dev]"

zotpilot setup
python -m pytest
python -m ruff check src/ tests/
```

Connector development:

```bash
cd connector
npm install
./build.sh -d
```

</details>

---

<div align="center">
  <code>pip install zotpilot &amp;&amp; zotpilot setup</code>
  <br><br>
  <sub>Claude Code &middot; Codex &middot; OpenCode</sub>
  <br><br>
  <a href="https://github.com/xunhe730/ZotPilot/issues">Report an issue</a> &middot;
  <a href="https://github.com/xunhe730/ZotPilot/discussions">Discussions</a>
  <br>
  <sub>MIT License &copy; 2026 xunhe</sub>
</div>
