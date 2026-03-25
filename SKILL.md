---
name: zotpilot
description: >-
  Use when user mentions Zotero, academic papers, citations,
  literature reviews, research libraries, or wants to search/organize their
  paper collection. Also triggers on "find papers about...", "what's in my
  library", "organize my papers", "who cites...", "tag these papers".
  Always use this skill for Zotero-related tasks.
license: MIT
compatibility:
  - Python 3.10+
  - Zotero desktop (installed and run at least once)
---

# ZotPilot

> All script paths are relative to this skill's directory.

## Step 1: Check readiness

**Python command:** Use `python3` on Linux/macOS. On Windows, use `python`.

Run: `python3 scripts/run.py status --json`  (Windows: `python scripts/run.py status --json`)

Parse the JSON output and follow the FIRST matching branch:

1. Command fails entirely → **Prerequisites** (see `references/setup-guide.md`)
2. `config_exists` is false → **First-Time Setup** (see `references/setup-guide.md`)
3. `errors` is non-empty → **First-Time Setup** (note: `warnings` like API key not in env are OK if key was passed to `register`)
4. `index_ready` is false or `doc_count` is 0 → go to **Index** below
5. All green → go to **Research**

**Inline fallback** (if agent cannot access references/):
```bash
python3 scripts/run.py setup --non-interactive --provider gemini
python3 scripts/run.py register
# Restart your AI agent, then ask again.
```

If any errors: run `python3 scripts/run.py doctor` for diagnostics.

## Index (if doc_count = 0)

MCP tools are now available. Index the user's papers:

```bash
python3 scripts/run.py index
```

Indexing takes ~2-5 seconds per paper. Documents over 40 pages are skipped by default.
After indexing, check for "Skipped N long documents" — offer to index them with `--max-pages 0`.

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
| Tag/organize one paper | `add_item_tags`, `add_to_collection` | `item_key` |
| Batch tag/organize many papers | `batch_tags`, `batch_collections` | `items` or `item_keys`, `action` |
| Search external databases for new papers | `search_academic_databases` | `query`, `limit=20` |
| Add a paper by DOI/arXiv/URL | `add_paper_by_identifier` | `identifier` |
| Batch add papers from search results | `ingest_papers` | `papers` (from search_academic_databases) |
| Capture a URL to Zotero via browser | `save_from_url` | `url`, `collection_key`, `tags` — **requires ZotPilot Connector** |
| Batch capture URLs to Zotero via browser | `save_urls` | `urls` (list, max 10), `collection_key`, `tags` — **requires ZotPilot Connector** |
| Profile the library / generate user research context | `profile_library` | — |

> **`save_from_url` prerequisites:** ZotPilot Connector must be installed in Chrome and Chrome must be open. The bridge (`:2619`) auto-starts. Returns `item_key` when found in Zotero within 3s discovery window. Requires `ZOTERO_API_KEY` for `item_key` discovery and routing — without it, save still works but `item_key` is not returned.

### Workflow chains

**Literature review:**
search_topic → get_paper_details (top 5) → find_references → search_papers with section_weights

**"What do I have on X?":**
search_topic(num_papers=20) → report count, year range, key authors, top passages

**Organize by theme (batch):**
search_topic → create_collection → batch_collections(action="add", item_keys=[...]) → batch_tags(action="add", items=[...])

**Find specific paper:**
search_boolean first (exact terms) → fallback to search_papers (semantic) → get_paper_details

**Find and add new papers:**
search_academic_databases → review candidates with user → ingest_papers → index_library

**Agent research discovery** ("帮我调研 X"):

Agent judges routing from context — no mechanical Q&A. Capability reference:

| Situation | Save path | PDF? | Speed |
|---|---|---|---|
| User says "要读" / "要索引" / "需要全文" | `save_from_url("doi.org/{doi}")` or `save_urls([...])` | ✅ institutional via browser | ~30s/URL |
| arXiv paper (any intent) | `add_paper_by_identifier("arxiv:ID")` | ✅ OA free | fast |
| Only need metadata / references | `ingest_papers([{doi:...}])` | ❌ OA only | fast, batch 50 |
| URL-only, no DOI/arXiv | `save_from_url(url)` or `save_urls(urls)` | ✅ institutional | ~30s/URL |

