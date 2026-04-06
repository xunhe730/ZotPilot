---
name: ztp-profile
description: Library analysis, theme inference, and organization workflow
---

# ztp-profile

Use this workflow for library profiling and organization planning.

Requirements:

- Set `ZOTPILOT_TOOL_PROFILE=extended`

Workflow:

1. Use `browse_library(view="overview")`, `browse_library(view="collections")`, and `browse_library(view="tags")` to map the library.
2. Use `profile_library` for higher-level structure, gaps, and density signals.
3. Use `get_notes` and `get_annotations` only when note density or annotation behavior matters.
4. Propose tag cleanup, collection refactors, and theme summaries before writing anything.
5. After user approval, apply `manage_tags`, `manage_collections`, and `create_collection` conservatively.

Rules:

- Prefer reuse of existing tags and collections over creating new ones.
- Treat `manage_tags(action="set")` as destructive.
- Batch write changes only after explicit user confirmation.
