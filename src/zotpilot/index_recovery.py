"""Zero-cost ChromaDB index recovery from intact SQLite + HNSW segment.

Productizes the proven recovery procedure (see ``.omc/plans`` P2): a quarantined
or version-unreadable Chroma directory still has ALL document text + metadata in
``chroma.sqlite3`` and ALL vectors in the persistent HNSW segment. We read both
directly (the high-level loader segfaults on a version-drifted index, but the
low-level ``chroma-hnswlib`` persistent reader does not), rebuild a fresh
collection by passing the vectors back EXPLICITLY — so the embedder is never
called and recovery costs nothing — write to a NEW directory, verify it, and only
then swap it into place.

When the HNSW segment is unreadable/missing but SQLite is intact, a separate,
opt-in fallback re-embeds the stored text via the configured embedder (this DOES
cost embedding-API calls and must be confirmed by the caller).
"""

from __future__ import annotations

import logging
import pickle
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import chromadb
from chromadb.config import Settings

if TYPE_CHECKING:
    from .interfaces import EmbedderProtocol

logger = logging.getLogger(__name__)

# Metadata key under which ChromaDB stores the document text in embedding_metadata.
_DOC_KEY = "chroma:document"
# Batch size for collection.add() during rebuild.
_ADD_BATCH = 1000
# Install hint surfaced when the optional recovery extra is missing.
INSTALL_HINT = "uv sync --extra recover"


class HnswlibUnavailableError(Exception):
    """Raised when the optional ``chroma-hnswlib`` dependency is not installed."""


class RecoveryVerificationError(Exception):
    """Raised when a rebuilt index fails the pre-swap verification gate."""


class RecoverySourceError(Exception):
    """Raised when no usable recovery source can be located."""


@dataclass
class RecoveryReport:
    """Outcome of a recovery attempt (returned for human sign-off)."""

    source: Path
    method: str = "hnsw"  # "hnsw" (zero-cost) or "reembed" (paid fallback)
    recovered_count: int = 0
    doc_count: int = 0
    merged_count: int = 0
    skipped_no_vector: int = 0
    verified: bool = False
    swapped: bool = False
    dry_run: bool = False
    out_dir: Path | None = None
    swapped_aside: Path | None = None
    messages: list[str] = field(default_factory=list)

    def log(self, message: str) -> None:
        """Record a human-readable progress/diagnostic message."""
        self.messages.append(message)
        logger.info("recover-index: %s", message)


@dataclass
class _ChunkRecord:
    """One reconstructed chunk row from the SQLite metadata segment."""

    chroma_id: str  # embeddings.embedding_id (the string id used in add())
    meta_id: int  # embeddings.id (int PK keying embedding_metadata)
    document: str
    metadata: dict[str, Any]


class _NoEmbeddingFunction:
    """Embedding function that must never be called — recovery supplies vectors."""

    def __call__(self, input: Any) -> Any:  # noqa: A002 - chromadb signature
        raise RuntimeError("embedding function was called during recovery — vectors must be supplied explicitly")

    def name(self) -> str:
        return "noop-recovery"


# ---------------------------------------------------------------------------
# Source discovery
# ---------------------------------------------------------------------------


def discover_corrupt_backups(db_path: Path) -> list[Path]:
    """Return quarantined backups matching the real ``{name}.corrupt-*`` glob.

    Matches the legacy ``{db_path.name}.corrupt-{suffix}`` quarantine naming
    left by older versions. Newest-first by suffix.
    """
    parent = db_path.parent
    if not parent.exists():
        return []
    backups = [p for p in parent.glob(f"{db_path.name}.corrupt-*") if p.is_dir()]
    return sorted(backups, key=lambda p: p.name, reverse=True)


def resolve_source(db_path: Path, source: Path | None) -> Path:
    """Resolve the recovery source directory.

    Explicit ``source`` wins; otherwise autodiscover the newest
    ``{name}.corrupt-*`` backup. Raises ``RecoverySourceError`` if none is found
    or the resolved directory has no ``chroma.sqlite3``.
    """
    if source is not None:
        resolved = Path(source)
        if not (resolved / "chroma.sqlite3").exists():
            raise RecoverySourceError(f"--source {resolved} does not contain chroma.sqlite3 (not a Chroma backup).")
        return resolved

    backups = discover_corrupt_backups(db_path)
    for backup in backups:
        if (backup / "chroma.sqlite3").exists():
            return backup
    raise RecoverySourceError(
        f"No recovery source found. Looked for '{db_path.name}.corrupt-*' next to "
        f"{db_path}. Pass --source <dir> to point at a backup explicitly."
    )


