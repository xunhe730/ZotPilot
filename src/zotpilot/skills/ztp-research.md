---
name: ztp-research
description: >
  Use for finding and ingesting new academic papers into Zotero.
  Trigger on: "调研X领域", "找论文", "论文入库", "帮我收集X相关的文献",
  "survey papers on X", "find recent papers about X", "ingest these DOIs",
  "add papers to my library", "collect papers on X".
  Covers the full pipeline: external search → candidate selection → PDF ingest → tagging → classification → indexing.
  For synthesizing papers already in the library, use ztp-review instead.
---
# Research Workflow

## Language Policy

**Detect the user's language from the message that triggered this workflow and use it consistently throughout.**
- All user-facing messages, prompts, table headers, and blockquotes must be in the detected language.
- The Chinese text in blockquotes below is illustrative only — translate to match user language.
- If the triggering message is ambiguous, default to English.

## Phase 1 — Discovery

1. **Clarify**: Topic, scope, year range, inclusion criteria
2. **External search**: `search_academic_databases` — see **Search SOP** below. Run 2-4 queries that anchor on DOI / venue / concept / quoted phrase. Merge results client-side, dedup by DOI, sort by cited_by_count.
   - Each result carries `local_duplicate: bool` and `existing_item_key: str | None` — the server already dedup'd against your **local library** via DOI + arXiv DOI + `extra` field lookup.
   - **Do NOT** run a separate `advanced_search` dedup call — the annotation is the authoritative source.
3. **[USER_REQUIRED]** Show ranked candidates using **this exact table** — do not invent columns, reorder, or split into multiple tables:

   ```
   | # | 引用量 | 状态 | 标题 (年份) | 作者 | 出版物 | OA |
   |---|-------|------|-----------|------|--------|-----|
   | 1 | 1102  | ✅ new | NSFnets: Physics-informed NN for Navier-Stokes (2020) | Jin et al. | J Comput Phys | ✅ |
   | 2 | 1026  | 📚 已在库 (key=AB12CDEF) | ... | ... | ... | ... |
   ```

   - `状态`: `✅ new` for fresh rows, `📚 已在库 (key=<existing_item_key>)` for local duplicates. Default-exclude 📚 rows from the selection unless the user asks to refresh metadata.
   - `OA`: `✅` when `is_oa_published: true` OR `arxiv_id` non-null, else `❌`.

   **End the turn with this line and stop — no tool calls, no step 4, no ingest:**

   > 请选择要入库的编号（如 `6,9,13` / `全部` / `除 10,14 外全部`），或回复 `取消` 终止。

   Resume at step 4 only after the user's next message names specific rows.

## Phase 2 — Ingestion

