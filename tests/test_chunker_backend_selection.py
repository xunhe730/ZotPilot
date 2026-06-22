# tests/test_chunker_backend_selection.py
import types
from pathlib import Path
import zotpilot.indexer as idx
from zotpilot.pdf.chunker import Chunker


def _make_indexer(monkeypatch, backend):
    monkeypatch.setattr(idx, "ZoteroClient", lambda *a, **k: object())
    monkeypatch.setattr(idx, "create_embedder", lambda c: object())
    monkeypatch.setattr(idx, "VectorStore", lambda *a, **k: object())
    monkeypatch.setattr(idx, "JournalRanker", lambda: object())
    cfg = types.SimpleNamespace(
        zotero_data_dir="/tmp", chunk_size=400, chunk_overlap=100,
        chroma_db_path=Path("/tmp"), vision_enabled=False, vision_provider="anthropic",
        chunker_backend=backend,
    )
    return idx.Indexer(cfg)


def test_default_backend_is_char_chunker(monkeypatch):
    inst = _make_indexer(monkeypatch, "char")
    assert isinstance(inst.chunker, Chunker)


def test_llamaindex_backend_selects_token_aware_chunker(monkeypatch):
    import pytest
    pytest.importorskip("llama_index.core")
    pytest.importorskip("tokenizers")
    from zotpilot.pdf.llamaindex_chunker import LlamaIndexChunker
    inst = _make_indexer(monkeypatch, "llamaindex")
    assert isinstance(inst.chunker, LlamaIndexChunker)