Discovery channels (agent picks based on topic):
- **OpenAlex** (primary): `search_academic_databases(query, year_min=...)` — structured results with abstracts, citation counts, DOIs, OA status, landing URLs; S2 supplemented automatically when `S2_API_KEY` is set
- **PubMed**: `mcp__claude_ai_PubMed__search_articles` — biomedical focus
- **arXiv / DOI direct**: `add_paper_by_identifier` — when identifier is known
- **Real browser search**: Playwright MCP `browser_navigate` + `browser_snapshot` — Google Scholar, any publisher page, no API limits

Step 1 — Discover: `search_academic_databases(X, limit=20)` (add PubMed for biomedical)
Step 2 — Score & rank: For each candidate paper, compute a weighted score (0-10) across 4 dimensions:

| Dimension | Default weight | Description |
|-----------|---------------|-------------|
| Query relevance | 40% | How directly the paper answers the user's query (based on abstract) |
| User context fit | 30% | Alignment with ZOTPILOT.md research focus and discipline (0% when no ZOTPILOT.md) |
| Quality signal | 20% | Citation count (normalized by year), source type (journal > conference > preprint) |
| Recency | 10% | Publication year proximity to current year |

**When no ZOTPILOT.md**: redistribute context fit weight → relevance becomes 70%, context 0%.

**Intent-driven weight adjustments** (agent judges from natural language, not keyword matching):

| Intent signal | Relevance | Context | Quality | Recency |
|---------------|-----------|---------|---------|---------|
| "最新" / "recent advances" | 35% | 25% | 10% | 30% |
| "经典" / "foundational work" | 35% | 25% | 30% | 10% |
| "探索新方向" / "exploring new area" | 60% | 0% | 25% | 15% |
| "高引" / "high impact" | 35% | 25% | 35% | 5% |
| "综述" / "survey" | 50% | 30% | 15% | 5% + add `type:review` filter |

Display format (show to user before ingesting):
```
1. [9.2] Attention Is All You Need (Vaswani et al., 2017) · 8420引用 · OA
   "直接奠定你研究的 transformer 基础，与你的 VLM 方向高度契合"

2. [7.8] CLIP (Radford et al., 2021) · 3200引用 · OA
   "视觉语言对齐的代表工作，补充你库中 contrastive learning 的空白"
```

Step 3 — Ingest (after user confirms list):

**De-duplicate first.** Batch-check all candidate DOIs in a single call before any saves:
- Use `advanced_search(conditions=[{field:"doi", op:"is", value:"doi1"}, {field:"doi", op:"is", value:"doi2"}, ...], match:"any")` to check all DOIs at once
- Papers already in library: skip ingest, inform user ("已在库中: Title"), proceed to Step 4 with the existing `item_key`
- Papers not found: proceed with routing below

Route each **new** paper by priority:

| Priority | Condition | Tool | Result |
|----------|-----------|------|--------|
| 1 | `arxiv_id` present | `add_paper_by_identifier("arxiv:ID")` | OA PDF, fast |
| 2 | `doi` + `is_oa=True` + `oa_url` | `add_paper_by_identifier(doi)` | OA PDF direct |
| 3 | `doi` + `is_oa=False` + `landing_page_url` | `save_urls([landing_page_url])` | Connector, ~30s/URL, requires Chrome+Connector |
| 4 | `doi` only (no URL) | `ingest_papers([{doi:...}])` | Metadata only, no PDF |
| 5 | No `doi` | Skip, inform user | (OpenAlex ID support: backlog) |

Step 4 — Post-ingest (per item_key): index → analyze → classify

For each successfully saved item with an `item_key`:
1. `index_library(item_key=...)` — incremental index, only this paper
2. `get_paper_details(item_key)` — read abstract, methods, key findings
3. Based on the content and ZOTPILOT.md context, make judgments:
   - Which collection(s) does this paper belong to? (check existing collections via `list_collections`)
   - What tags best describe it? (use existing tag vocabulary from `list_tags` where possible)
   - Is there anything worth noting — a key method, finding, or connection to the user's work?
4. `add_to_collection(item_key, collection_key)` + `add_item_tags(item_key, tags)` — classify
5. `create_note(item_key, content)` — only if the paper is highly relevant; write a concise structured note: what it does, key method, main finding, relevance to user's research

Do NOT create notes for every paper — reserve them for papers the user is likely to read closely. Use ZOTPILOT.md to judge relevance.

If `item_key` missing from result: `advanced_search(title=...)` to locate the item first.

Alert user when using `save_from_url` / `save_urls`: ~30s per URL, Chrome + Connector must be running.

