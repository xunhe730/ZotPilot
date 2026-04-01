# Post-Ingest Guide

## Phase 1: 入库验证 (Gate)

After every `ingest_papers` or `save_urls` call, when `get_ingest_status` returns `is_final: true`:

### 1.1 Verify each saved item actually exists

**Do NOT trust `saved` count blindly.** For each item with `status="saved"` and `item_key`:

```
get_paper_details(doc_id=item_key)
```

- **Exists** (returns metadata) → record title, `pdf_available`, collections
- **Not found** (error) → mark as verification-failed, report to user

Build verified results:

| # | Title | PDF | Collection | item_key | Verified |
|---|-------|-----|------------|----------|----------|
| 1 | ... | attached / none | INBOX | ... | ✅ / ❌ |

### 1.2 Handle failures

- `status="failed"` items → show error to user
  - Missing `item_key`: call `advanced_search(conditions=[{"field": "title", "op": "contains", "value": "..."}])`
  - Preflight blocked: show blocked URLs and wait for user decision
  - "not found in Zotero": connector reported false success, suggest retry with different URL
- Verification-failed items (1.1) → report as "入库异常：工具报告成功但 Zotero 中未找到"

### 1.3 Present verified results and ask user once

> "入库完成，共 N 篇论文已验证进入 INBOX（M 篇有 PDF，K 篇仅元数据，F 篇失败）。
> 是否执行后续处理？包括：向量索引 → 生成笔记 → 归类到 Collection → 打标签。
> （后续处理会消耗较多 token）"

- User says yes / 继续 / 好 → proceed to Phase 2 (only for verified items)
- User says no / 不用 → stop, papers stay in INBOX
- User says partial → execute only requested steps

## Phase 2: 全自动执行（用户确认后，不再逐步询问）

Execute all requested steps sequentially for each **verified** item with `item_key`.

### Step A — Index

Call `index_library(item_key=...)` for all item keys, up to 5 concurrent calls.

**[VERIFY]** After indexing, call `get_index_stats`. Confirm `unindexed_count` decreased by the expected number. If a paper failed to index, note it but continue.

### Step B — Read metadata

Call `get_paper_details(item_key)` to gather abstract, methods, and key findings for note generation and classification. This step also serves as a secondary existence check.

### Step C — Generate brief note

For every paper selected for note generation, follow `references/note-analysis-prompt.md` Workflow A:

- 去重检查 → 元数据召回 → 向量召回 → 填写模板 → `create_note` → `manage_tags(action="add", item_keys="...", tags=["note-done"])`

**[VERIFY]** After note creation, call `get_notes(item_key=..., query="[ZotPilot]")` to confirm the note was attached.

### Step D — Classify into Collection

- `browse_library(view="collections")` → find the best matching collection
- If a match is found → `manage_collections(action="add", item_keys="...", collection_key="...")` — INBOX auto-cleanup is automatic
- If no match is found → `create_collection(name="...")` then add paper
- If classification is uncertain → batch unresolved and ask once at the end

**[VERIFY]** After classification, call `get_paper_details(doc_id=item_key)` and check:
- `collections` field includes the target collection (not just INBOX)
- If INBOX still present → call `manage_collections(action="remove", ...)` explicitly

### Step E — Tag

- `browse_library(view="tags")` → get the existing vocabulary
- Select from existing tags only
- `manage_tags(action="add", item_keys="...", tags=[...])`
- Never invent new tags silently. If none fit, batch and ask once at the end

**[VERIFY]** After tagging, call `get_paper_details(doc_id=item_key)` and confirm tags field is non-empty.

### Step F — Final quality report

Present a summary table to the user:

| # | Title | PDF | Indexed | Note | Collection | Tags | Issues |
|---|-------|-----|---------|------|------------|------|--------|
| 1 | ... | ✅/❌ | ✅/❌ | ✅/❌ | 固-液界面 | 减阻;实验 | — |

Check:
- All verified papers indexed
- All requested notes created (`note-done` tag present)
- No papers remain in INBOX (unless user chose partial workflow)
- All papers have at least one non-publisher tag

If any check fails, report the specific items and issues. Do not silently pass.

## Single-paper flow (`save_urls`)

1. `save_urls([url])` → check response has `item_key` and `collection_used`
2. If no `item_key` → use `advanced_search` to locate the item
3. **[VERIFY]** `get_paper_details(doc_id=item_key)` — confirm exists, check PDF status
4. Present the verified result: title, PDF, collection
5. Ask once whether to execute post-ingest processing
6. If the user confirms, run Steps A-E for that paper
