---
name: ztp-research
description: >
  Literature discovery, ingestion, and post-processing workflow.
  Covers the full pipeline: search → select → ingest → tag → classify → index → verify.
---
# Research Workflow

## Phase 1 — Discovery

1. **Clarify**: Topic, scope, year range, inclusion criteria
2. **External search**: `search_academic_databases` — see **Search SOP** below. Run 2-4 queries that anchor on DOI / venue / concept / quoted phrase. Merge results client-side, dedup by DOI, sort by cited_by_count.
   - Each result carries `local_duplicate: bool` and `existing_item_key: str | None` — the server already dedup'd against your **local library** via DOI + arXiv DOI + `extra` field lookup.
   - **Do NOT** run a separate `advanced_search` dedup call — the annotation is the authoritative source.
3. **[USER_REQUIRED]** Show ranked candidates with scores and an explicit duplicate flag per row (`✅ new` / `📚 already in library (key=<existing_item_key>)`). Default-exclude library duplicates from the selection unless the user explicitly asks to refresh metadata on an existing item. Wait for user selection before proceeding to Phase 2.

## Phase 2 — Ingestion

4. **Pre-ingest institutional access check** [USER_REQUIRED]: Before calling `ingest_by_identifiers`, check the selected candidates' venues. If **any** of them is from a paywalled publisher (Springer / Elsevier / Wiley / IEEE / ACM / Nature Publishing Group / etc.) **and** `is_oa` is false, pause and ask the user **once**:
   > 本次入库包含付费出版社的论文（例如 Springer/IEEE），请确认你当前是否位于**校园网 / VPN / 有机构订阅**的环境。(Y/N)
   >
   > - **Y** → 直接继续入库
   > - **N** → 请先启用机构网络，再告诉我继续
   - Skip this check entirely when all selected candidates are arXiv-only, DOAJ-listed, or already marked `is_oa: true` — those papers are reachable without institutional access.
   - Plan C's Connector save uses the user's **current network context**. Paywalled papers without institutional access will come back as `saved_metadata_only`; this reminder avoids that failure mode before it happens.
5. **Ingest**: `ingest_by_identifiers(candidates=selected_search_results)`
   - **Forward search result dicts directly.** `search_academic_databases` already returns structured candidates with `doi`, `arxiv_id`, `landing_page_url`, `is_oa_published`, and `title`. Pass the selected rows unchanged to `candidates=`. Do NOT reconstruct identifier strings from memory, and do NOT use the deprecated `identifiers=` parameter for search results.
   - The `local_duplicate` annotation from search tells you which candidates are already in the library. Filter them out before calling ingest unless the user explicitly wants a metadata refresh.
   - If the tool raises `INBOX collection unavailable`, `ZOTERO_API_KEY` / `ZOTERO_USER_ID` is missing — stop and ask the user to configure credentials before retrying.
6. **Handle results**:
   - If `action_required` contains `"preflight_blocked"` → **STOP**, show the blocked report and wait for user to complete verification (see **Preflight Blocking** below)
   - If `action_required` contains `"anti_bot"` (from save_single_and_verify) → **STOP**, tell user to manually open browser for verification, wait for confirmation, retry with IDENTICAL inputs
   - If `action_required` contains "connector_offline" → **STOP**, surface remediation to user
   - All saved → proceed to Phase 3

   **Preflight Blocking** — when `action_required` contains `"preflight_blocked"`: 
   - The preflight gate intercepted anti-bot detection (e.g. Cloudflare CAPTCHA) before save. **The browser tab has already been auto-popped to the foreground.**
   - Show a status report distinguishing two statuses:
     | Paper | Status | 说明 |
     |-------|--------|------|
     | Nature paper | preflight_blocked | anti-bot 拦截，需要用户处理 |
     | arXiv paper  | preflight_pending | 通过预检，等待批次放行后入库 |
   - Tell the user:
     > 验证页面已自动在浏览器中打开（[blocked publishers] 的论文），请完成页面验证（如 Cloudflare CAPTCHA / 登录），完成后告诉我，我将重新提交整批入库。preflight_pending 的论文通过了预检，无需任何操作。
   - Wait for user confirmation
   - Re-run `ingest_by_identifiers` with **IDENTICAL inputs** (include ALL original candidates, both preflight_pending and preflight_blocked)

   **Status meanings:**
   - `preflight_blocked`: preflight gate intercepted, paper needs user anti-bot verification. Browser tab already auto-popped.
   - `preflight_pending`: paper passed preflight but is waiting because other publishers in the batch were blocked. Will save normally on retry — **do NOT treat as a failure**.

   **Two anti-bot signals — know the difference:**
   - `preflight_blocked` (NEW): preflight gate intercepted, tab already auto-popped to foreground, user operates the already-open page
   - `anti_bot_detected` (EXISTING, from save_single_and_verify): save stage intercepted, user needs to manually open the URL in browser