**Agent research ingest** (single paper, deep read):
Prerequisites: Chrome open, ZotPilot Connector installed, `ZOTERO_API_KEY` configured
1. `save_from_url(url)` → get `item_key` from result
2. `index_library(item_key=...)` → incremental index
3. `get_paper_details(item_key)` → read abstract, methods, conclusions
4. Judge: relevant collection, appropriate tags, worth a structured note?
5. `add_to_collection` + `add_item_tags` → classify
6. `create_note(item_key, content)` → structured reading note if highly relevant

If `item_key` missing: use `advanced_search(title=result["title"])` to locate first.

**Library profiling & ZOTPILOT.md** (first-time setup or refresh):
Prerequisites: Library indexed (`index_library` run at least once)

This is an **intelligent, adaptive workflow** — not a fixed questionnaire. The agent should bring genuine understanding of the library to the conversation, form hypotheses, and conduct a natural dialogue rather than mechanically asking preset questions.

**Phase 1 — Deep library understanding (before talking to the user):**
1. `profile_library()` → get metadata stats + existing_profile
2. Use `search_topic()` across multiple angles to understand what the library actually contains: dominant research themes, methodology patterns, key application domains, temporal trends. Form your own interpretation of what this researcher works on and why.
3. Notice anomalies: off-topic papers, gaps in coverage, unusual clusters — these become conversation material.

**Phase 2 — Dialogue (bring your understanding, not a form):**
- Open by sharing your interpretation of the library, including specific observations: "从你的文献结构来看，你似乎在做X方向，重点在Y，Z这块覆盖较少——这个判断准确吗？"
- Let the conversation flow naturally. Use what you know about the library to ask targeted follow-up questions rather than a fixed list. Examples of things worth exploring:
  - What's the core problem they're trying to solve?
  - Which papers/directions are most central to their current work?
  - Are the anomalous/off-topic entries intentional or accidental?
  - What's missing that they wish they had more of?
- Minimum information needed for a useful profile: discipline, role, primary research focus, cross-disciplinary interests — but gather these through conversation, not a checklist.

**Phase 3 — Write `~/.config/zotpilot/ZOTPILOT.md`:**
The profile should feel like a researcher summary written by someone who understands the work — not a filled-in template. Include:
- Identity (role, discipline, cross-disciplinary interests)
- Research focus (primary directions, specific problems being worked on)
- Library character (what the collection reveals about their research style and stage)
- Gaps and notes (coverage weaknesses, off-topic entries, what to watch for)

The format is flexible — write what's useful, not what fits a schema.

Trigger: when user says "分析我的文献库", "建立研究档案", "profile my library", or when ZOTPILOT.md does not exist and a research workflow is about to start.

**Organize library (classification advisor):**
get_library_overview + list_collections + list_tags → analyze themes via
search_topic → diagnose issues (uncategorized papers, inconsistent tags,
oversized collections) → propose collection hierarchy + tag normalization
→ interview user for confirmation → batch_collections + batch_tags(add/remove) to execute

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
| "GEMINI_API_KEY not set" | Expected if key was passed to `register`. Only re-run setup if provider is wrong |
| "ZOTERO_API_KEY not set" | Write ops need Zotero Web API credentials — see `references/setup-guide.md` |
| "Document has no DOI" | Cannot use citation tools for this paper |
| "No chunks found" | Paper not indexed — run `index_library(item_key="...")` |
| `save_from_url`: "extension_not_connected" | Chrome not open or ZotPilot Connector not installed/enabled. Open Chrome and check `chrome://extensions/` |
| `save_from_url`: no `item_key` in result | `ZOTERO_API_KEY` not set, or title match was ambiguous. Use `advanced_search(title=...)` to find the item |

### Write operations (tags, collections)

Write tools require Zotero Web API credentials (`ZOTERO_API_KEY` + `ZOTERO_USER_ID`).
If missing, see **Configure Zotero Web API** in `references/setup-guide.md`.

**Single-item:** `add_item_tags`, `set_item_tags`, `remove_item_tags`, `add_to_collection`, `remove_from_collection`, `create_collection`

**Batch (max 100 items):** `batch_tags(action="add|set|remove")`, `batch_collections(action="add|remove")`
Partial failures are reported per-item without rollback.

For detailed parameter reference, see `references/tool-guide.md`.
For common issues and fixes, see `references/troubleshooting.md`.
