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


# ---------------------------------------------------------------------------
# Task 4: index_all_libraries orchestrator
# ---------------------------------------------------------------------------

class _FakeIndexerStore:
    def __init__(self, indexed_ids):
        self._ids = set(indexed_ids)
    def get_indexed_doc_ids(self):
        return set(self._ids)


class _FakeIndexer:
    """Per-library fake recording the reconcile kwarg and returning a canned result."""
    instances = []

    def __init__(self, config, library_id=1):
        self.library_id = library_id
        self.store = _FakeIndexerStore({"AAA"} if library_id == 1 else {"BBB"})
        self.calls = []
        _FakeIndexer.instances.append(self)

    def _library_unreachable(self):
        return False

    def index_all(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "results": [], "indexed": 1, "failed": 0, "empty": 0, "skipped": 0,
            "skipped_long": 0, "has_more": False, "long_documents": [],
            "skipped_no_pdf": [], "quality_distribution": {"A": 1},
            "extraction_stats": {"native": 1},
        }


def _wire_orchestrator(monkeypatch, union):
    _FakeIndexer.instances = []
    monkeypatch.setattr(idx, "Indexer", _FakeIndexer)
    monkeypatch.setattr(idx, "enumerate_indexable_libraries",
                        lambda c: [(1, "My Library"), (3, "Group A")])
    monkeypatch.setattr(idx, "global_pdf_doc_ids", lambda c: set(union))
    recon = []
    monkeypatch.setattr(idx, "reconcile_orphaned_index_docs",
                        lambda store, ids, **k: recon.append((set(ids), k)) or {"deleted_count": 0})
    return recon


def test_orchestrator_runs_each_library_with_reconcile_false(monkeypatch):
    _wire_orchestrator(monkeypatch, {"AAA", "BBB"})
    out = idx.index_all_libraries(types.SimpleNamespace())
    assert [i.library_id for i in _FakeIndexer.instances] == [1, 3]
    assert all(c["reconcile"] is False for i in _FakeIndexer.instances for c in i.calls)
    assert out["indexed"] == 2
    assert out["quality_distribution"] == {"A": 2}
    assert out["extraction_stats"] == {"native": 2}


def test_orchestrator_reconciles_once_against_global_union(monkeypatch):
    recon = _wire_orchestrator(monkeypatch, {"AAA", "BBB"})
    idx.index_all_libraries(types.SimpleNamespace())
    assert len(recon) == 1                       # exactly one global pass
    assert recon[0][0] == {"AAA", "BBB"}         # against the union


# ---------------------------------------------------------------------------
# Step 5: tests ported from main (adapted to _FakeIndexer / _wire_orchestrator)
# ---------------------------------------------------------------------------

def test_index_all_libraries_batch_reports_aggregate_has_more(monkeypatch):
    """Library 1 reports has_more=True with real work done; aggregate must be True."""

    class _HasMoreFakeIndexer(_FakeIndexer):
        def index_all(self, **kwargs):
            self.calls.append(kwargs)
            if self.library_id == 1:
                return {
                    "results": ["r1"], "indexed": 1, "failed": 0, "empty": 0,
                    "skipped": 0, "skipped_long": 0, "has_more": True,
                    "long_documents": [], "skipped_no_pdf": [],
                    "quality_distribution": {"A": 1}, "extraction_stats": {"native": 1},
                }
            return {
                "results": ["r2"], "indexed": 1, "failed": 0, "empty": 0,
                "skipped": 0, "skipped_long": 0, "has_more": False,
                "long_documents": [], "skipped_no_pdf": [],
                "quality_distribution": {"A": 1}, "extraction_stats": {"native": 1},
            }

    _FakeIndexer.instances = []
    monkeypatch.setattr(idx, "Indexer", _HasMoreFakeIndexer)
    monkeypatch.setattr(idx, "enumerate_indexable_libraries",
                        lambda c: [(1, "My Library"), (3, "Group A")])
    monkeypatch.setattr(idx, "global_pdf_doc_ids", lambda c: {"AAA", "BBB"})
    monkeypatch.setattr(idx, "reconcile_orphaned_index_docs",
                        lambda store, ids, **k: {"deleted_count": 0})

    result = idx.index_all_libraries(types.SimpleNamespace(), batch_size=2)
    assert result["has_more"] is True


