---
name: ztp-research
description: >
  Literature discovery, ingest, indexing, and organization workflow
  for ZotPilot. Use this skill whenever the user mentions finding /
  importing / organizing papers, building a literature collection,
  doing a topic survey, or asks anything that involves
  search_academic_databases, ingest_papers, or post-ingest checkpoints
  — even if they don't explicitly say 'research workflow'. This skill
  enforces the user gates around ingestion that protect against
  partial-batch corruption (see 2026-04-08 incident).
---

# ztp-research

Use this workflow for external discovery and collection building.

Requirements:

- Set `ZOTPILOT_TOOL_PROFILE=research`
- Prefer ZotPilot MCP tools over generic web search

Workflow:

1. Clarify the topic, scope, year range, and inclusion criteria.
2. If the user already has a ZotPilot batch id, resume with `get_batch_status(batch_id=...)`.
3. Otherwise start a new batch with `search_academic_databases`.
4. Use `search_academic_databases` for candidate discovery.

   ## §4 — Search SOP for fuzzy NL queries (REQUIRED)

   For any fuzzy natural-language topic query (no `author:` prefix, no DOI, no quoted phrase, no boolean operators), follow this 4-step SOP **without skipping**:

   ### Step 4.1 — Context priming via WebFetch (REQUIRED)
   Pick ONE authoritative source and call WebFetch to extract anchor information:
   - **Preferred**: English Wikipedia article on the topic
   - **Alternative**: a recent ArXiv survey, OpenReview venue, lab homepage, or named researcher's page

   Extract from the fetched page:
   - 5-10 candidate keyphrases (terms used by the field, not generic)
   - 3-5 anchor author last names (founders/key contributors)
   - 2-3 anchor DOIs (seminal papers explicitly named in the source)

   ### Step 4.2 — Issue 3-5 PRECISE queries in parallel
   Do NOT issue a single bare-NL query. Instead:
   - **DOI direct lookup**: For each anchor DOI, call `search_academic_databases("10.x/yyy")`. The server auto-routes DOI-like queries to direct fetch — guaranteed hit.
   - **Author-anchored**: `search_academic_databases("author:LastName | topic")` for each anchor author.
   - **Phrase boolean**: `search_academic_databases('"exact phrase" AND (term1 OR term2)')` using extracted keyphrases.

   ### Step 4.3 — Merge client-side
   - Deduplicate by DOI
   - Sort by `cited_by_count` desc, then `top_venue` first
   - Note `_source` of each paper (which query path found it)

   ### Step 4.4 — Report transparently to the user
   Format: "Primed via WebFetch [URL] → extracted [author1, author2, ...]; issued N precise OpenAlex queries; returned M unique candidates; anchor papers [title1, title2] recovered via DOI direct lookup."

   ### Worked example
   Query: "AI flow field reconstruction"
   1. WebFetch `https://en.wikipedia.org/wiki/Physics-informed_neural_networks` → extract authors `Raissi, Karniadakis, Brunton`, DOIs `10.1126/science.aaw4741`, `10.1016/j.jcp.2018.10.045`
   2. Parallel queries:
      - `search_academic_databases("10.1126/science.aaw4741")` → Raissi 2020 *Science* (guaranteed)
      - `search_academic_databases("10.1016/j.jcp.2018.10.045")` → Raissi 2019 JCP (guaranteed)
      - `search_academic_databases("author:Raissi | flow")` → Raissi-authored flow papers
      - `search_academic_databases("author:Brunton | sparse sensor flow")` → Brunton group
      - `search_academic_databases('"flow field reconstruction" AND (neural OR PINN)')` → boolean
   3. Merge → top 15-20 candidates including Raissi 2020 *Science* (1854 cites)
   4. Report: "Primed via Wikipedia PINN article → extracted Raissi/Karniadakis/Brunton; 5 OpenAlex queries → 18 unique candidates; Raissi 2020 *Science* and JCP 2019 recovered via DOI direct lookup."

   ### Hard rules
   - **DO NOT** call `search_academic_databases` once with bare NL and call it done.
   - **DO NOT** skip WebFetch priming for fuzzy queries — the server returns `_warnings: [{"code":"missing_priming",...}]` if you do; treat that as a blocking error and restart from Step 4.1.
   - **DO** anchor on extracted DOIs first (cheapest, guaranteed) before keyword/boolean variants.

   ### When to skip §4
   - Query already contains `author:`, DOI, quoted phrase, or boolean operators (`AND`/`OR`/`NOT`) → user/agent already specified intent precisely; single-call is fine.