def find_vector_segment_dir(source: Path) -> Path | None:
    """Locate the persistent HNSW segment directory inside a Chroma backup.

    Prefers the VECTOR segment id recorded in ``segments``; falls back to any
    subdirectory containing ``data_level0.bin``.
    """
    sqlite_path = source / "chroma.sqlite3"
    if sqlite_path.exists():
        con = sqlite3.connect(f"file:{sqlite_path}?mode=ro&immutable=1", uri=True)
        try:
            rows = con.execute("SELECT id, type, scope FROM segments").fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            con.close()
        for seg_id, seg_type, scope in rows:
            if scope == "VECTOR" or "hnsw" in (seg_type or "").lower():
                candidate = source / str(seg_id)
                if (candidate / "data_level0.bin").exists():
                    return candidate

    for child in sorted(source.iterdir()):
        if child.is_dir() and (child / "data_level0.bin").exists():
            return child
    return None


# ---------------------------------------------------------------------------
# SQLite reconstruction (text + metadata)
# ---------------------------------------------------------------------------


def _typed_value(row: sqlite3.Row) -> Any:
    """Return the single non-null typed value from an embedding_metadata row."""
    for col in ("string_value", "int_value", "float_value", "bool_value"):
        value = row[col]
        if value is not None:
            return value
    return None


