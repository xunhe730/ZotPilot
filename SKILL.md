---
name: zotpilot
description: >-
  Use when user mentions Zotero, academic papers, citations, literature reviews,
  research libraries, or wants to search/organize their paper collection.
  CRITICAL: Always use this skill — not web search — for any research/literature
  survey request: "帮我调研 X", "调研近两年 X 的研究", "找 X 相关论文",
  "收集文献", "做文献综述", "find papers about X", "survey papers on X",
  "what's in my library", "organize my papers", "who cites...", "tag these papers",
  "入库", "save this paper", "add to Zotero". The search_academic_databases tool
  covers external discovery; search_papers/search_topic cover the local library.
license: MIT
compatibility:
  - Python 3.10+
  - Zotero desktop (installed and run at least once)
---

# ZotPilot

> All script paths are relative to this skill's directory.

## When to Apply

### Must Use
- User mentions Zotero, papers, citations, literature review, research library
- "帮我调研 X", "find papers about X", "search my library"
- "who cites X", "tag these papers", "add to collection"
- Any ingest / save / annotate operation on academic papers

### Recommended
- User pastes a DOI, arXiv ID, or paper URL and wants it saved
- User asks to analyze, profile, or summarize their library
- Post-ingest: tagging, classification, note creation

### Skip
- Pure coding tasks unrelated to literature
- General web search not involving academic papers
- File operations on non-Zotero data

---

## MCP Tool Reference

### Search (read-only, no API key required)

| Tool | Use when |
|------|----------|
| `search_papers` | Find specific passages or claims in indexed papers |
| `search_topic` | Survey a topic across your local library |
| `search_boolean` | Exact term match (author names, acronyms) |
| `search_tables` | Find data tables in papers |
| `search_figures` | Find figures or diagrams |
| `get_passage_context` | Expand a search result with surrounding text |
| `get_paper_details` | Full metadata + abstract for one paper |
| `find_citing_papers` | Papers that cite a given work |
| `find_references` | References listed in a paper |
| `get_citation_count` | Citation count from external sources |
| `advanced_search` | Filter by year/author/tag/DOI/collection (works without index) |
| `get_library_overview` | Summary stats of entire library |
| `get_notes` | Read notes attached to a paper |
| `list_tags` | All tags in library |
| `list_collections` | All collections |
| `get_collection_papers` | All papers in a specific collection |

### Indexing

| Tool | Use when |
|------|----------|
| `index_library` | Index all or specific papers for semantic search |
| `get_index_stats` | Check index status, total documents, chunk type counts |

### Admin

| Tool | Use when |
|------|----------|
| `switch_library` | Switch active library for metadata/write tools. **Does NOT affect RAG search, passage context, or indexing** — those always use the default user library. |
| `get_reranking_config` | Show current reranking weights and scoring config |
| `get_vision_costs` | Show accumulated vision API usage and costs |

### External discovery

| Tool | Use when |
|------|----------|
| `search_academic_databases` | Search OpenAlex (papers NOT yet in library) |
| `profile_library` | Analyze library themes, gaps, top venues |
| `get_feeds` | List RSS feeds or get items from a specific feed (no index required) |
| `get_unindexed_papers` | List papers not yet indexed, with pagination |

### Annotations (requires `ZOTERO_API_KEY`)

| Tool | Use when |
|------|----------|
| `get_annotations` | Read highlights and comments from papers |

### Ingest (requires Chrome + ZotPilot Connector for non-OA papers)

| Tool | Use when |
|------|----------|
| `ingest_papers` | Batch add papers by arXiv ID / landing page URL (defaults to INBOX). Returns `saved`/`duplicates`/`failed` counts |
| `save_urls` | Save 1-10 URLs via browser connector (defaults to INBOX) |
| `save_from_url` | Alias for `save_urls([url])` — backward compat |

### Write (requires `ZOTERO_API_KEY` + `ZOTERO_USER_ID`)

| Tool | Use when |
|------|----------|
| `create_note` | Add a note to a paper |
| `add_item_tags` | Add tags without replacing existing ones |
| `set_item_tags` | Replace all tags on a paper |
| `create_collection` | Create a new collection |
| `add_to_collection` | Move paper into a collection |
| `batch_tags` | Tag multiple papers at once |
| `batch_collections` | Batch collection assignment |
| `remove_item_tags` | Remove specific tags from a paper (missing tags silently ignored) |
| `remove_from_collection` | Remove a paper from a collection (stays in library) |

