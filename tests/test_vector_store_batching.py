"""Tests that VectorStore slices Chroma inserts under the client max batch size.

ChromaDB's collection.add() rejects any single call larger than
client.get_max_batch_size() (5461 on the SQLite backend). Large documents
(books) routinely exceed this, so _guarded_add must slice every insert.
"""

from zotpilot.vector_store import VectorStore


class _RecordingCollection:
    """Stand-in for a Chroma collection that records each add() call's ids."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def add(self, *, ids, documents, embeddings, metadatas):
        self.calls.append(list(ids))


def _make_store(tmp_path, mock_embedder):
    store = VectorStore(tmp_path / "chroma", mock_embedder)
    store.collection = _RecordingCollection()
    return store


class TestChromaBatchSize:
    def test_large_insert_is_sliced_under_cap(self, tmp_path, mock_embedder):
        """A document larger than the cap is split into multiple add() calls,
        with every id preserved in order."""
        from zotpilot.models import Chunk

        store = _make_store(tmp_path, mock_embedder)
        cap = store._max_add_batch
        n = 12_000

        chunks = [
            Chunk(
                text=f"chunk number {i}",
                chunk_index=i,
                page_num=i // 10,
                char_start=i,
                char_end=i + 1,
                section="body",
                section_confidence=1.0,
            )
            for i in range(n)
        ]
        store.add_chunks("BIG001", {}, chunks)

        recorder = store.collection
        # Every individual call stays within the cap.
        for call_ids in recorder.calls:
            assert len(call_ids) <= cap, f"{len(call_ids)} ids exceeds cap {cap}"
        # Slicing actually happened (12k > any realistic cap).
        assert len(recorder.calls) > 1

        # Concatenated ids equal what add_chunks built, in order, with no
        # drops or duplicates — derived from the chunks, not the id format.
        inserted = [cid for call in recorder.calls for cid in call]
        expected = [f"BIG001_chunk_{c.chunk_index:04d}" for c in chunks]
        assert inserted == expected
        assert len(set(inserted)) == n

    def _call_count_for(self, store, n: int) -> int:
        recorder = _RecordingCollection()
        store.collection = recorder
        ids = [f"id_{i}" for i in range(n)]
        docs = [f"doc {i}" for i in range(n)]
        embs = [[0.1] * 8 for _ in range(n)]
        metas = [{"i": i} for i in range(n)]
        store._guarded_add(ids, docs, embs, metas)
        # ids round-trip regardless of slicing.
        assert [cid for call in recorder.calls for cid in call] == ids
        return len(recorder.calls)

    def test_edge_zero_records(self, tmp_path, mock_embedder):
        store = _make_store(tmp_path, mock_embedder)
        assert self._call_count_for(store, 0) == 0

    def test_edge_exactly_cap(self, tmp_path, mock_embedder):
        store = _make_store(tmp_path, mock_embedder)
        assert self._call_count_for(store, store._max_add_batch) == 1

    def test_edge_cap_plus_one(self, tmp_path, mock_embedder):
        store = _make_store(tmp_path, mock_embedder)
        assert self._call_count_for(store, store._max_add_batch + 1) == 2