def load_sqlite_records(sqlite_path: Path) -> list[_ChunkRecord]:
    """Reconstruct chunk text + metadata from an intact ``chroma.sqlite3``.

    Join: ``embeddings.embedding_id`` is the chroma id; ``embeddings.id`` keys
    ``embedding_metadata``. Key ``chroma:document`` carries the chunk text; every
    other key is metadata (value = the single non-null typed column; None dropped).
    """
    con = sqlite3.connect(f"file:{sqlite_path}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    try:
        emb_rows = con.execute("SELECT id, embedding_id FROM embeddings").fetchall()
        meta_by_id: dict[int, dict[str, Any]] = {}
        docs_by_id: dict[int, str] = {}
        for row in con.execute(
            "SELECT id, key, string_value, int_value, float_value, bool_value FROM embedding_metadata"
        ):
            value = _typed_value(row)
            if value is None:
                continue
            if row["key"] == _DOC_KEY:
                docs_by_id[row["id"]] = value
            else:
                meta_by_id.setdefault(row["id"], {})[row["key"]] = value
    finally:
        con.close()

    records: list[_ChunkRecord] = []
    for row in emb_rows:
        meta_id = row["id"]
        records.append(
            _ChunkRecord(
                chroma_id=row["embedding_id"],
                meta_id=meta_id,
                document=docs_by_id.get(meta_id, ""),
                metadata=meta_by_id.get(meta_id, {}),
            )
        )
    return records


# ---------------------------------------------------------------------------
# HNSW vector extraction (zero-cost path)
# ---------------------------------------------------------------------------


def load_hnsw_vectors(seg_dir: Path, dim: int) -> dict[str, list[float]]:
    """Read all vectors from a persistent HNSW segment, keyed by chroma id.

    Requires the optional ``chroma-hnswlib`` wheel (the persistent split-file
    format — ``header.bin``/``data_level0.bin``/``length.bin``/``link_lists.bin``
    plus ``index_metadata.pickle`` — is not readable by stock ``hnswlib``).
    """
    try:
        import hnswlib  # type: ignore[import-not-found, import-untyped]
    except ImportError as exc:
        raise HnswlibUnavailableError(
            f"chroma-hnswlib is required to read vectors from the HNSW segment but is "
            f"not installed. Install the optional recovery extra: {INSTALL_HINT}"
        ) from exc

    pickle_path = seg_dir / "index_metadata.pickle"
    # The pickle is ChromaDB's own segment metadata from the user's local index
    # backup (not untrusted external input) — it is the only source of the
    # chroma_id<->HNSW-label map, which Chroma itself stores only as a pickle.
    with open(pickle_path, "rb") as handle:
        meta = pickle.load(handle)
    id_to_label: dict[str, int] = meta["id_to_label"]
    total = int(meta.get("total_elements_added") or len(id_to_label))

    # data_level0.bin is preallocated to max_elements * size_per_element, so the
    # element stride (and thus capacity) is derivable from the file size and the
    # stored element count — no fragile header parsing required.
    data_bytes = (seg_dir / "data_level0.bin").stat().st_size
    size_per_element = data_bytes // total if total else 0
    max_elements = data_bytes // size_per_element if size_per_element else total

    index = hnswlib.Index(space="cosine", dim=dim)
    index.load_index(str(seg_dir), is_persistent_index=True, max_elements=max_elements)

    chroma_ids = list(id_to_label.keys())
    labels = [id_to_label[cid] for cid in chroma_ids]
    items = index.get_items(labels)
    return {cid: list(vec) for cid, vec in zip(chroma_ids, items)}


# ---------------------------------------------------------------------------
# Assembly + rebuild
# ---------------------------------------------------------------------------


def _assemble(
    records: list[_ChunkRecord],
    vectors: dict[str, list[float]],
    dim: int,
) -> tuple[list[str], list[list[float]], list[str], list[dict[str, Any] | None], int]:
    """Join SQLite records with vectors into chromadb.add() arguments.

    Rows without a recovered vector are skipped (reported as ``skipped_no_vector``).
    Raises ``RecoveryVerificationError`` if any vector dimensionality disagrees
    with the configured ``dim``.
    """
    ids: list[str] = []
    embeddings: list[list[float]] = []
    documents: list[str] = []
    metadatas: list[dict[str, Any] | None] = []
    skipped = 0
    for record in records:
        vector = vectors.get(record.chroma_id)
        if vector is None:
            skipped += 1
            continue
        if len(vector) != dim:
            raise RecoveryVerificationError(f"vector for {record.chroma_id} has dim {len(vector)} != configured {dim}")
        ids.append(record.chroma_id)
        embeddings.append(vector)
        documents.append(record.document)
        # chromadb rejects empty metadata dicts; pass None instead.
        metadatas.append(record.metadata or None)
    return ids, embeddings, documents, metadatas, skipped


def rebuild_collection(
    out_dir: Path,
    ids: list[str],
    embeddings: list[list[float]],
    documents: list[str],
    metadatas: list[dict[str, Any] | None],
    dim: int,
) -> int:
    """Create a fresh 'chunks' collection in ``out_dir`` from explicit vectors.

    Passing ``embeddings`` explicitly + a never-call embedding function guarantees
    ZERO embedding-API calls. Returns the resulting collection count.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(out_dir), settings=Settings(anonymized_telemetry=False))
    try:
        client.delete_collection("chunks")
    except Exception:  # noqa: BLE001 - collection may not exist yet
        pass
    collection = client.create_collection(
        name="chunks",
        metadata={"hnsw:space": "cosine", "embedding_dimensions": dim},
        embedding_function=_NoEmbeddingFunction(),  # type: ignore[arg-type]
    )
    for start in range(0, len(ids), _ADD_BATCH):
        end = start + _ADD_BATCH
        collection.add(
            ids=ids[start:end],
            embeddings=embeddings[start:end],  # type: ignore[arg-type]
            documents=documents[start:end],
            metadatas=metadatas[start:end],  # type: ignore[arg-type]
        )
    return collection.count()


def _merge_live_only(
    db_path: Path,
    out_dir: Path,
    recovered_ids: set[str],
) -> int:
    """Best-effort merge of chunks present only in the live (current) index.

    Reads the current index at ``db_path`` and copies any chunk whose id is not in
    the recovered set into ``out_dir`` — so papers added after the crash are not
    lost. Never raises: a missing/unreadable live index simply contributes nothing.
    """
    if not (db_path / "chroma.sqlite3").exists():
        return 0
    try:
        live_client = chromadb.PersistentClient(path=str(db_path), settings=Settings(anonymized_telemetry=False))
        live = live_client.get_collection("chunks")
        existing = live.get(include=["documents", "metadatas", "embeddings"])
    except Exception as exc:  # noqa: BLE001 - live index is best-effort only
        logger.warning("recover-index: live-merge skipped (%s)", exc)
        return 0

    live_ids = existing.get("ids") or []
    fresh = [i for i, cid in enumerate(live_ids) if cid not in recovered_ids]
    if not fresh:
        return 0

    embeddings = existing.get("embeddings")
    documents = existing.get("documents")
    metadatas = existing.get("metadatas")
    if embeddings is None:
        return 0

    out_client = chromadb.PersistentClient(path=str(out_dir), settings=Settings(anonymized_telemetry=False))
    collection = out_client.get_collection("chunks")
    add_ids = [live_ids[i] for i in fresh]
    add_embs = [list(embeddings[i]) for i in fresh]
    add_docs = [(documents[i] if documents else "") for i in fresh]
    add_metas = [((metadatas[i] if metadatas else None) or None) for i in fresh]
    for start in range(0, len(add_ids), _ADD_BATCH):
        end = start + _ADD_BATCH
        collection.add(
            ids=add_ids[start:end],
            embeddings=add_embs[start:end],  # type: ignore[arg-type]
            documents=add_docs[start:end],
            metadatas=add_metas[start:end],  # type: ignore[arg-type]
        )
    return len(add_ids)


# ---------------------------------------------------------------------------
# Verification gate
# ---------------------------------------------------------------------------


def verify_recovery(
    out_dir: Path,
    expected_count: int,
    dim: int,
    sample_id: str | None,
    sample_vector: list[float] | None,
) -> None:
    """Assert a rebuilt index is faithful before it is swapped in.

    Checks (any failure raises ``RecoveryVerificationError``):
      * the read-only probe opens the new dir safely;
      * collection count == ``expected_count``;
      * stored ``embedding_dimensions`` == configured ``dim``;
      * round-trip self-NN: querying a recovered vector returns its own id first;
      * ``get_indexed_doc_ids`` round-trips without error.
    """
    from .vector_store import _probe_chroma_db_access

    if not _probe_chroma_db_access(out_dir):
        raise RecoveryVerificationError("recovered index failed the read-only open probe")

    client = chromadb.PersistentClient(path=str(out_dir), settings=Settings(anonymized_telemetry=False))
    collection = client.get_collection("chunks")

    count = collection.count()
    if count != expected_count:
        raise RecoveryVerificationError(
            f"count mismatch: recovered index has {count} chunks, expected {expected_count}"
        )

    stored_dim = (collection.metadata or {}).get("embedding_dimensions")
    if stored_dim is not None and stored_dim != dim:
        raise RecoveryVerificationError(f"dimension mismatch: recovered index reports {stored_dim}, configured {dim}")

    if sample_id is not None and sample_vector is not None:
        result = collection.query(query_embeddings=[sample_vector], n_results=1)  # type: ignore[arg-type]
        ids = result.get("ids") or [[]]
        top = ids[0][0] if ids and ids[0] else None
        if top != sample_id:
            raise RecoveryVerificationError(f"round-trip self-NN failed: nearest to {sample_id} was {top}")


# ---------------------------------------------------------------------------
# Re-embed fallback (paid path)
# ---------------------------------------------------------------------------


def _reembed_records(
    records: list[_ChunkRecord],
    embedder: EmbedderProtocol,
) -> tuple[list[str], list[list[float]], list[str], list[dict[str, Any] | None]]:
    """Re-embed stored chunk text via the configured embedder (costs API calls)."""
    usable = [r for r in records if r.document]
    texts = [r.document for r in usable]
    embeddings = embedder.embed(texts, task_type="RETRIEVAL_DOCUMENT") if texts else []
    ids = [r.chroma_id for r in usable]
    documents = [r.document for r in usable]
    metadatas: list[dict[str, Any] | None] = [(r.metadata or None) for r in usable]
    return ids, list(embeddings), documents, metadatas


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _swap_into_place(db_path: Path, out_dir: Path, report: RecoveryReport) -> None:
    """Move the verified ``out_dir`` to ``db_path``, preserving the old dir aside."""
    if db_path.exists():
        aside = db_path.with_name(f"{db_path.name}.pre-rescue-{time.time_ns()}")
        while aside.exists():
            aside = db_path.with_name(f"{db_path.name}.pre-rescue-{time.time_ns()}")
        shutil.move(str(db_path), str(aside))
        report.swapped_aside = aside
        report.log(f"moved previous index aside to {aside}")
    shutil.move(str(out_dir), str(db_path))
    report.swapped = True
    report.out_dir = db_path
    report.log(f"swapped recovered index into {db_path}")


def recover_index(
    db_path: Path,
    dim: int,
    *,
    source: Path | None = None,
    dry_run: bool = False,
    embedder: EmbedderProtocol | None = None,
    allow_reembed: bool = False,
    confirm: Callable[[RecoveryReport], bool] | None = None,
    merge_live: bool = True,
) -> RecoveryReport:
    """Rebuild a Chroma index from an intact SQLite + HNSW backup.

    Default path is zero-cost (vectors read from HNSW, supplied explicitly). When
    the HNSW segment is unreadable/missing and ``allow_reembed`` is set, falls back
    to re-embedding stored text via ``embedder`` (paid; gated by ``confirm``).

    Writes to a NEW directory and swaps only after the verification gate passes; on
    any failure the new dir is left aside and the original is untouched.
    """
    resolved_source = resolve_source(db_path, source)
    report = RecoveryReport(source=resolved_source, dry_run=dry_run)
    report.log(f"source: {resolved_source}")

    sqlite_path = resolved_source / "chroma.sqlite3"
    records = load_sqlite_records(sqlite_path)
    report.log(f"reconstructed {len(records)} chunk rows from SQLite")

    seg_dir = find_vector_segment_dir(resolved_source)
    vectors: dict[str, list[float]] = {}
    use_reembed = False
    if seg_dir is not None:
        try:
            vectors = load_hnsw_vectors(seg_dir, dim)
            report.log(f"loaded {len(vectors)} vectors from HNSW segment {seg_dir.name} (zero-cost)")
        except HnswlibUnavailableError as exc:
            report.log(str(exc))
            use_reembed = True
        except Exception as exc:  # noqa: BLE001 - unreadable HNSW falls back to re-embed
            report.log(f"HNSW segment unreadable ({exc}); re-embed fallback required")
            use_reembed = True
    else:
        report.log("no HNSW vector segment found; re-embed fallback required")
        use_reembed = True

    if use_reembed:
        report.method = "reembed"
        if not allow_reembed or embedder is None:
            raise HnswlibUnavailableError(
                f"Vectors cannot be read without chroma-hnswlib. Install it ({INSTALL_HINT}) "
                f"to recover for free, or re-run with the re-embed fallback enabled to rebuild "
                f"by re-embedding {sum(1 for r in records if r.document)} stored chunks "
                f"(this WILL cost embedding-API calls)."
            )

    if use_reembed:
        ids, embeddings, documents, metadatas = _reembed_records(records, embedder)  # type: ignore[arg-type]
        report.log(f"re-embedding {len(ids)} stored chunks via configured embedder (PAID — embedding-API calls)")
        skipped = len(records) - len(ids)
    else:
        ids, embeddings, documents, metadatas, skipped = _assemble(records, vectors, dim)

    report.recovered_count = len(ids)
    report.skipped_no_vector = skipped
    report.doc_count = len({m.get("doc_id") for m in metadatas if m and m.get("doc_id")})
    report.log(f"assembled {len(ids)} chunks across {report.doc_count} documents (skipped {skipped} without a vector)")

    if dry_run:
        report.log("dry-run: no files written, no swap performed")
        return report

    if confirm is not None and not confirm(report):
        report.log("recovery cancelled by caller before rebuild")
        return report

    out_dir = db_path.with_name(f"{db_path.name}.recovered-{time.time_ns()}")
    added = rebuild_collection(out_dir, ids, embeddings, documents, metadatas, dim)
    report.out_dir = out_dir
    report.log(f"rebuilt {added} chunks into {out_dir}")

    if merge_live and not use_reembed:
        merged = _merge_live_only(db_path, out_dir, set(ids))
        report.merged_count = merged
        if merged:
            report.log(f"merged {merged} live-only chunks from the current index")

    expected = added + report.merged_count
    sample_id = ids[0] if ids else None
    sample_vector = embeddings[0] if embeddings else None
    try:
        verify_recovery(out_dir, expected, dim, sample_id, sample_vector)
    except RecoveryVerificationError as exc:
        report.log(f"VERIFICATION FAILED: {exc} — original index left untouched; new dir kept at {out_dir}")
        raise
    report.verified = True
    report.log("verification gate passed (count + dim + self-NN + probe)")

    _swap_into_place(db_path, out_dir, report)
    return report
