# Architecture

## Overview

ZotPilot is a local-first MCP server that provides semantic search over Zotero libraries. It reads Zotero's SQLite database directly (read-only) and maintains a ChromaDB vector index for fast similarity search.

## Index Pipeline

```
Zotero SQLite
      │
      ▼
  zotero_client.py    Read-only access to Zotero's EAV schema
      │
      ▼
  indexer.py           Orchestrates extraction → chunking → embedding → storage
      │
      ├──► pdf/extractor.py     Text + table + figure extraction (pymupdf4llm + pymupdf-layout)
      │        ├── chunker.py              Section-aware text chunking with overlap
      │        ├── section_classifier.py   Heading → section label mapping
      │        ├── orphan_recovery.py      Caption recovery for unmatched figures/tables
      │        └── reference_matcher.py    Maps tables/figures to citing text chunks
      │
      ├──► feature_extraction/   Vision-based table extraction (optional)
      │        ├── vision_api.py           Anthropic Batch API for table cells
      │        ├── paddle_extract.py       PaddleOCR alternative
      │        └── captions.py             Caption detection from PDF blocks
      │
      ├──► embeddings/           Text → vector conversion
      │        ├── gemini.py               Gemini API (asymmetric, 768d)
      │        └── local.py                ChromaDB default (symmetric, 384d)
      │
      └──► vector_store.py       ChromaDB persistent storage
```

## Query Pipeline

```
User query
      │
      ▼
  embeddings/         Embed query (RETRIEVAL_QUERY task type)
      │
      ▼
  vector_store.py     Cosine similarity search in ChromaDB
      │
      ▼
  retriever.py        Context expansion (adjacent chunks)
      │
      ▼
  reranker.py         Composite scoring:
      │               score = similarity^α × section_weight × journal_weight
      │
      ▼
  MCP tool response   Formatted results with metadata
```

## Key Design Decisions

### Local-first
- Zotero SQLite is read directly (no network dependency for reads)
- ChromaDB stores vectors locally
- Only embeddings and citations require network (Gemini API, OpenAlex)

### Read-only SQLite access
- `file:...?mode=ro&immutable=1` — safe even while Zotero is running
- Write operations use Zotero Web API v3 via Pyzotero

### Asymmetric embeddings
- Documents embedded with `RETRIEVAL_DOCUMENT` task type
- Queries embedded with `RETRIEVAL_QUERY` task type
- Improves retrieval quality for Gemini embeddings

### Generic OpenAI-compatible embedding provider
- The `openai-compatible` provider (`embeddings/openai_compat.py`) targets any
  OpenAI-compatible `/embeddings` endpoint (SiliconFlow, Zhipu/GLM, Ollama,
  vLLM, self-hosted). Reaching a new vendor is a `base_url` + `model` +
  `dimensions` configuration choice, not new code.
- `embedding_dimensions` is an explicit, required user input — never
  auto-detected. The configured value flows unchanged to the embedder, is
  asserted against the first response vector's length, and is recorded in the
  Chroma collection metadata (guarded at load time by
  `EmbeddingDimensionMismatchError`).
- `providers.py` is the single source of truth for the embedding allow-list,
  per-provider defaults, the wizard-only vendor preset catalog, and the shared
  `{env:VAR}` secret/URL resolver (`_resolve_secret`).

### Section-aware reranking
- PDF headings classified into academic sections (abstract, methods, results, etc.)
- Each section has a relevance weight in the composite score
- Users can override weights per-query for focused search

### Stdio transport
- MCP server communicates via stdin/stdout
- Parent process monitoring ensures clean shutdown
- No HTTP server or port management needed

## Module Responsibilities

| Module | Lines | Responsibility |
|--------|-------|---------------|
| `state.py` | ~200 | FastMCP instance, lazy singletons, shared helpers |
| `server.py` | ~15 | Entry point, imports tools for registration |
| `tools/*.py` | ~2000 | MCP tool implementations plus ingestion helper modules |
| `pdf/extractor.py` | ~1600 | PDF text/table/figure extraction |
| `pdf/chunker.py` | ~90 | Text chunking with overlap |
| `config.py` | ~100 | Configuration loading with migration |
| `vector_store.py` | ~400 | ChromaDB operations |
| `indexer.py` | ~550 | Index pipeline orchestration |
| `reranker.py` | ~230 | Composite relevance scoring |
| `zotero_client.py` | ~450 | Zotero SQLite read access |
| `embeddings/*.py` | ~230 | Embedding providers (gemini, dashscope, local, openai-compatible) |
| `providers.py` | ~140 | Embedding provider registry, defaults, vendor presets, secret resolver |

## Data Flow

### Document metadata
```
Zotero SQLite → ZoteroItem dataclass → doc_meta dict → ChromaDB metadata
```

### Text content
```
PDF → pymupdf4llm markdown → section classification → chunking → embedding → ChromaDB
```

### Tables
```
PDF → pymupdf-layout detection → cell extraction → caption matching → markdown → embedding → ChromaDB
```

### Figures
```
PDF → pymupdf-layout detection → PNG export → caption matching → caption embedding → ChromaDB
```
