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
5. Use `advanced_search` against the local library to detect duplicates.
6. Present ranked candidates and stop at checkpoint 1.
7. After explicit approval, call `research_session(action="approve", checkpoint="candidate-review")`.
8. Call `ingest_papers`, then poll `get_ingest_status` until terminal.
9. Present ingest results and downstream plan, then stop at checkpoint 2.
10. After explicit approval, call `research_session(action="approve", checkpoint="post-ingest-review")`.
11. Run `index_library` as needed until `has_more=false`.
12. Use `browse_library`, `manage_collections`, `create_note(idempotent=True)`, and `manage_tags(action="add")` for post-ingest organization.
13. End with a per-paper report that separates success, failure, and skipped items.

Hard rules:

- Do not replace `search_academic_databases` with generic web search.
- Do not call `ingest_papers` before checkpoint 1 approval.
- Do not run post-ingest writes before checkpoint 2 approval.
- Keep post-ingest writes idempotent.
