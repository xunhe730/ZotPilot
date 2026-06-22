# tests/test_multi_library_indexing.py
import types
import zotpilot.indexer as idx


def _patch_zoteroclient(monkeypatch):
    """Replace ZoteroClient with a fake that just records its library_id."""
    captured = {}

    class _FakeZC:
        def __init__(self, data_dir, library_id=1):
            self.data_dir = data_dir
            self.library_id = library_id
            captured["library_id"] = library_id

    monkeypatch.setattr(idx, "ZoteroClient", _FakeZC)
    return captured


def test_indexer_forwards_library_id_to_client(monkeypatch, tmp_path):
    captured = _patch_zoteroclient(monkeypatch)
    # Neutralize the rest of __init__ so the test stays a unit test.
    monkeypatch.setattr(idx, "Chunker", lambda **k: object())
    monkeypatch.setattr(idx, "create_embedder", lambda c: object())
    monkeypatch.setattr(idx, "VectorStore", lambda *a, **k: object())
    monkeypatch.setattr(idx, "JournalRanker", lambda: object())

    cfg = types.SimpleNamespace(
        zotero_data_dir=tmp_path, chunk_size=400, chunk_overlap=100,
        chroma_db_path=tmp_path, vision_enabled=False, vision_provider="anthropic",
    )
    idx.Indexer(cfg, library_id=7)
    assert captured["library_id"] == 7
