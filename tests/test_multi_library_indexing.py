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


def test_index_all_skips_reconcile_when_disabled(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        idx, "reconcile_orphaned_index_docs",
        lambda *a, **k: calls.append(a) or {"deleted_count": 0},
    )
    # Build an Indexer shell without running heavy __init__.
    inst = idx.Indexer.__new__(idx.Indexer)
    inst.config = types.SimpleNamespace(zotero_data_dir=None, chroma_db_path=tmp_path)

    class _Z:
        def get_all_items_with_pdfs(self):
            return []  # empty library -> startup reconcile is the only candidate

    class _Store:
        def get_indexed_doc_ids(self):
            return []

    inst.zotero = _Z()
    inst.store = _Store()
    inst.journal = None
    inst._formula_provider = None
    inst._vision_api = None
    # Stub out filesystem-dependent helpers not under test.
    monkeypatch.setattr(idx.Indexer, "_ensure_formula_provider_available", lambda self: None)
    monkeypatch.setattr(idx.Indexer, "_library_unreachable", lambda self: False)
    monkeypatch.setattr(idx.Indexer, "_load_empty_docs", lambda self: {})
    monkeypatch.setattr(idx.Indexer, "_save_empty_docs", lambda self, m: None)
    # _config_hash_path.exists() must return False so stored_hash stays None (no drift check).
    inst._config_hash_path = types.SimpleNamespace(exists=lambda: False, write_text=lambda t: None)
    monkeypatch.setattr(idx, "_config_hash", lambda c: "test-hash")

    inst.index_all(reconcile=False)
    assert calls == []  # reconciliation suppressed

    inst.index_all(reconcile=True)
    assert len(calls) == 1  # startup reconcile ran (empty-library no-op call)


def test_enumerate_and_union_span_all_libraries(monkeypatch, tmp_path):
    libs = [
        {"library_id": "1", "library_type": "user", "name": "My Library", "item_count": 2},
        {"library_id": "2350352", "library_type": "group", "name": "Group A", "item_count": 1},
    ]

    class _FakeZC:
        def __init__(self, data_dir, library_id=1):
            self.library_id = library_id
        def get_libraries(self):
            return libs
        def get_all_items_with_pdfs(self):
            return []  # union content covered via current_library_pdf_doc_ids patch
        @staticmethod
        def resolve_group_library_id(data_dir, gid):  # stub; replaced by monkeypatch below
            raise NotImplementedError

    monkeypatch.setattr(idx, "ZoteroClient", _FakeZC)
    monkeypatch.setattr(idx.ZoteroClient, "resolve_group_library_id",
                        staticmethod(lambda data_dir, gid: {2350352: 3}[gid]))
    cfg = types.SimpleNamespace(zotero_data_dir=tmp_path)

    assert idx.enumerate_indexable_libraries(cfg) == [(1, "My Library"), (3, "Group A")]

    # Each library contributes a distinct doc id to the union.
    seen = {1: {"AAA"}, 3: {"BBB"}}
    monkeypatch.setattr(
        "zotpilot.index_authority.current_library_pdf_doc_ids",
        lambda zc: seen[zc.library_id],
    )
    assert idx.global_pdf_doc_ids(cfg) == {"AAA", "BBB"}