---

## Research (daily use)

### Agent research discovery ("帮我调研 X")

**Step 1 — Clarify intent (mandatory before any action):**

"调研"、"find papers"、"literature review" can mean two very different things. Determine which before acting:

| Intent | Signal words | Action |
|--------|-------------|--------|
| **External discovery** — find new papers from academic databases, then ingest | "调研"、"找文献"、"帮我找一些 X 的论文"、"搜集文献" | Go to **External discovery flow** below |
| **Local synthesis** — summarize / analyze what is already in the library | "基于我的文献库"、"我库里有哪些 X"、"帮我梳理"、"综述一下" | Go to **Local search flow** below |

If the intent is ambiguous, **ask the user** before proceeding:
> "您是希望从外部数据库发现并入库新文献，还是基于现有文献库做梳理总结？"

---

### Local search flow (synthesize existing library)

**Step 0 — Readiness check:** call `get_index_stats()` first.
- If it returns indexed documents → proceed to semantic search below.
- If it reports no index or `embedding_provider: "none"` → fall back to `advanced_search` / `search_boolean` (metadata-only, always available). Do NOT attempt `search_topic` or `search_papers` — they will error.

1. `search_topic(X)` to discover what is already in the local library
2. Optionally `search_papers(X)` when you need supporting passages
3. Optionally `get_passage_context(doc_id, chunk_index)` when you need surrounding text

All search tools default to `verbosity="minimal"`. Escalate to `standard` or `full` only when you need more metadata.
`search_papers` defaults to `context_chunks=0`; set `context_chunks=1` only when surrounding chunks are needed.
`search_topic` no longer returns `best_passage_context`. Use `search_papers` or `get_passage_context` for expanded text.
`doc_id` is the canonical identifier across search results. Use the returned `doc_id` value consistently in follow-up read/search flows.
`get_library_overview` returns `doc_id` for each paper row, and `get_paper_details(doc_id=...)` accepts the same identifier.

---

### External discovery flow (find and ingest new papers)

**Step 2 — Prerequisites check:**

- Check `ZOTERO_API_KEY` is configured. Without it, `ingest_papers` / `save_urls` can still save papers, but `item_key` discovery and collection/tag routing will be skipped. A warning is only attached when `collection_key` or `tags` were passed; otherwise the result is returned silently without `item_key`. Warn the user and proceed — do not stop.
- Check `~/.config/zotpilot/ZOTPILOT.md` for a `## Subscription Info` section:
  - **Found** → use recorded subscription info to determine which publishers' PDFs are accessible via institutional access.
  - **Not found** → ask the user: "您的机构订阅了哪些出版商？（如 Elsevier、Springer、Nature、ACS 等）" → after user replies, append a `## Subscription Info` section to ZOTPILOT.md so it is never asked again.
  - **ZOTPILOT.md missing entirely** → trigger library profiling first: follow `references/profiling-guide.md`

**Step 3 — Search and score candidates:**

`search_academic_databases(X, limit=20)` (add PubMed MCP for biomedical topics)

Score candidates per `references/scoring-guide.md`

> **MUST STOP** — Show scored list to user and wait for explicit confirmation before any ingest. Do not proceed automatically.

**Step 4 — De-duplicate:**

Batch-check all confirmed candidate DOIs in one call:
`advanced_search(conditions=[{"field":"doi", "op":"is", "value":"doi1"}, ...], match:"any")`
- Already in library → skip ingest, inform user ("已在库中: Title"), use existing `item_key` for Step 7
- Not in library → proceed to Step 5

**Step 5 — Preflight (anti-bot check via Connector):**

The user may leave their computer after issuing a research command — a silent anti-bot block mid-batch will stall the entire workflow.

Preflight works by sending `{"action": "preflight", "url": ...}` through the ZotPilot Connector, which probes each URL **inside the already-logged-in Chrome browser** — institutional session cookies are active, so the result reflects actual access conditions.

