---
name: zotpilot
description: AI-powered Zotero research assistant — semantic search, citation graphs, and library management via MCP
---

# ZotPilot Skill

## Mode Detection

Check if ZotPilot MCP tools are available in the current session.

**If tools are NOT available** (install mode):
→ See `install-guide.md` for setup instructions.

**If tools ARE available** (usage mode):
→ Follow the search strategy and workflow templates below.

## Search Strategy Decision Tree

Choose the right tool based on your research goal:

| Goal | Tool | When to use |
|------|------|-------------|
| Find specific arguments/claims | `search_papers` | Know roughly what you're looking for |
| Explore a research area | `search_topic` | Discovering what's in the library on a topic |
| Find exact paper by keywords | `search_boolean` | Know specific terms (author names, acronyms) |
| Find data/results in tables | `search_tables` | Looking for quantitative data |
| Find diagrams/charts | `search_figures` | Looking for visual content |

### search_papers tips
- Use natural language queries: "effects of sleep deprivation on memory consolidation"
- Add `required_terms` for acronyms the embedding might miss: `required_terms=["EEG", "N400"]`
- Filter by section: `section_weights={"results": 1.0, "methods": 0.0}` to focus on findings
- Use `chunk_types=["text", "table"]` to include both text passages and tables

### search_topic tips
- Returns deduplicated papers (one entry per paper, not per chunk)
- Use for literature reviews: "What papers in my library discuss transformer architectures?"
- Results include `num_relevant_chunks` — higher means the paper is more deeply relevant

### search_boolean tips
- Exact word matching (no synonyms): "attention mechanism transformer"
- Use OR for alternatives: `operator="OR"` with query "BERT GPT transformer"
- Combine with search_papers: find papers with boolean, then search_papers for specific passages

## Workflow Templates

### Literature Review
1. `search_topic` — find the most relevant papers on your topic
2. `get_paper_details` — get full metadata + abstract for top papers
3. `find_references` — discover what those papers cite (expand your reading list)
4. `search_papers` — deep-dive into specific claims across papers

### Finding Related Work
1. `search_papers` — find passages similar to your research focus
2. `find_citing_papers` — who cites the most relevant papers?
3. `get_citation_count` — gauge paper impact

### Library Organization
1. `get_library_overview` — see all papers with indexing status
2. `search_topic` — find papers on specific themes
3. `create_collection` — create a new folder for the theme
4. `add_to_collection` — organize papers into collections
5. `add_item_tags` — add tags for cross-cutting themes

### Expanding Context
When a search result is interesting but truncated:
- `get_passage_context` with `doc_id` and `chunk_index` from search results
- Increase `window` (1-5) for more surrounding text

## Troubleshooting

| Issue | Fix |
|-------|-----|
| "No results" | Check `get_index_stats` — library may not be indexed yet |
| Low relevance scores | Try rephrasing query, or use `search_boolean` for exact terms |
| Missing tables/figures | Re-index with `index_library` (tables require initial indexing) |
| "GEMINI_API_KEY not set" | Run `zotpilot setup` or set the env var |
| Write operations fail | Set ZOTERO_API_KEY and ZOTERO_USER_ID |
| Stale results | Run `index_library` to pick up new papers added to Zotero |
