# ZotPilot Advanced Tool Recipes

> Parameter defaults and return fields are in tool schemas — this guide covers **strategies and patterns** only.

## Search Strategies

### Combining search tools

For thorough research, chain keyword → semantic:
1. `search_boolean` to find papers with exact terms (author names, acronyms, gene names)
2. `search_papers` on the same topic for semantic matches that keyword search missed

### Focusing search results

Use `section_weights` to focus on specific paper sections:
- Find key findings: `section_weights={"results": 1.0, "conclusion": 1.0, "methods": 0.0}`
- Find methodology: `section_weights={"methods": 1.5, "results": 0.5}`

Use `required_terms` for exact matching within semantic results — useful for acronyms, chemical formulas, gene names that embeddings might miss.

### Table and figure search

- Check `get_index_stats` → `chunk_types.table` first. If 0, tables weren't extracted during indexing.
- For table context, use `get_passage_context` to find the text that references the table.
- Orphan figures (no caption) appear with generic descriptions in `search_figures`.

## Literature Review Recipe

1. `search_topic(query, num_papers=15)` — find core papers
2. `get_paper_details` on top 3-5 — read abstracts
3. `get_citations(direction="references")` on the most relevant paper — expand reading list
4. `search_papers(query, section_weights={"results": 1.0, "conclusion": 1.0})` — extract findings

## Organize by Theme Recipe

1. `browse_library(view="overview")` — see current state
2. `search_topic(theme)` — find matching papers
3. `create_collection(theme_name)` — create folder
4. `manage_collections(action="add", ...)` — add matching papers
5. `manage_tags(action="add", ...)` — cross-reference with tags

## Research → Ingest → Organize Recipe

1. `search_academic_databases(query, limit=20)` — find candidates
2. Show candidates to user, confirm which to ingest
3. `ingest_papers(papers)` — save to INBOX
4. `get_ingest_status(batch_id)` — wait for completion
5. On user confirmation, follow `references/post-ingest-guide.md`

## Presenting Results

When showing search results to the user:
- Lead with paper title, authors, year
- Quote the relevant passage directly
- Include page number and section name
- For multiple results, organize by paper (not by chunk)
- Render table markdown directly when showing table results
- For citations, mention journal name and citation count

Do not dump raw JSON. Format as readable text with key information highlighted.

## Batch Operation Safety

- Confirm with the user before modifying more than 5 papers
- `manage_tags(action="set")` replaces ALL tags — always warn first
- Batch operations return per-item success/failure — check for partial failures
