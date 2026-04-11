---
name: ztp-profile
description: Library structure analysis, theme inference, and organization workflow
---
# Profile Workflow

## Steps
1. **Map Taxonomy**: Run `browse_library(view="overview")`, `browse_library(view="collections")`, and `browse_library(view="tags")`.
2. **Orphan Detection**: Run `advanced_search` to isolate orphan papers (no tags AND no collections) that need attention.
3. **Analyze**: Run `profile_library` for a comprehensive full-library theme/density/gap analysis (note: slow, takes 30-60s).
4. **Deep Dive** (Optional): Use `get_notes` / `get_annotations` when note/annotation density reveals hidden themes.
5. **Propose**: Generate a structured curation report explicitly listing 4 categories of proposals:
   - *Tag Cleanup*: Identifying auto-generated or junk tags.
   - *Tag Merge*: Consolidating duplicates (e.g., "AI", "Artificial Intelligence", "LLM").
   - *Collection Refactor*: Restructuring or suggesting new collections based on themes.
   - *Orphan Papers*: Proposing assignments for ungrouped/untagged works.
6. **[USER_REQUIRED]** Present proposal, wait for explicit user approval before execution.
7. **Execute**: 
   - Tag Operations: Use `manage_tags` to clean/merge terms.
   - Collection Assignments: Use `manage_collections` to assign orphaned works.
   - Collection Creation: **[USER_REQUIRED]** If proposing non-existent collections, `manage_collections(action="create")` must be verified and authorized individually.

## Rules
- Prefer the reuse of the existing taxonomy over creating any new tags/collections.
- Treat `manage_tags(action="set")` as destructive — it replaces all tags. Request direct user validation.
- Batch write actions (>5 papers) must not be executed without confirmation.
- `get_annotations` requires a remote `ZOTERO_API_KEY`. If it fails due to missing keys, gracefully skip annotation ingestion rather than pausing.
- Never batch-create multiple new collections silently. Ask for strict itemized permission.