def test_index_all_libraries_batch_exhaustion_skips_unvisited_library(monkeypatch):
    """Budget depletion at loop top skips unvisited libraries and sets has_more=True."""

    class _BudgetFakeIndexer:
        instances = []

        def __init__(self, config, library_id=1):
            self.library_id = library_id
            self.store = _FakeIndexerStore({"AAA"} if library_id == 1 else {"BBB"})
            self.calls = []
            _BudgetFakeIndexer.instances.append(self)

        def _library_unreachable(self):
            return False

        def index_all(self, **kwargs):
            self.calls.append(kwargs)
            if self.library_id == 1:
                # Indexes exactly batch_size (2) docs with no more pending.
                # This depletes budget to 0, so library 3 is never visited.
                return {
                    "results": ["r1", "r2"], "indexed": 2, "failed": 0, "empty": 0,
                    "skipped": 0, "skipped_long": 0, "has_more": False,
                    "long_documents": [], "skipped_no_pdf": [],
                    "quality_distribution": {}, "extraction_stats": {},
                }
            return {
                "results": ["r3"], "indexed": 1, "failed": 0, "empty": 0,
                "skipped": 0, "skipped_long": 0, "has_more": False,
                "long_documents": [], "skipped_no_pdf": [],
                "quality_distribution": {}, "extraction_stats": {},
            }

    _BudgetFakeIndexer.instances = []
    monkeypatch.setattr(idx, "Indexer", _BudgetFakeIndexer)
    monkeypatch.setattr(idx, "enumerate_indexable_libraries",
                        lambda c: [(1, "My Library"), (3, "Group A")])
    monkeypatch.setattr(idx, "global_pdf_doc_ids", lambda c: {"AAA", "BBB"})
    monkeypatch.setattr(idx, "reconcile_orphaned_index_docs",
                        lambda store, ids, **k: {"deleted_count": 0})

    result = idx.index_all_libraries(types.SimpleNamespace(), batch_size=2)

    # Only library 1 should have been instantiated.
    assert len(_BudgetFakeIndexer.instances) == 1
    # Budget exhaustion must set has_more=True.
    assert result["has_more"] is True
    # Verify the aggregated count from library 1 only.
    assert result["indexed"] == 2


def test_index_all_libraries_does_not_stall_on_fully_indexed_first_library(monkeypatch):
    """A fully-indexed lib1 (has_more=True, indexed=0) must NOT starve lib2."""

    class _StallFakeIndexer:
        instances = []

        def __init__(self, config, library_id=1):
            self.library_id = library_id
            self.store = _FakeIndexerStore({"AAA"} if library_id == 1 else {"BBB"})
            self.calls = []
            _StallFakeIndexer.instances.append(self)

        def _library_unreachable(self):
            return False

        def index_all(self, **kwargs):
            self.calls.append(kwargs)
            if self.library_id == 1:
                # Already fully indexed: has_more=True but zero new work done.
                return {
                    "results": [], "indexed": 0, "failed": 0, "empty": 0,
                    "skipped": 0, "skipped_long": 0, "has_more": True,
                    "long_documents": [], "skipped_no_pdf": [],
                    "quality_distribution": {}, "extraction_stats": {},
                }
            return {
                "results": ["r2"], "indexed": 1, "failed": 0, "empty": 0,
                "skipped": 0, "skipped_long": 0, "has_more": False,
                "long_documents": [], "skipped_no_pdf": [],
                "quality_distribution": {}, "extraction_stats": {},
            }

    _StallFakeIndexer.instances = []
    monkeypatch.setattr(idx, "Indexer", _StallFakeIndexer)
    monkeypatch.setattr(idx, "enumerate_indexable_libraries",
                        lambda c: [(1, "My Library"), (3, "Group A")])
    monkeypatch.setattr(idx, "global_pdf_doc_ids", lambda c: {"AAA", "BBB"})
    monkeypatch.setattr(idx, "reconcile_orphaned_index_docs",
                        lambda store, ids, **k: {"deleted_count": 0})

    result = idx.index_all_libraries(types.SimpleNamespace(), batch_size=5)

    # Both libraries must have been visited.
    assert len(_StallFakeIndexer.instances) == 2
    # Only lib2's work counts.
    assert result["indexed"] == 1


def test_limit_zero_indexes_nothing(monkeypatch):
    """limit=0 must be passed through to index_all as 0, not coerced to None."""
    _wire_orchestrator(monkeypatch, {"AAA", "BBB"})
    idx.index_all_libraries(types.SimpleNamespace(), limit=0)
    for inst in _FakeIndexer.instances:
        assert inst.calls[0]["limit"] == 0, (
            f"library {inst.library_id}: expected limit=0, got {inst.calls[0]['limit']!r}"
        )


def test_aggregate_already_indexed_is_distinct_not_summed(monkeypatch):
    """already_indexed must be the distinct store×union count, not the per-library sum.

    _FakeIndexer stores {"AAA"} for lib 1 and {"BBB"} for lib 3; union is {"AAA","BBB"}.
    The last library visited is lib 3 (store={"BBB"}), so already_indexed = |{"BBB"} & {"AAA","BBB"}| = 1.
    A naive per-library sum would produce 2 (1+1), which is wrong — the distinct count is 1.
    """
    _wire_orchestrator(monkeypatch, {"AAA", "BBB"})
    result = idx.index_all_libraries(types.SimpleNamespace())
    # already_indexed is derived from last_idxr.store (lib 3 -> {"BBB"}) & union ({"AAA","BBB"})
    assert result["already_indexed"] == 1
    assert result["already_indexed"] != 2, "already_indexed must not be the naive sum (2)"


# ---------------------------------------------------------------------------
# Direct reconcile invariant tests (ported from main as-is)
# ---------------------------------------------------------------------------

from zotpilot.index_authority import reconcile_orphaned_index_docs


