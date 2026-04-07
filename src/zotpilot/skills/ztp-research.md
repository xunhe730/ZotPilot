---
name: ztp-research
description: Literature discovery, ingest, indexing, and organization workflow
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
9. Present ingest results and downstream plan, then stop at checkpoint 2.
   - **If `saved_metadata_only > 0`**, surface a clear warning: "N of M papers saved as metadata-only (no PDF attached). These cannot be indexed or semantically searched."
     List the affected titles from `pdf_missing_items` and ask the user to choose:
     (1) log in to institutional VPN/SSO and re-ingest those URLs, (2) keep as
     metadata-only references, or (3) delete the metadata-only entries. Do NOT
     silently proceed to indexing — the user must make a conscious choice.
10. After explicit approval, call `research_session(action="approve", checkpoint="post-ingest-review")`.
11. Run `index_library` as needed until `has_more=false`.
    - If the response includes `skipped_no_pdf_count > 0`, **do not treat this as
      success alone**: list the skipped titles from `skipped_no_pdf_items` and
      remind the user that those entries remain reference-only until PDFs are
      attached.
12. Use `browse_library`, `manage_collections`, `create_note(idempotent=True)`, and `manage_tags(action="add")` for post-ingest organization.
13. End with a per-paper report that separates success (with PDF), metadata-only, failure, and skipped items.

Hard rules:

- Do not replace `search_academic_databases` with generic web search.
- Do not call `ingest_papers` before checkpoint 1 approval.
- Do not run post-ingest writes before checkpoint 2 approval.
- Keep post-ingest writes idempotent.
