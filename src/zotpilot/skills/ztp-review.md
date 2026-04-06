---
name: ztp-review
description: Local-library review and literature synthesis workflow
---

# ztp-review

Use this workflow for local-library-first review and synthesis.

Requirements:

- Set `ZOTPILOT_TOOL_PROFILE=extended`

Workflow:

1. Clarify the review question, scope, and desired depth.
2. Use `search_topic` and `advanced_search` to define the local paper set.
3. Use `search_papers` and `get_passage_context` to extract supporting evidence.
4. Use `get_paper_details` to fill metadata gaps for cited papers.
5. Use `get_citations` only when the user explicitly wants citation expansion.
6. Draft an outline, refine it with the user if needed, then synthesize the review.
7. Optionally write the final synthesis back with `create_note`.

Rules:

- Stay local-library-first.
- Do not call `search_academic_databases` unless the user explicitly pivots to collection building.
- If coverage is too thin, say so and recommend `ztp-research`.
