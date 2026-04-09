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
2. Call `research_session(action="get")` to detect any active session.
3. If none exists, call `research_session(action="create", query=...)`.
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
6. Present ranked candidates and stop at checkpoint 1.
7. After explicit approval, call `research_session(action="approve", checkpoint="candidate-review")`.
8. Call `ingest_papers`, then poll `get_ingest_status` until terminal.
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

9. After `ingest_papers` finalizes, read `blocking_decisions` from the response.
   For each decision in the list, present it to the user **once** and wait for
   their choice before proceeding. The list is empty when no decisions need
   attention. The `pdf_missing_items` list is the canonical payload for any
   metadata-only items referenced by a decision.
10. After explicit approval, call `research_session(action="approve", checkpoint="post-ingest-review")`.
11. After checkpoint 2 (`post-ingest-review`) is approved (step 10), execute post-ingest
    steps in the order returned by `suggested_next_steps`.
    Call `index_library(session_id=<active session id>)` as the FIRST step — semantic
    search depends on it. Do not call `index_library` before approval: the tool enforces
    this via a code gate and will reject the call.

    Why: pre-approval indexing writes unverified or partial-batch items into ChromaDB and
    burns embedding API quota. The gate exists because on 2026-04-08 an agent called
    `index_library` immediately after ingest without waiting for checkpoint 2.

    Underlying tools: `index_library` (pass session_id), `create_note(idempotent=True)`,
    `manage_collections`, `manage_tags`, `browse_library`. If `index_library` returns
    `status="blocked"` with `user_consent_required: true`, present the linked decision to
    the user and pass their consent through the field named in `resolution_parameter`.

    **Classify/tag rules**: why: auto-creating taxonomy confuses the user's existing
    organizational structure and is hard to undo. Classify and tag operations reuse
    the user's **existing** collection and tag vocabulary. Before calling
    `manage_collections`/`manage_tags`, call `list_collections` and `list_tags`
    to load the current taxonomy, then assign each new paper to the
    best-matching existing entries. Do not create new collections or tags
    silently. Only when a paper genuinely does not fit any existing category
    (or when the existing taxonomy is clearly inadequate for the imported set),
    stop and ask the user for explicit approval to either create a new
    collection/tag or restructure the existing taxonomy. Never auto-create a
    "topic-of-this-research-session" collection.
12. **Final verification** — why: tool "success" responses don't guarantee data
    integrity — verification catches silent failures before you report to the user.
    Before reporting, run an end-to-end check on every imported item:

    | Check | Tool | What to verify |
    |---|---|---|
    | PDF on disk | `browse_library` / `get_paper_details` | attachment present, not metadata-only |
    | Index coverage | `get_index_stats` / `search_topic` | new items reachable via semantic search |
    | Note created | `get_notes` | `create_note` actually persisted (idempotent may skip) |
    | Collection assignment | `browse_library` | each item lands in the expected **existing** collection |
    | Tag coverage | `get_paper_details` | tags applied and all drawn from existing vocabulary |

    If any check fails, surface it in the report and ask the user how to proceed
    (retry, manual fix, or skip). Do not silently re-run write operations.

13. End with a per-paper report grouped as:
    - ✅ Full success (PDF + indexed + note + classified + tagged)
    - ⚠️ Metadata-only (no PDF — user decides whether to fetch manually)
    - ⚠️ Partial (e.g. indexed but unclassified, or classification unmatched)
    - ❌ Failure / skipped

Hard rules:

- Do not replace `search_academic_databases` with generic web search.
- Do not call `ingest_papers` before checkpoint 1 approval.
- Do not run post-ingest writes before checkpoint 2 approval.
- Keep post-ingest writes idempotent.

**Why the approve gate matters**: `approve` is a state-machine transition that requires
the agent to first surface checkpoint contents to the user. The flow:

  reach checkpoint (auto)
    → research_session(action="get") returns checkpoint payload
    → agent shows that payload to the user in chat
    → user replies with their decision
    → research_session(action="approve") (only succeeds if get was called
      after the checkpoint was reached and within the freshness window)

The tool will reject approve calls that skip the get step or use a stale
get. This is enforced by `last_get_at` + `checkpoint_reached_at` state, not
by inspecting the agent's prose. On 2026-04-08, an agent that fabricated
approval drove post-ingest writes on a partially-failed batch — the state
machine exists so that failure mode cannot recur in a single turn.

For incident history and detailed root causes, see
`references/post-ingest-incidents.md` (load when handling post-ingest
errors or designing similar gates).
