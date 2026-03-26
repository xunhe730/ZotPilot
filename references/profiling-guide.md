# Profiling Guide — Library Profiling & ZOTPILOT.md

Trigger: when user says "分析我的文献库", "建立研究档案", "profile my library", or when ZOTPILOT.md does not exist and a research workflow is about to start.

Prerequisites: Library indexed (`index_library` run at least once)

This is an **intelligent, adaptive workflow** — not a fixed questionnaire. The agent should bring genuine understanding of the library to the conversation, form hypotheses, and conduct a natural dialogue rather than mechanically asking preset questions.

## Phase 1 — Deep library understanding (before talking to the user)

1. `profile_library()` → get metadata stats + existing_profile
2. Use `search_topic()` across multiple angles to understand what the library actually contains: dominant research themes, methodology patterns, key application domains, temporal trends. Form your own interpretation of what this researcher works on and why.
3. Notice anomalies: off-topic papers, gaps in coverage, unusual clusters — these become conversation material.

## Phase 2 — Dialogue (bring your understanding, not a form)

- Open by sharing your interpretation of the library, including specific observations: "从你的文献结构来看，你似乎在做X方向，重点在Y，Z这块覆盖较少——这个判断准确吗？"
- Let the conversation flow naturally. Use what you know about the library to ask targeted follow-up questions rather than a fixed list. Examples of things worth exploring:
  - What's the core problem they're trying to solve?
  - Which papers/directions are most central to their current work?
  - Are the anomalous/off-topic entries intentional or accidental?
  - What's missing that they wish they had more of?
- Minimum information needed for a useful profile: discipline, role, primary research focus, cross-disciplinary interests — but gather these through conversation, not a checklist.

## Phase 3 — Write `~/.config/zotpilot/ZOTPILOT.md`

The profile should feel like a researcher summary written by someone who understands the work — not a filled-in template. Include:

- Identity (role, discipline, cross-disciplinary interests)
- Research focus (primary directions, specific problems being worked on)
- Library character (what the collection reveals about their research style and stage)
- Gaps and notes (coverage weaknesses, off-topic entries, what to watch for)

The format is flexible — write what's useful, not what fits a schema.

## Organize library (classification advisor)

`get_library_overview` + `list_collections` + `list_tags` → analyze themes via `search_topic` → diagnose issues (uncategorized papers, inconsistent tags, oversized collections) → propose collection hierarchy + tag normalization → interview user for confirmation → `batch_collections` + `batch_tags(add/remove)` to execute.
