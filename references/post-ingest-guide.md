# Post-Ingest Guide

## Phase 1: 入库完成确认 (Gate)

After every `ingest_papers` or `save_urls` call:

### 1.1 Check result

- `saved > 0` → proceed to Phase 2
- `failed > 0` → show failed items and errors to the user:
  - Missing `item_key`: call `advanced_search(conditions=[{"field": "title", "op": "contains", "value": "..."}])`
  - Preflight blocked: show blocked URLs to user and wait for their decision
  - Let the user decide whether to retry failed items or proceed with successful ones

### 1.2 Present ingest result to user (mandatory)

Show this table:

| # | Title | PDF | Collection | item_key |
|---|-------|-----|------------|----------|
| 1 | ... | attached / none | INBOX | ... |

- `attached` — PDF downloaded successfully
- `none` — metadata saved, PDF missing (no subscription, anti-bot, or paywalled)

For `pdf: none` items, explain the likely reason.

### 1.3 Ask user once

> "入库完成，共 N 篇论文已进入 INBOX（M 篇有 PDF，K 篇仅元数据）。
> 是否执行后续处理？包括：向量索引 → 生成笔记 → 归类到 Collection → 打标签。
> （后续处理会消耗较多 token）"

- User says yes / 继续 / 好 → proceed to Phase 2
- User says no / 不用 → stop, papers stay in INBOX for manual handling
- User says partial (for example "只索引不要笔记") → execute only the requested steps

## Phase 2: 全自动执行（用户确认后，不再逐步询问）

Execute all requested steps sequentially for each successfully ingested item with `item_key`.

### Step A — Index (always first)

Call `index_library(item_key=...)` for all item keys in parallel, up to 5 concurrent calls.

### Step B — Read metadata

Call `get_paper_details(item_key)` in parallel with Step A to gather abstract, methods, and key findings for note generation and classification.

### Step C — Generate brief note

For every paper selected for note generation, follow `references/note-analysis-prompt.md` Workflow A:

- 去重检查 → 元数据召回 → 向量召回 → 填写模板 → `create_note` → `add_item_tags(["note-done"])`

### Step D — Classify into Collection

- `list_collections` → find the best matching collection
- If a match is found → `add_to_collection(item_key, collection_key)` and `remove_from_collection(item_key, inbox_key)`
- If no match is found → `create_collection(name)` with a reasonable topic name, then add the paper there
- If classification is uncertain → batch all uncertain papers and ask once at the end with suggested destinations

### Step E — Tag

- `list_tags` → get the existing vocabulary
- Select from existing tags only
- `add_item_tags(item_key, selected_tags)`
- Never invent new tags silently. If none fit, batch those papers and ask once at the end

### Step F — Quality check

- All requested papers indexed
- All requested notes created (`note-done` present when note generation was requested)
- No papers remain in INBOX without a target collection unless the user chose a partial workflow
- All `pdf: none` items were reported in Phase 1

If quality checks fail, report a summary to the user instead of asking step-by-step questions.

## Single-paper flow (`save_urls`)

1. `save_urls([url])` → check response has `item_key` and `collection_used`
2. If no `item_key` → use `advanced_search` to locate the item
3. Present the result: title, collection
4. Ask once whether to execute post-ingest processing
5. If the user confirms, run Steps A-E for that paper