7. **[USER_REQUIRED]** Show ingest results table (title / status / has_pdf / item_key). Items are already in the `INBOX` collection at this point. **STOP here** and ask explicitly:
   > 入库完成（已归档到 INBOX 集合）。是否继续进入 Phase 3 后处理（标签 + 分类 + 索引）？(Y/N)

   Do NOT call **any** post-processing tool (`manage_tags`, `manage_collections`, `create_note`, `index_library`) until the user replies `Y`. If the user replies `N` or only asks for indexing, skip to step 13.

## Phase 3 — Post-processing

**Only enter this phase after the user replies Y to step 7.**

Items are already in the `INBOX` collection (routed at save time in Phase 2). Phase 3's job is **topic tagging + moving into domain collections + indexing** — not Inbox management.

Post-processing is a **plan-then-execute** flow: gather context, draft ONE unified batch plan covering all ingested papers, get ONE user confirmation, then batch-execute. Do NOT fire incremental `manage_tags` / `manage_collections` calls while drafting — every call triggers a permission prompt and the user ends up approving 10+ times for a 4-paper batch.

8. **Gather context** for the classification plan (read-only, parallelizable):
    - `browse_library(view="tags")` — snapshot the existing tag vocabulary
    - `browse_library(view="collections")` — snapshot the existing collection tree
    - `get_paper_details` on each new `item_key` to see which auto-generated publisher / arXiv tags need cleanup

9. **Draft the unified batch plan.** A research batch is typically on a single topic, so plan tags in two layers:

    - **Common tags** — 1-3 tags applied to EVERY paper in the batch, capturing the shared research theme (e.g. `视觉语言模型`, `多模态大模型`, `深度学习`). Prefer the existing vocabulary from step 8's snapshot. If no existing tag captures the theme, you may propose at most ONE new common tag — it MUST be listed as `NEW: <tag>` for explicit user confirmation in step 10.
    - **Variant tags** — 0–1 per paper, distinguishing content type within the batch *when the distinction matters*: `综述` (survey), `经典` (seminal / foundational), `实验` (empirical study), `评测` (benchmark), `方法` (new-method proposal). Draw from existing vocabulary or propose new ones as `NEW: <tag>`. **Skip variant tags entirely when the batch is homogeneous** — common tags alone carry the meaning.
    - **Cleanup** (`remove_tags`) — auto-generated publisher / arXiv tags to delete (e.g. `Computer Science - Computation and Language`).

    Build ONE table covering **all** ingested papers:

    | item_key | title | remove_tags | add_tags (common + variant) | target_collection(s) |
    |---|---|---|---|---|
    | ... | ... | (publisher auto-tags) | (e.g. `视觉语言模型`, `综述`) | (from existing tree, or explicit `create: <name>`) |

    **Hard rules while drafting**:
    - **Prefer existing vocabulary.** Verify each tag against the step-8 snapshot before adding it to the plan.
    - **New tags are allowed on demand** — but every new tag must be listed as `NEW: <tag>` so the user confirms it explicitly in step 10. Treat new tags as vocabulary additions (they will apply to future papers too), not as throwaways.
    - **Tag quality**: a valid tag is a meaningful research-domain term (topic / field / content-type / era). NEVER use cryptic or prefixed tags (`*方法`, `_tmp`, `#tag`), NEVER workflow / state tags (`已读`, `待读`, `重要`, `TODO`), NEVER single generic filler words (`方法`, `method`, `paper`, `research`).
    - **Collections from the existing tree first.** Only propose `create: <name>` when no existing collection is a reasonable parent, and justify in a one-line note.
    - **Never use `manage_tags(action="set")`** — destructive; use `add` / `remove` only.
    - Do NOT propose notes unless the user explicitly requested research notes in the original query.