class _FakeStore:
    def __init__(self, doc_ids):
        self._ids = set(doc_ids)
        self.deleted = []

    def get_indexed_doc_ids(self):
        return set(self._ids)

    def delete_document(self, doc_id):
        self.deleted.append(doc_id)
        self._ids.discard(doc_id)


def test_reconcile_with_union_keeps_other_library_docs():
    # Store holds docs from library A (AAA) and library B (BBB).
    store = _FakeStore({"AAA", "BBB"})
    union = {"AAA", "BBB"}  # global union -> nothing is orphaned

    result = reconcile_orphaned_index_docs(store, union)

    assert result["deleted_count"] == 0
    assert store.deleted == []
    assert store.get_indexed_doc_ids() == {"AAA", "BBB"}


def test_reconcile_without_union_would_delete_other_library_docs():
    # Demonstrates the ORIGINAL bug: reconciling against only library A's docs
    # deletes library B's doc. This documents why the union is required.
    # allow_mass_delete=True is required to bypass the 25% safety floor
    # (deleting 1/2 docs = 50% would otherwise be refused).
    store = _FakeStore({"AAA", "BBB"})
    only_library_a = {"AAA"}

    result = reconcile_orphaned_index_docs(store, only_library_a, allow_mass_delete=True)

    assert "BBB" in result["orphaned_doc_ids"]
    assert store.deleted == ["BBB"]


def test_cli_and_mcp_call_index_all_libraries(monkeypatch):
    import zotpilot.cli as cli
    seen = {}
    monkeypatch.setattr("zotpilot.indexer.index_all_libraries",
                        lambda config, **k: seen.setdefault("called", k) or
                        {"results": [], "indexed": 0, "failed": 0, "empty": 0,
                         "skipped": 0, "already_indexed": 0, "skipped_no_pdf": [],
                         "has_more": False})
    # The CLI/MCP wiring imports index_all_libraries from .indexer; assert the
    # symbol is referenced (smoke check that the call site was switched over).
    import inspect
    assert "index_all_libraries" in inspect.getsource(cli.cmd_index)
    import zotpilot.tools.indexing as ti
    assert "index_all_libraries" in inspect.getsource(ti.index_library)


# ---------------------------------------------------------------------------
# Task 6: cross-library stats
# ---------------------------------------------------------------------------

def test_collect_unindexed_papers_spans_all_libraries(monkeypatch, tmp_path):
    """Behavioral test: _collect_unindexed_papers must visit ALL libraries.

    Setup:
      - Two libraries: lib 1 (My Library) has item AAA; lib 3 (Group A) has item BBB.
      - Global union = {"AAA", "BBB"}.
      - Nothing is indexed (store returns empty set).
    Expected: total == 2 and both AAA and BBB appear in the result.
    This assertion FAILS on the buggy single-library version (total == 1, BBB missing).
    """
    import types as _types
    import zotpilot.tools.indexing as ti
    from zotpilot.zotero_client import ZoteroItem
    from unittest.mock import MagicMock

    # --- fake item constructor (mirrors test_token_budget._make_pdf_item_with_key) ---
    def _make_item(key):
        pdf_path = MagicMock()
        pdf_path.exists.return_value = True
        return ZoteroItem(
            item_key=key,
            title=f"Paper {key}",
            authors="Auth",
            year=2024,
            pdf_path=pdf_path,
            citation_key=f"{key.lower()}2024",
            publication="Journal",
            doi=f"10.1000/{key.lower()}",
            tags="ml",
            collections="AI",
        )

    # lib 1 -> item AAA; lib 3 -> item BBB
    lib_items = {1: [_make_item("AAA")], 3: [_make_item("BBB")]}

    class _FakeZC:
        def __init__(self, data_dir, library_id=1):
            self.library_id = library_id
        def get_all_items_with_pdfs(self):
            return lib_items[self.library_id]

    # Patch the seams _collect_unindexed_papers reads from
    monkeypatch.setattr(ti, "_get_config",
                        lambda: _types.SimpleNamespace(zotero_data_dir=tmp_path))
    monkeypatch.setattr("zotpilot.indexer.enumerate_indexable_libraries",
                        lambda config: [(1, "My Library"), (3, "Group A")])
    monkeypatch.setattr("zotpilot.indexer.global_pdf_doc_ids",
                        lambda config: {"AAA", "BBB"})
    # The fixed code does `from ..indexer import ZoteroClient` at call time,
    # so patching zotpilot.indexer.ZoteroClient is the correct seam.
    import zotpilot.indexer
    monkeypatch.setattr(zotpilot.indexer, "ZoteroClient", _FakeZC)

    class _Store:
        def get_indexed_doc_ids(self):
            return set()  # nothing indexed -> both AAA and BBB are unindexed

    monkeypatch.setattr(ti, "_get_store", lambda: _Store())

    papers, total = ti._collect_unindexed_papers()

    doc_ids = {p["doc_id"] for p in papers}
    assert total == 2, f"expected total=2, got {total} (BBB from group lib missing?)"
    assert "AAA" in doc_ids, "AAA not in results"
    assert "BBB" in doc_ids, f"BBB not in results — group-library paper was skipped! got {doc_ids}"