4. **Pre-ingest institutional access check** [USER_REQUIRED when applicable]: Determine which selected candidates need an access/subscription confirmation using these rules:

   - **Always check** candidates with `is_oa_published: false`
   - **Also check even when `is_oa_published: true`** if the candidate appears to come from one of these publishers / hosts:
     - IEEE: `publisher` contains `ieee`, or `landing_page_url` contains `ieeexplore.ieee.org`
     - Wiley: `publisher` contains `wiley`, or `landing_page_url` contains `wiley.com` / `onlinelibrary.wiley.com`
     - Springer: `publisher` contains `springer`, or `landing_page_url` contains `springer.com` / `springerlink.com` / `link.springer.com`

   This check must be based on the **actual selected rows the user asked to ingest**, not on the whole search result page.

   If no selected candidates match the rules above, skip step 4 entirely. Otherwise group the matched set by `publisher` (fall back to the hostname of `landing_page_url` when `publisher` is missing — don't require the field to be present), pick one representative per group (prefer `landing_page_url`, else `https://doi.org/{doi}`), then show:

   > 本次入库包含付费出版社的论文，请点击以下链接确认你当前的网络环境可以访问：
   >
   > | 出版社 | 代表论文 |
   > |--------|---------|
   > | {publisher} | [{title}]({landing_page_url or doi_url}) |
   > | … | … |
   >
   > 确认可以正常访问后回复 **Y** 继续入库；无法访问请先启用机构网络/VPN 后告诉我。

   Gate semantics:
   - A `Y` reply to **step 4** only authorizes continuing **Phase 2 ingestion**.
   - It MUST trigger step 5 (`ingest_by_identifiers`) next.
   - It MUST NOT be interpreted as approval for Phase 3 post-processing.

   Connector save uses the user's **current network context**. Do not assume OA means "no login / no subscription confirmation needed" on IEEE, Wiley, or Springer — those publishers can still require user login or institutional access even for papers marked OA by upstream metadata. This reminder pre-empts the common `saved_metadata_only` failure mode.

   **4b. Manual-verification notice** [USER_REQUIRED when applicable — MANDATORY gate, do NOT skip even when `is_oa_published` is true]. A candidate is "Elsevier-like" and triggers this warning whenever **any** of the following holds, regardless of OA status:

   - `needs_manual_verification` is `true` (when the field is present in the candidate)
   - `doi` starts with `10.1016/`
   - `landing_page_url` contains `sciencedirect.com` or `linkinghub.elsevier.com`
   - `publisher` contains `elsevier` (case-insensitive)

   If **any** selected candidate matches, you MUST show this block BEFORE calling `ingest_by_identifiers` and wait for a `Y` reply. Never call ingest in the same turn:

   > ⚠️ 以下论文来自经常需要用户手动完成验证的出版社（如 Elsevier / ScienceDirect）。入库过程中可能出现 publisher 页面的 anti-bot / 登录验证，或 Zotero Desktop 的 translator 确认步骤。**请保持 Zotero 与浏览器在前台，并按页面或 Zotero 的实际提示立即完成验证**：
   >
   > - {title} ({publisher or "Elsevier"})
   > - …
   >
   > 如果没有及时完成这些验证步骤，本次入库可能超时；且 **切勿重复触发入库**——会在库里留下重复的 item。确认你已经准备好后回复 **Y**。

   Gate semantics:
   - A `Y` reply to **step 4b** only authorizes continuing **Phase 2 ingestion**.
   - It MUST trigger step 5 (`ingest_by_identifiers`) next.
   - It MUST NOT be interpreted as approval for Phase 3 post-processing.
   - Step 4b is a one-time batch gate for the selected Elsevier-like items, not a default per-paper stop. After step 4b has been satisfied, continue the selected manual-verification candidates in the same ingest batch unless the tool actually returns `manual_completion_required`, `anti_bot_detected`, or `preflight_blocked`.

   - Only list matching candidates in the bullet list.
   - If none of the selected candidates matches — skip this step entirely.
   - This is independent from step 4's access check. Step 4 may still run for OA items on IEEE / Wiley / Springer; step 4b CANNOT be skipped on OA grounds either. Elsevier's translator dialog triggers regardless of subscription / OA status.

5. **Ingest**: `ingest_by_identifiers(candidates=selected_search_results)`
   - **Forward search result dicts directly.** `search_academic_databases` already returns structured candidates with `doi`, `arxiv_id`, `landing_page_url`, `is_oa_published`, and `title`. Pass the selected rows unchanged to `candidates=`. Do NOT reconstruct identifier strings from memory, and do NOT use the deprecated `identifiers=` parameter for search results.
   - The `local_duplicate` annotation from search tells you which candidates are already in the library. Filter them out before calling ingest unless the user explicitly wants a metadata refresh.
   - If the tool raises `INBOX collection unavailable`, `ZOTERO_API_KEY` / `ZOTERO_USER_ID` is missing — stop and ask the user to configure credentials before retrying.
6. **Handle results**:
   - If `action_required` contains `"preflight_blocked"` → **STOP**, show the blocked report and wait for user to complete verification (see **Preflight Blocking** below)
   - If `action_required` contains `"anti_bot_detected"` (from save_single_and_verify) → **STOP**, tell user to manually open browser for verification, wait for confirmation, retry with IDENTICAL inputs
   - If `action_required` contains "connector_offline" → **STOP**, surface remediation to user
   - All saved → proceed to Phase 3

   Gate semantics:
   - Replies to step 4 / 4b / anti-bot / manual-completion prompts are scoped to the pending Phase 2 action only.
   - Do not treat a bare `Y` from any of those prompts as consent for Phase 3.
   - Phase 3 may start only after step 7's ingest-results prompt is shown and the user replies `Y` to that specific prompt.
   - If any `action_required` entry is present after ingest, you MAY still show step 7's results table for visibility, but you MUST pair it with the retry / remediation instruction for that action and then STOP. In that case, do NOT ask the Phase 3 question yet.
   - **Never combine the Phase 2 retry/remediation gate and the Phase 3 `Y/N` gate in the same message.** The user must never see one prompt where `Y` could mean either "retry blocked ingest" or "enter Phase 3".

   **Preflight Blocking** — when `action_required` contains `"preflight_blocked"`:
   - Preflight detected a real problem (anti-bot, subscription wall, or timeout) before save. **Preflight does NOT auto-open browser tabs — the user must open the URLs themselves.** Never tell the user that a tab was auto-opened.
   - The `action_required` entry contains:
     - `publishers`: affected publisher hostnames (e.g. `sciencedirect.com`)
     - `urls_to_verify`: concrete URLs for the user to open
     - `details[]`: per-publisher `{publisher, error_code, scope, sample_urls}`
     - `message`: pre-formatted human-readable instruction that already includes the URL list — you may surface this field verbatim
   - If you re-render the message yourself, it MUST include:
     1. A one-line reason per publisher (publisher + error_code)
     2. The full `urls_to_verify[]` as a clickable / copy-pasteable list
     3. The ask: "请在浏览器中打开以上链接确认页面可访问（必要时完成 CAPTCHA / 登录），完成后回复 Y，我将重新调用 ingest_by_identifiers。"
   - Passed preflight items may already continue into save in the same batch. Do NOT invent or mention any internal "pending" bookkeeping status to the user.
   - Wait for user confirmation (Y)
   - Re-run `ingest_by_identifiers` with the candidates that still need retry. Do not force already-saved items back through the pipeline unless the user explicitly asks.

   **Two anti-bot signals — both require the same user action (open URL manually):**
   - `preflight_blocked` (preflight stage): show URLs from `urls_to_verify`
   - `anti_bot_detected` (save stage, after preflight passed): show the affected URL from the result
7. **[USER_REQUIRED]** Show ingest results using **this exact table** — one row per result, no extra narrative columns:

   ```
   | # | 状态 | PDF | item_key | 标题 |
   |---|-----|-----|---------|------|
   | 1 | ✅ saved_with_pdf | ✅ | JEKDTA66 | NSFnets ... |
   | 2 | ⚠️ metadata_only | ❌ | Z26SVWHL | Quantifying ... |
   | 3 | ❌ blocked | — | — | ... (reason: anti_bot_detected) |
   ```

   - `状态` column: use the `status` field verbatim (`saved_with_pdf` / `saved_metadata_only` / `duplicate` / `blocked` / `failed`) with a matching emoji (✅ / ⚠️ / 📚 / ❌).
   - `PDF` column: `✅` if `has_pdf: true`, `❌` if false, `—` if no item was created.
   - For `blocked` / `failed` rows, append `(reason: <error or error_code>)` to 标题.
   - Items are already in the `INBOX` collection at this point (routed at save time).
   - If `action_required` is non-empty, show the table first, then show the blocking / retry instruction (for example, manual browser verification + "完成后回复 Y，我将重新调用 ingest_by_identifiers"), and **STOP there**. Do NOT ask about Phase 3 yet. A bare `Y` after that message must resume the pending Phase 2 retry only.
   - When `action_required` is non-empty, the prompt must be single-purpose. Good example:

   > 还有 {n} 篇论文卡在 Phase 2（入库重试）。请先完成上面的浏览器验证；完成后回复 **Y**，我只会重试这些未完成条目的入库。回复 **N** 则结束本轮，暂不进入 Phase 3。

   - Bad example (forbidden): any prompt like "先处理拦截重试还是直接进入后处理，由你决定" or any combined `Y/N` question that mentions both retrying blocked items and entering Phase 3.
   - Only when `action_required` is empty, **STOP here** and ask:

   > 入库完成（已归档到 INBOX 集合）。是否继续进入 Phase 3 后处理（标签 + 分类 + 索引）？(Y/N)

   Do NOT call **any** post-processing tool (`manage_tags`, `manage_collections`, `create_note`, `index_library`) until the user replies `Y` to that specific Phase 3 question. If the user replies `N` or only asks for indexing, skip to step 13.

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
    - `index_library(item_keys=[newly_ingested_keys], batch_size=2)` and repeat while `has_more=true`
    - Only after the final successful batch, use `get_index_stats` + `search_topic` to confirm new papers are searchable
    - If an `index_library` call times out, do NOT immediately resubmit the full set
    - If a follow-up indexing call returns `Indexing in progress, please wait.`, treat that as evidence the previous indexing run is still active, stop immediately, and do NOT call `get_index_stats` in the same turn

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
- A `Y` reply to step 4 / 4b / preflight / anti-bot / manual-completion gates is **not** Phase 3 approval; it only authorizes the pending Phase 2 retry / ingest action
- If `action_required` is non-empty → STOP, surface to user, do NOT work around
- Never substitute web search for `search_academic_databases`
- **Never flatten structured search results.** Phase 1 selection returns full candidate dicts; Phase 2 ingest must receive them via `candidates=`, never via reconstructed `identifiers=[...]`. The `identifiers` parameter is deprecated and only exists for manual user input.
- **Inbox routing happens at save time**, not as a Phase 3 move. `ingest_by_identifiers` puts new items in `INBOX` automatically. Do NOT call `manage_collections` to "move to Inbox" — that is redundant and wasteful.
- **Phase 3 is plan-then-execute.** All tagging / classification must go through a single drafted batch plan (step 9) and a single user confirmation (step 10). Never fire incremental `manage_tags` / `manage_collections` calls before the plan is approved.
- **Tag vocabulary discipline.** Prefer existing tags from the step-8 snapshot. Proposing new tags is allowed when clearly needed, but each must appear as `NEW: <tag>` in the plan for explicit user confirmation — treat as vocabulary additions. NEVER cryptic/prefixed tags (`*方法`, `_tmp`, `#tag`), NEVER workflow-state tags (`已读`, `待读`, `重要`, `TODO`), NEVER single generic filler words (`方法`, `method`, `paper`).
- **Batch-structured tagging.** Identify common tags (shared research theme, applied to all papers) and variant tags (0-1 per paper for content-type distinction). Execute via one batched `manage_tags(action="add", item_keys=[all_new], tags=[common])` call, then small subset calls for variants.
- Treat `manage_tags(action="set")` as destructive — use `add`/`remove` instead
- Do not report "完成" if any paper has missing post-processing steps
