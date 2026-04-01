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

## Tool Index

| Tool | Purpose |
|------|---------|
| `search_papers` | Passage-level semantic search |
| `search_topic` | Paper-level topic discovery |
| `search_boolean` | Exact keyword search (author names, acronyms) |
| `advanced_search` | Metadata filter (year/author/tag/DOI/collection, works without index) |
| `search_tables` | Search table content (headers, cells, captions) |
| `search_figures` | Search figure captions |
| `get_passage_context` | Expand a search result with surrounding text |
| `get_paper_details` | Full metadata + abstract for one paper |
| `browse_library` | Browse library overview / tags / collections / collection papers |
| `get_notes` | Read or search notes attached to papers |
| `get_annotations` | Read highlights and comments (requires API key) |
| `profile_library` | Full-library theme analysis |
| `get_citations` | Citation graph: citing papers / references / counts |
| `index_library` | Build or update vector index |
| `get_index_stats` | Check index readiness and stats |
| `get_unindexed_papers` | List papers not yet indexed |
| `manage_tags` | Add / replace / remove tags (single or batch) |
| `create_collection` | Create a new collection folder |
| `manage_collections` | Add / remove papers from collections (single or batch) |
| `create_note` | Create a note on a paper |
| `search_academic_databases` | Search OpenAlex for papers NOT yet in library |
| `ingest_papers` | Batch add papers by arXiv ID / DOI / URL |
| `get_ingest_status` | Poll async ingestion progress |
| `save_urls` | Save 1-10 URLs via browser connector |
| `switch_library` | Switch active Zotero library |

## Critical Behaviors

- `manage_tags(action="set")` — **[DESTRUCTIVE]** replaces ALL existing tags. Confirm with user first.
- `profile_library` — **[SLOW]** full-library scan, takes 30-60 seconds.
- `ingest_papers` — **[ASYNC]** returns `batch_id` immediately. Must poll `get_ingest_status`.
- `manage_collections(action="add")` — auto-removes paper from INBOX when added to another collection.
- Batch write operations — confirm with user before modifying >5 papers.

---

## Intent Router (execute first)

| User intent | Signal words | Workflow |
|-------------|-------------|----------|
| **External discovery** | "调研"、"找文献"、"搜集"、"find papers" | → External Discovery |
| **Local search** | "我库里有"、"帮我梳理"、"综述"、"what's in my library" | → Local Search |
| **Ingest** | "加入 Zotero"、"保存这篇"、"入库"、"save this" | → Direct Ingest |
| **Organize** | "打标签"、"分类"、"归档"、"tag these" | → Organize |
| **Analyze** | "分析我的库"、"画像"、"profile" | → Profile |

If intent is ambiguous, ask:
> "您是希望从外部数据库发现并入库新文献，还是基于现有文献库做梳理总结？"

---

## Workflows

### Local Search

1. Check index readiness → `get_index_stats`
   - If indexed → proceed with semantic search
   - If no index → fall back to `search_boolean` / `advanced_search` only
2. Discover relevant papers → `search_topic`
3. Find specific passages → `search_papers`
4. Expand context → `get_passage_context`

### External Discovery

1. Clarify intent (router above)
2. Prerequisites check → `get_index_stats`; check `ZOTPILOT.md` for subscription info
   - If `ZOTPILOT.md` missing → follow `references/profiling-guide.md` first
3. Search candidates → `search_academic_databases`
4. Score candidates → see `references/scoring-guide.md`
5. **[USER_REQUIRED]** Show scored list, wait for user to confirm which papers to ingest
6. De-duplicate → `advanced_search` (batch DOI check)
7. Ingest → `ingest_papers` (defaults to INBOX)
8. Wait → `get_ingest_status` (poll every ~10 seconds until `is_final` is true)
9. **[VERIFY]** 入库验证 — 对每个 `status="saved"` 的 item_key 调用 `get_paper_details`：
   - 确认论文存在且 title 正确
   - 记录 `pdf_available` 状态
   - 如有 `status="failed"` 的条目，汇总失败原因
10. **[USER_REQUIRED]** 展示验证后的入库结果表（title / PDF 状态 / 集合），ask whether to run post-ingest
11. Post-ingest → see `references/post-ingest-guide.md`

### Direct Ingest

1. `ingest_papers` or `save_urls`
2. `get_ingest_status` (poll if `is_final` is false)
3. **[VERIFY]** 对每个 saved item_key 调用 `get_paper_details` 确认存在
4. **[USER_REQUIRED]** 展示验证结果，ask whether to run post-ingest
5. Post-ingest → see `references/post-ingest-guide.md`

### Organize

1. Browse library → `browse_library`
2. Find target papers → `search_topic` or `advanced_search`
3. Tag → `manage_tags`
4. Classify into collection → `manage_collections` (+ `create_collection` if needed)
5. **[USER_REQUIRED]** Confirm before batch operations on >5 papers

### Profile

Follow `references/profiling-guide.md`

---

## Error Recovery

| Error / symptom | Cause | Fix |
|---|---|---|
| `extension_not_connected` | Chrome not open or Connector not installed | Open Chrome, check extensions, enable ZotPilot Connector |
| `anti_bot_detected` | Publisher blocked the save | Retry with `doi.org/{doi}`, or ask user to open page in Chrome first |
| `translator_fallback_detected` | No Zotero translator matched | Saved as web snapshot — user should verify in Zotero |
| `pdf: "none"` + warning | PDF not attached | Metadata saved; user downloads PDF manually |
| `pdf: "pending"` + warning | Zotero still downloading PDF | Wait 1-2 minutes, then verify with `get_paper_details` |
| `status: "pending"` in batch | Anti-bot mid-batch | Re-run ingest for pending items after user resolves blocked page |
