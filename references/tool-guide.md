# ZotPilot Tool Guide

You have access to a Zotero library through ZotPilot's 26 MCP tools. This guide tells you how to use them effectively. Follow these instructions precisely — they encode hard-won knowledge about tool behavior, parameter choices, and failure modes.

## Before you start: check readiness

Call `get_index_stats` first. If `total_documents` is 0 or `unindexed_count` is high, tell the user to run `zotpilot index` before searching. Do not attempt search on an empty index.

## Choosing the right search tool

This is the most important decision. Pick wrong and you'll get bad results or waste the user's time.

| User intent | Tool | Why this one |
|---|---|---|
| Find specific passages or claims | `search_papers` | Returns individual chunks with context. Best for "find evidence that X" |
| Survey a topic / find relevant papers | `search_topic` | Returns per-paper results, deduplicated. Best for "what do I have on X" |
| Find a known paper by exact terms | `search_boolean` | Uses Zotero's word index, not embeddings. Best for author names, acronyms, exact phrases |
| Find data tables with numbers | `search_tables` | Searches table content (headers, cells, captions). Only works if tables were indexed |
| Find figures or diagrams | `search_figures` | Searches figure captions. Returns image paths |

**When unsure:** Start with `search_topic` for broad exploration, then `search_papers` to drill into specific claims.

**Combining tools:** For thorough research, chain boolean → semantic. Use `search_boolean` to find papers containing exact terms, then `search_papers` on the same topic for semantic matches the keyword search missed.

## How to use each search tool well

### search_papers — your primary tool

```
search_papers(
  query="natural language description of what you want",
  top_k=10,           # increase to 20-30 for broad searches
  context_chunks=1,    # set to 2-3 when you need more surrounding text
  section_weights={"results": 1.0, "methods": 0.0},  # focus on findings
  required_terms=["EEG", "N400"],  # force exact terms that embeddings might miss
  chunk_types=["text", "table"],   # include tables in results
  author="Smith",      # substring filter, case-insensitive
  year_min=2020,
)
```

**Key behaviors to know:**
- Results are ranked by composite_score = similarity^0.7 × section_weight × journal_weight
- `required_terms` does whole-word case-insensitive matching — use it for acronyms, gene names, chemical formulas
- `section_weights` valid keys: abstract, introduction, background, methods, results, discussion, conclusion, references, appendix, preamble, table, unknown. Set a section to 0 to exclude it entirely
- Chinese queries are auto-translated and searched bilingually
- Returns `doc_id` and `chunk_index` — save these for `get_passage_context`

### search_topic — for paper-level discovery

```
search_topic(
  query="transformer architectures for EEG classification",
  num_papers=10,
)
```

- Returns one entry per paper (not per chunk), sorted by avg_composite_score
- `num_relevant_chunks` tells you how deeply relevant the paper is (higher = discusses topic throughout, not just once)
- Use `best_passage` for a quick preview; use `doc_id` with `get_passage_context` for full text

### search_boolean — for exact matches

```
search_boolean(query="BERT attention mechanism", operator="AND")
```

- This searches Zotero's full-text word index, NOT semantic vectors
- No stemming: "running" won't match "run"
- No phrase search: "neural network" matches both words independently
- Returns `item_key` (not `doc_id`) — use with `get_paper_details`

### search_tables and search_figures

- `search_tables` only returns results if tables were extracted during indexing. Check `get_index_stats` → `chunk_types.table` first. If 0, tables weren't indexed
- `search_figures` searches caption text. Orphan figures (no caption) are included with generic descriptions
- For table context, use `get_passage_context(doc_id, chunk_index, table_page=page, table_index=idx)` to find the text that references the table

## Expanding context

After any search, use `get_passage_context` to read more:

```
get_passage_context(
  doc_id="ABC123",      # from search result
  chunk_index=5,         # from search result
  window=3,              # 1-5 chunks before and after
)
```

Returns `merged_text` (all passages concatenated) and `passages` list with per-chunk metadata. Use `window=3` or higher when the user needs to understand the full argument around a finding.