5. Use `advanced_search` against the local library to detect duplicates.
6. Present ranked candidates and stop for user selection.
7. After explicit approval, call `confirm_candidates(batch_id=..., selected_ids=[...])`.
8. If preflight is clear, call `approve_ingest(batch_id=...)`. If preflight is blocked, call `resolve_preflight(batch_id=...)` only after the user completes browser verification.
9. Poll `get_batch_status(batch_id=...)` until the batch reaches `post_ingest_verified`.
   - If the response contains `error_code: "connector_offline"`, **STOP
     immediately and surface the `error` and `remediation` fields verbatim
     to the user**. Do not silently fall back to any alternate path. Ask
     the user to fix Chrome/Connector and confirm before retrying.
   **Why preflight blocks matter**: preflight 是为用户介入设计的闸门，不是路由
   提示。2026-04-08 的真实事故里，agent 收到 `preflight_blocked` 后改用
   `save_urls` + DOI 链接重试，结果只是把同一个 Cloudflare 墙撞了五次，并把部分
   成功部分失败的脏批次写进了库。preflight 阻塞 = 你停下，用户开浏览器，整批
   重试。降级路径不存在 —— 试图绕过 = 加重事故。

   If `ingest_papers` returns `error_code: "anti_bot_detected"` with
   `blocking_decisions[].decision_id == "preflight_blocked"`: STOP. Surface
   `blocking_decisions` verbatim to the user. Wait for browser verification
   confirmation. Retry `ingest_papers` with IDENTICAL inputs. Do not fall back
   to `save_urls` or DOI links.

10. After ingest verification, read `blocking_decisions` from the response.
   For each decision in the list, present it to the user **once** and wait for
   their choice before proceeding. The list is empty when no decisions need
   attention. The `pdf_missing_items` list is the canonical payload for any
   metadata-only items referenced by a decision.
11. After explicit approval, call `approve_post_ingest(batch_id=...)`.
12. Poll `get_batch_status(batch_id=...)` until the batch reaches `post_process_verified`.
13. Review the `final_report`. Treat `full_success_count`, `partial_count`, per-item `missing_steps`,
    and `reindex_eligible` as the source of truth. Do not infer note/tag/classification completion from prose.
14. If the user accepts the verified report, call `approve_post_process(batch_id=...)`.
15. **Final verification** — why: tool "success" responses don't guarantee data
    integrity — verification catches silent failures before you report to the user.
    Before reporting, confirm the final batch state still matches the underlying tools:

    | Check | Tool | What to verify |
    |---|---|---|
    | PDF on disk | `browse_library` / `get_paper_details` | attachment present, not metadata-only |
    | Index coverage | `get_index_stats` / `search_topic` | new items reachable via semantic search |
    | Final report truth | `get_batch_status` | `final_report.items[].missing_steps` still matches batch item flags |
    | Degraded recovery | `reindex_degraded` | only for items listed in `reindex_eligible` |

    If any check fails, surface it in the report and ask the user how to proceed
    (retry, manual fix, or skip). Do not silently re-run write operations.

16. End with a per-paper report grouped as:
    - ✅ Full success (PDF + indexed + no missing post-process steps)
    - ⚠️ Metadata-only (no PDF — user decides whether to fetch manually)
    - ⚠️ Partial (follow `missing_steps` exactly; do not claim unfinished steps are done)
    - ❌ Failure / skipped

Hard rules:

- Do not replace `search_academic_databases` with generic web search.
- Do not call `confirm_candidates` until the user has selected candidates.
- Do not call `approve_ingest` until the user has approved the preflighted batch.
- Do not call `approve_post_ingest` until the user has reviewed the ingest result.
- Do not call `approve_post_process` until the user has reviewed the final verified report.
- Do not invent legacy session tools; the batch id is the workflow handle.
- Keep post-ingest writes truthful. If `missing_steps` is non-empty, report a partial outcome instead of claiming success.

For incident history and detailed root causes, see
`references/post-ingest-incidents.md` (load when handling post-ingest
errors or designing similar gates).