10. **[USER_REQUIRED]** Present the plan as a single table and STOP. Ask:
    > 以上后处理方案是否执行？(Y / 修改 / N)

    - List any `NEW:` tags prominently above the table so the user sees vocabulary additions before confirming.
    - `Y` → proceed to step 11
    - `修改` → accept user edits, redraft, ask again
    - `N` → skip steps 11-12, jump to step 13 (index + verify only)

11. **Execute the approved plan** in as few calls as possible. Exploit batch structure — common tags apply to everyone, variant tags only to subsets:
    - `manage_tags(action="remove", item_keys=[group], tags=[cleanup set])` per group sharing the same cleanup set
    - **One** `manage_tags(action="add", item_keys=[all_new_keys], tags=[common_tags])` — a single call covers the whole batch with common tags
    - Additional `manage_tags(action="add", item_keys=[subset], tags=[variant_tag])` per variant-tag subset (skip this bullet if no variant tags)
    - `manage_collections(action="add", item_keys=[group], collection_key=<key>)` per target collection
    - Only `create_note` entries the user explicitly asked for

12. **Transient error handling**: if a batch call fails with an SSL / network error, retry the **same** batch call once. Do NOT fragment into per-paper calls unless the batch fails twice — fragmentation multiplies permission prompts.

13. **Index update and verify**:
    - `index_library(item_keys=[newly_ingested_keys])`
    - `get_index_stats` + `search_topic` to confirm new papers are searchable

## Phase 4 — Final Report

14. **[USER_REQUIRED]** Present per-paper report:
    - ✅ Full success (PDF + indexed + tagged + classified)
    - ⚠️ Metadata-only (no PDF — note reason: paywall / OA mismatch)
    - ⚠️ Partial (missing: tag / collection / index — list specifically)
    - ❌ Failed / skipped (with reason)

## Cold Start — when the topic is unfamiliar

**WebFetch's role here is KEYWORD DISCOVERY, not literature search.** Its job is to convert the user's fuzzy intent into the vocabulary that `search_academic_databases` can consume (canonical English term, seminal DOIs, venues, concept names).

**Decision rule** — skip this section and go directly to structured search IF you can name, from internal knowledge, all four of:

1. The canonical English term for the topic
2. A seminal paper (by DOI, arXiv ID, or author+title)
3. A common venue (CVPR / NeurIPS / ICLR / TPAMI / ACL / etc.)
4. An OpenAlex concept that covers it ("Computer vision", "Natural language processing", …)

Otherwise — non-English input (`调研XX`), niche/new term, ambiguous acronym, or uncertainty — **run reconnaissance FIRST**. Do NOT ask the user to supply the plan; they invoked `/ztp-research` precisely because they do not know.

### Reconnaissance recipe

1. **WebFetch one reference page** (in order of preference):
   - `https://en.wikipedia.org/wiki/<Topic>` — best for established topics
   - `https://paperswithcode.com/search?q=<term>` — task taxonomies + leaderboards
   - `https://arxiv.org/list/<category>/<yyyy-mm>` — recent papers in a subfield

2. **Extract the search plan** from the page (not the full content):
   - **Canonical English term** → for quoted-phrase queries
   - **2-3 seminal paper DOIs / arXiv IDs** → for DOI-direct lookups
   - **Common venues** → for `venue=` filter
   - **Related concept names** → for `concepts=` filter

3. **Proceed to structured search** with the learned vocabulary via the Search SOP below.

### Anti-patterns

- Do NOT WebFetch when the canonical term is already known (e.g. user says "调研 CLIP" → skip reconnaissance, go DOI direct).
- Do NOT WebFetch to read full papers — that is what `get_paper_details` and `search_papers` are for after ingest.
- Do NOT reconnaissance-loop: one WebFetch is enough to extract a search plan; stop and use the plan.
- Do NOT fall back to bag-of-words queries on retry after a rejection — the guardrail is structural.

## Search SOP — `search_academic_databases`

The tool **hard-rejects** bag-of-words natural-language queries (e.g. `"vision language model survey benchmark"`) unless a structured filter narrows the search. Use one of the four precise query forms, optionally combined with OpenAlex-native filters.

