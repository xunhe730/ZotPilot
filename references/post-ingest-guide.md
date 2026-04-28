# Post-Ingest Guide

## Phase 1: 入库验证 (Gate)

After `get_ingest_status` returns `is_final: true`:

### 1.1 Batch verify existence

**Do NOT trust `saved` count blindly.** Verify with ONE batch call:

```
advanced_search(conditions=[{field:"doi", op:"is", value:"doi1"}, ...], match:"any")
```

Compare returned count with `saved` count:
- **Match** → trust ingest results, use `item_key` from ingest status
- **Mismatch** → only then call `get_paper_details(item_key)` for each discrepant item to identify what's missing

### 1.2 Present verified results

| # | Title | PDF | Verified |
|---|-------|-----|----------|
| 1 | ... | attached / none | ✅ / ❌ |

- `attached` — `pdf_available: true` from ingest result or paper details
- `none` — metadata only. **Acceptable** when user has no subscription or publisher is paywalled (not an error). For OA papers, PDF should be available—if missing, retry or report.
- `❌` — reported saved but not found in Zotero

For failed/missing items, show error and suggest: retry with different URL, or manually add in Zotero.

### 1.3 Ask user once

> "入库完成，共 N 篇已验证入库（M 篇有 PDF，K 篇仅元数据，F 篇失败）。
> 是否执行后续处理？包括：索引 → 笔记 → 归类 → 标签。"

- yes → Phase 2 (only for verified items)
- no → stop
- partial → execute only requested steps

## Phase 2: 全自动执行

Execute sequentially for each verified `item_key`. **Step B 的 `get_paper_details` 结果复用于后续所有步骤**——不重复调用。

### Step A — Index

Call `index_library(item_key=...)` for all items (up to 5 parallel).

### Step B — Read metadata (复用贯穿后续步骤)

Call `get_paper_details(item_key)` for each paper. Cache result for Steps C-E:
- abstract/title → note generation (Step C)
- collections → classification check (Step D)
- tags → tagging decision (Step E)

### Step C — Generate brief note

Follow `references/note-analysis-prompt.md` Workflow A:
- 去重检查 → 元数据+向量召回 → 填写模板 → `create_note` → `manage_tags(action="add", ..., tags=["note-done"])`

### Step D — Classify into Collection

- `browse_library(view="collections")` → find best match (ONE call, reuse for all papers)
- `manage_collections(action="add", ...)` — INBOX auto-cleanup is automatic
- If no match → `create_collection(name="...")` then add
- Uncertain → batch and ask once at the end

### Step E — Tag

- `browse_library(view="tags")` → get vocabulary (ONE call, reuse for all papers)
- Select from existing tags only
- `manage_tags(action="add", ...)`
- Never invent new tags silently

### Step F — Final quality report

ONE batch verification call at the end — `advanced_search` by DOIs or `get_paper_details` for the batch:

| # | Title | PDF | Indexed | Note | Collection | Tags |
|---|-------|-----|---------|------|------------|------|
| 1 | ... | ✅ | ✅ | ✅ | ML/NLP | deep-learning; NLP |

Check:
- No papers remain in INBOX (unless partial workflow)
- All have at least one tag
- `note-done` present if notes were requested

Report issues to user. Do not silently pass.

## Single-paper flow (`save_urls`)

1. `save_urls([url])` → check response
2. If no `item_key` → `advanced_search` to locate
3. **[VERIFY]** `get_paper_details(item_key)` — confirm exists + PDF status
4. Present result, ask about post-ingest
5. If confirmed, run Steps A-E
