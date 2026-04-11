---
name: ztp-review
description: Local library review and literature synthesis
---
# Review Workflow

## Steps
1. **Scope**: Clarify the specific topic, targeted questions, and expected depth.
2. **Search**: 
   - `search_topic` for paper-level discovery
   - `search_papers` for passage-level semantic queries
   - Use `get_paper_details` on candidate papers to retrieve critical missing metadata (full abstract, publication venue).
3. **Expand**: Run `get_passage_context` for key passages that need deeper understanding.
4. **Citation Expansion** (Optional): If the user explicitly asks for references or citing papers, use `get_citations` to trace related works.
5. **Note Integration**: Use `get_notes` to fetch the user's existing insights and fold them directly into the synthesis.
6. **Synthesize**: Cross-reference the findings, cluster by theme, methodology, or timeline.
7. **Report**: Deliver a structured, highly cohesive summary citing the actual sources.

## Fallback
If semantic search fails (due to lack of vector index): fall back to `advanced_search` for metadata-only (author/year/tag) discovery.

## Hard Rules
- Stay local-library-first. Do not call `search_academic_databases` unless the user explicitly pivots to collection building or external discovery.
- If existing library coverage is too thin to adequately answer the prompt, say so immediately and recommend running `ztp-research` to ingest more papers.