## Browsing the library

| Task | Tool | Notes |
|---|---|---|
| See all papers | `get_library_overview(limit=100, offset=0)` | Paginate with offset |
| Paper metadata + abstract | `get_paper_details(item_key)` | Use item_key from boolean search or overview |
| List all folders | `list_collections()` | Returns collection keys needed for write ops |
| Papers in a folder | `get_collection_papers(collection_key)` | Use key from list_collections |
| All tags | `list_tags(limit=50)` | Sorted by frequency |

## Organizing the library

Write operations require `ZOTERO_API_KEY` and `ZOTERO_USER_ID` environment variables. If they fail with "ZOTERO_API_KEY not set", tell the user to configure these.

**Tags:**
- `add_item_tags(item_key, ["tag1", "tag2"])` — safe, preserves existing tags
- `remove_item_tags(item_key, ["old-tag"])` — silently ignores missing tags
- `set_item_tags(item_key, ["only", "these"])` — **destructive**: replaces ALL tags. Warn the user before using

**Collections:**
- `add_to_collection(item_key, collection_key)` — paper stays in other collections too
- `remove_from_collection(item_key, collection_key)` — paper stays in library
- `create_collection(name="New Folder", parent_key=None)` — set parent_key for nested folders

**Batch operations:** When organizing many papers (e.g., "tag all ML papers"), search first with `search_topic`, then loop through results calling write operations. Confirm with the user before modifying more than 5 papers.

## Citation exploration

All citation tools require the paper to have a DOI. They use `doc_id` (from semantic search), not `item_key` (from boolean search).

```
# Who cites this paper?
find_citing_papers(doc_id="ABC123", limit=20)

# What does this paper cite?
find_references(doc_id="ABC123", limit=50)

# Quick impact check
get_citation_count(doc_id="ABC123")
```

If you get "Document has no DOI", the paper's DOI field is empty in Zotero. Tell the user.

## Workflow recipes

### Literature review
1. `search_topic(query, num_papers=15)` — find core papers
2. For top 3-5 papers: `get_paper_details(item_key)` — read abstracts
3. `find_references(doc_id)` on the most relevant paper — expand reading list
4. `search_papers(query, section_weights={"results": 1.0, "conclusion": 1.0})` — extract key findings

### "What do I have on X?"
1. `search_topic(query, num_papers=20)` — broad survey
2. Report: paper count, year range, key authors, top passages
3. If user wants depth: `search_papers` on specific sub-questions

### Organize by theme
1. `get_library_overview()` — see current state
2. `search_topic(theme)` — find papers matching theme
3. `create_collection(theme_name)` — create folder
4. For each matching paper: `add_to_collection(item_key, collection_key)`
5. `add_item_tags(item_key, [theme_tag])` — cross-reference with tags

### Find a specific paper
1. Try `search_boolean(query="author_last_name keyword", operator="AND")` first
2. If no results, try `search_papers(query="description of the paper")`
3. `get_paper_details(item_key)` to confirm it's the right one

## Error handling

| Error | Cause | Fix |
|---|---|---|
| Empty results from search | Index empty or query too specific | Check `get_index_stats`. Try broader query or `search_boolean` |
| "GEMINI_API_KEY not set" | Embedding provider needs API key | User must set env var |
| "ZOTERO_API_KEY not set" | Write operation without credentials | User must set env var |
| "Document has no DOI" | Citation lookup on paper without DOI | Cannot use citation tools for this paper |
| "No chunks found for doc_id" | Paper not indexed | Run `index_library(item_key="...")` |
| `chunk_types.table` is 0 | Tables weren't extracted | Re-index with `index_library(force_reindex=True)` |

## Presenting results to the user

When showing search results:
- Lead with the paper title, authors, year, and citation key
- Quote the relevant passage directly
- Include page number and section name
- For multiple results, organize by paper (not by chunk)
- When showing tables, render the markdown table directly
- For citations, mention the journal name and citation count

Do not dump raw JSON to the user. Format results as readable text with the key information highlighted.
