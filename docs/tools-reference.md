# Tools Reference

## Search Tools

### search_papers
Semantic search over research paper chunks. Returns relevant passages with surrounding context, reranked by composite score.

**Parameters:**
- `query` (str): Natural language search query
- `top_k` (int, 1-50): Number of results (default: 10)
- `context_chunks` (int, 0-3): Adjacent chunks to include (default: 1)
- `year_min` / `year_max` (int): Publication year filter
- `author` (str): Author name substring filter
- `tag` (str): Zotero tag substring filter
- `collection` (str): Collection name substring filter
- `chunk_types` (list[str]): Filter by "text", "figure", "table"
- `section_weights` (dict): Override section relevance weights
- `journal_weights` (dict): Override journal quartile weights
- `required_terms` (list[str]): Words that must appear in results

### search_topic
Find most relevant papers for a topic, deduplicated by document. Each paper scored by average composite relevance.

**Parameters:** Same as search_papers minus `context_chunks` and `required_terms`. Uses `num_papers` instead of `top_k`.

### search_boolean
Boolean full-text search using Zotero's native word index. Exact word matching with AND/OR logic.

**Parameters:**
- `query` (str): Space-separated search terms
- `operator` (str): "AND" or "OR" (default: "AND")
- `year_min` / `year_max` (int): Year filter

### search_tables
Search for tables by content (headers, cells, captions).

**Parameters:** `query`, `top_k`, `year_min`, `year_max`, `author`, `tag`, `collection`, `journal_weights`

### search_figures
Search for figures by caption content.

**Parameters:** `query`, `top_k`, `year_min`, `year_max`, `author`, `tag`, `collection`

## Context Tool

### get_passage_context
Expand context around a specific passage. Use after search_papers.

**Parameters:**
- `doc_id` (str): Document ID from search results
- `chunk_index` (int): Chunk index from search results
- `window` (int, 1-5): Chunks before/after (default: 2)
- `table_page` / `table_index` (int): For table context lookup

## Library Tools

### list_collections
List all Zotero collections with hierarchy.

### get_collection_papers
Get papers in a specific collection. Params: `collection_key`, `limit`.

### list_tags
List all tags with usage counts. Params: `limit`.

### get_paper_details
Get complete metadata for a paper. Params: `item_key`.

### get_library_overview
Paginated overview of all papers. Params: `limit`, `offset`.

## Indexing Tools

### index_library
Index Zotero PDFs into vector store. Params: `force_reindex`, `limit`, `item_key`, `title_pattern`, `no_vision`.

### get_index_stats
Get index statistics and check for unindexed papers.

## Citation Tools

### find_citing_papers
Find papers citing a document (via OpenAlex). Params: `doc_id`, `limit`.

### find_references
Find papers referenced by a document. Params: `doc_id`, `limit`.

### get_citation_count
Get citation and reference counts. Params: `doc_id`.

## Write Tools

### set_item_tags / add_item_tags / remove_item_tags
Manage Zotero tags on items. Requires ZOTERO_API_KEY.

### add_to_collection / remove_from_collection
Manage collection membership. Requires ZOTERO_API_KEY.

### create_collection
Create a new Zotero collection. Params: `name`, `parent_key`.

## Admin Tools

### get_reranking_config
Get current reranking weights and valid section names.

### get_vision_costs
Get vision API usage and cost summary. Params: `last_n`.