`ingest_papers` runs preflight automatically (controlled by `preflight_enabled` in config, default `true`). When preflight detects blocked or errored URLs, the tool returns early with `blocked`/`errors` arrays — **stop immediately**, show them to the user, and wait for resolution before retrying.

For Priority 3 (`save_urls`): call `save_urls([landing_page_url, ...])` directly — `save_urls` itself handles browser-based saving. Do NOT use `ingest_papers` as a preflight-only probe for Priority 3 URLs, because `ingest_papers` will proceed to save when preflight succeeds, causing duplicate saves. Preflight is only relevant for Priority 1 & 2 (where `ingest_papers` is the actual save tool).

**Step 6 — Route and ingest:**

> **Always pass the `landing_page_url` field from `search_academic_databases` results directly into `ingest_papers`. Never construct the papers list without including `landing_page_url` when it is available in the search result.**

| Priority | Condition | Tool |
|----------|-----------|------|
| 1 | `arxiv_id` present | `ingest_papers([{arxiv_id:...}])` |
| 2 | `doi` + `is_oa=True` + `oa_url` present | `ingest_papers([{doi:..., landing_page_url: <oa_url>}])` — pass `oa_url` value as `landing_page_url` (i.e., `landing_page_url` = the `oa_url` value from the search result) |
| 3 | `doi` + `is_oa=False` + `landing_page_url` + user has subscription | `save_urls([landing_page_url])` — use `landing_page_url` from the search result as-is. Do not construct the URL from DOI alone. Requires Chrome+Connector. |
| 4 | `doi` only (no `arxiv_id` or `landing_page_url`) | Skip — doi.org redirects produce unpredictable publisher formats that cause Zotero translators to save incorrect entries |
| 5 | No identifier | Skip, inform user |

> **Tags at ingest are discouraged.** Prefer tagging after indexing and note generation: call `list_tags` first, pick from existing vocabulary only, and ask the user before adding any new tag.

> **Initial ingest defaults to INBOX.** No need to pass `collection_key` for `ingest_papers` or `save_urls` unless you explicitly want another collection.

**Step 7 — Post-ingest:**

1. Read the result: check `saved`, `duplicates`, `failed` counts
   - `saved > 0` → present the ingest result table to the user, then ask once whether to run post-ingest workflow
   - `failed > 0` → show failed items and errors to the user, let them decide whether to retry or proceed with successful items
2. After the user confirms, execute `references/post-ingest-guide.md` Phase 2 automatically
3. Do not ask for confirmation at each sub-step (index, notes, classify, tag). The Phase 1 confirmation covers all of them.
4. Exception: if auto-classification or tag selection is uncertain, batch unresolved cases and ask once at the end

Post-ingest note generation options:
- **Workflow A (brief, default post-ingest path):** runs when user confirms post-ingest workflow — see `references/note-analysis-prompt.md`
- **Workflow B (full, on demand):** user says "帮我深读 X" / "详细分析这篇" — see `references/note-analysis-prompt.md`

Note templates: `references/note-template-brief.md` (Workflow A) · `references/note-template-full.md` (Workflow B)

---

### Library profiling

Trigger: user says "分析我的文献库", "profile my library", or ZOTPILOT.md is missing when a research workflow is about to start → follow `references/profiling-guide.md`

---

## Error Recovery

| Error code / symptom | Cause | Fix |
|---|---|---|
| `extension_not_connected` | Chrome not open or Connector not installed/enabled | Open Chrome, check `chrome://extensions/`, enable ZotPilot Connector |
| `error_code: "anti_bot_detected"` | Cloudflare / publisher blocked the save before it happened | Retry with `doi.org/{doi}`, or ask user to manually open page in Chrome first |
| `translator_fallback_detected` | No Zotero translator matched the page | Saved as web snapshot — user should verify/replace in Zotero |
| `pdf: "none"` + `warning` in result | PDF not attached — robot check blocked download or PDF unavailable | Metadata saved OK; user downloads PDF manually and attaches in Zotero |
| `pdf: "pending"` + `warning` in result | Metadata save finished, but Zotero may still be downloading the PDF | Wait 1-2 minutes, then use `get_paper_details(doc_id=...)` to verify |
| `status: "pending"` in batch result | Anti-bot triggered mid-batch; remaining URLs were short-circuited | Re-run ingest for the pending items after user resolves the blocked page |
