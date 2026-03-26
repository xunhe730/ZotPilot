# Post-Ingest Guide — Step 4 of Agent Research Discovery

For each successfully saved item with an `item_key`:

1. `index_library(item_key=...)` — call for ALL successfully ingested item_keys **in parallel** (up to 5 concurrent calls). Do NOT call them one by one sequentially. Each call is independent and indexes a single paper.
2. `get_paper_details(item_key)` — read abstract, methods, key findings
3. Based on the content and ZOTPILOT.md context, make judgments:
   - Which collection(s) does this paper belong to? (check existing collections via `list_collections`)
   - If no existing collection clearly matches: tell the user "这篇论文不属于现有任何分类（当前有: X, Y, Z）。建议：[新建分类 'Topic A'] 或 [将现有分类 'Y' 重命名/合并为 'Y+Topic A']。" — wait for user confirmation before executing.
   - If the paper is in INBOX, move it to the appropriate collection with `add_to_collection` and optionally `remove_from_collection` for INBOX.
   - What tags best describe it? (use existing tag vocabulary from `list_tags` where possible)
   - Is there anything worth noting — a key method, finding, or connection to the user's work?
4. `add_to_collection(item_key, collection_key)` + `add_item_tags(item_key, tags)` — classify
5. `create_note(item_key, content)` — only if the paper is highly relevant; write a concise structured note: what it does, key method, main finding, relevance to user's research

Do NOT create notes for every paper — reserve them for papers the user is likely to read closely. Use ZOTPILOT.md to judge relevance.

If `item_key` missing from result: `advanced_search(title=...)` to locate the item first.

## Agent research ingest (single paper, deep read)

Prerequisites: Chrome open, ZotPilot Connector installed, `ZOTERO_API_KEY` configured

1. `save_from_url(url)` → get `item_key` from result
2. `index_library(item_key=...)` → incremental index
3. `get_paper_details(item_key)` → read abstract, methods, conclusions
4. Judge: relevant collection, appropriate tags, worth a structured note?
5. `add_to_collection` + `add_item_tags` → classify
6. `create_note(item_key, content)` → structured reading note if highly relevant

If `item_key` missing: use `advanced_search(title=result["title"])` to locate first.