### Query forms

| Form | Example | Use case |
|------|---------|---------|
| DOI direct | `query="10.48550/arxiv.2103.00020"` | Known seminal paper |
| Author-anchored | `query="author:Radford CLIP"` | Known author + topic token |
| Quoted phrase | `query='"visual instruction tuning"'` | Canonical topic term |
| Boolean | `query='"LLaVA" OR "Flamingo" OR "GPT-4V"'` | Method cluster |

### Filters (stack for precision)

| Param | Value | Effect |
|-------|-------|--------|
| `concepts` | `["Computer vision", "Natural language processing"]` | Restrict to OpenAlex concept hierarchy. Escapes fuzzy-query rejection. |
| `venue` | `"CVPR"` / `"NeurIPS"` / `"IEEE TPAMI"` / `"ICLR"` | Restrict to one publication venue. |
| `institutions` | `["Google DeepMind", "Stanford University"]` | Restrict to specific affiliations. |
| `min_citations` | `100` | Cut long tail; tune to topic age. |
| `oa_only` | `true` | Only papers with open-access PDF. |
| `year_min` / `year_max` | `2023` | Publication window. |
| `cursor` | from previous response | Next page (cursor-based pagination). |

### Recipes

- **Known seminal paper** → DOI direct. Fastest, zero noise.
- **Topic discovery on niche term** → `query='"vision-language model"'`, `venue="IEEE TPAMI"`, `year_min=2023`, `sort_by="citations"`.
- **Method cluster survey** → `query='"LLaVA" OR "MiniGPT-4" OR "InstructBLIP"'`, `year_min=2023`.
- **Concept-anchored browse** (bag-of-words becomes OK) → `query="instruction tuning"`, `concepts=["Computer vision"]`, `min_citations=50`.
- **Seed expansion** (after finding 1-2 anchors) → `get_citations(direction="citing", doc_id=<seed>)` to walk the citation graph.

### Response shape

`search_academic_databases` now returns a dict (not a list):
```json
{
  "results": [...],
  "next_cursor": "string|null",
  "total_count": 1234,
  "unresolved_filters": ["venue:Foobar"]   // names that failed name→ID resolution
}
```

If `unresolved_filters` is non-empty, correct the name and retry (e.g. `"TPAMI"` → `"IEEE Transactions on Pattern Analysis and Machine Intelligence"`).

## Hard Rules
- Never skip Phase 1 step 3 (user must select candidates before ingest)
- Never skip the Phase 2 step 7 gate — user must reply `Y` before any Phase 3 tool runs
- If `action_required` is non-empty → STOP, surface to user, do NOT work around
- Never substitute web search for `search_academic_databases`
- **Never flatten structured search results.** Phase 1 selection returns full candidate dicts; Phase 2 ingest must receive them via `candidates=`, never via reconstructed `identifiers=[...]`. The `identifiers` parameter is deprecated and only exists for manual user input.
- **Inbox routing happens at save time**, not as a Phase 3 move. `ingest_by_identifiers` puts new items in `INBOX` automatically. Do NOT call `manage_collections` to "move to Inbox" — that is redundant and wasteful.
- **Phase 3 is plan-then-execute.** All tagging / classification must go through a single drafted batch plan (step 9) and a single user confirmation (step 10). Never fire incremental `manage_tags` / `manage_collections` calls before the plan is approved.
- **Tag vocabulary discipline.** Prefer existing tags from the step-8 snapshot. Proposing new tags is allowed when clearly needed, but each must appear as `NEW: <tag>` in the plan for explicit user confirmation — treat as vocabulary additions. NEVER cryptic/prefixed tags (`*方法`, `_tmp`, `#tag`), NEVER workflow-state tags (`已读`, `待读`, `重要`, `TODO`), NEVER single generic filler words (`方法`, `method`, `paper`).
- **Batch-structured tagging.** Identify common tags (shared research theme, applied to all papers) and variant tags (0-1 per paper for content-type distinction). Execute via one batched `manage_tags(action="add", item_keys=[all_new], tags=[common])` call, then small subset calls for variants.
- Treat `manage_tags(action="set")` as destructive — use `add`/`remove` instead
- Do not report "完成" if any paper has missing post-processing steps
