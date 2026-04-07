"""Regression tests for token-slimming contracts."""
import json
from unittest.mock import MagicMock, patch

from zotpilot.models import RetrievalResult, ZoteroItem


def _make_config():
    config = MagicMock()
    config.oversample_multiplier = 3
    config.oversample_topic_factor = 5
    config.rerank_enabled = True
    config.rerank_alpha = 0.5
    config.embedding_provider = "gemini"
    return config


def _make_result(doc_id: str, idx: int, *, with_context: bool = False) -> RetrievalResult:
    return RetrievalResult(
        chunk_id=f"{doc_id}_chunk_{idx:04d}",
        text=(f"Passage {idx} " * 12).strip(),
        score=0.9 - idx * 0.01,
        doc_id=doc_id,
        doc_title=f"Paper {doc_id}",
        authors=(
            "Smith, J.; Doe, A.; Johnson, B.; Williams, C.; Brown, D.; "
            "Taylor, E.; Anderson, F.; Thomas, G."
        ),
        year=2024,
        page_num=idx + 1,
        chunk_index=idx,
        citation_key=f"smith_doe_johnson_williams_brown_2024_{idx}",
        publication=(
            "Nature Biotechnology Special Issue on Long Context Retrieval and "
            "Token Efficiency for Research Agents"
        ),
        section="results",
        section_confidence=0.91,
        journal_quartile="Q1",
        tags=(
            "ml;rag;retrieval;semantic-search;literature-review;benchmark;"
            "token-slimming;agent-workflows;passage-ranking;context-expansion"
        ),
        collections=(
            "AI;Long Context;Benchmarks;Token Slimming;Research Agents;"
            "Semantic Search;Passage Retrieval"
        ),
        composite_score=0.8 - idx * 0.01,
        context_before=["Before context"] if with_context else [],
        context_after=["After context"] if with_context else [],
    )


def _make_item(idx: int) -> ZoteroItem:
    return ZoteroItem(
        item_key=f"KEY{idx}",
        title=f"Paper {idx}",
        authors="Auth",
        year=2020 + idx,
        pdf_path=None,
        citation_key=f"auth{idx}",
        publication="Journal",
        doi=f"10.1000/{idx}",
        tags="ml",
        collections="AI",
    )


class TestSearchContracts:
    def test_search_papers_minimal_vs_full_contract(self):
        from zotpilot.tools.search import search_papers

        retriever = MagicMock()
        reranker = MagicMock()
        config = _make_config()
        results = [_make_result(f"DOC{i}", i) for i in range(10)]

        def search_side_effect(*_args, **kwargs):
            if kwargs.get("context_window", 0) > 0:
                return [_make_result("DOC0", 0, with_context=True)]
            return results

        retriever.search.side_effect = search_side_effect
        reranker.rerank.side_effect = lambda items, *args, **kwargs: items

        with (
            patch("zotpilot.tools.search._get_retriever", return_value=retriever),
            patch("zotpilot.tools.search._get_reranker", return_value=reranker),
            patch("zotpilot.tools.search._get_config", return_value=config),
        ):
            minimal = search_papers("test query", top_k=10)
            with_context_minimal = search_papers("test query", top_k=1, context_chunks=1)
            with_context_standard = search_papers("test query", top_k=1, context_chunks=1, verbosity="standard")
            full = search_papers("test query", top_k=10, verbosity="full")

        assert len(minimal) == 10
        assert minimal[0]["doc_id"] == "DOC0"
        assert "authors" not in minimal[0]
        assert "context_before" not in minimal[0]
        assert "item_key" not in minimal[0]
        assert "authors" in full[0]
        assert "tags" in full[0]
        assert retriever.search.call_args_list[0].kwargs["context_window"] == 0
        assert retriever.search.call_args_list[1].kwargs["context_window"] == 1
        assert "context_before" not in with_context_minimal[0]
        assert "context_before" in with_context_standard[0]
        assert len(json.dumps(minimal, ensure_ascii=False)) < 5000
        assert len(json.dumps(minimal, ensure_ascii=False)) < len(json.dumps(full, ensure_ascii=False)) * 0.40

    def test_search_topic_minimal_returns_pointers_not_passage_text(self):
        from zotpilot.tools.search import search_topic

        retriever = MagicMock()
        reranker = MagicMock()
        config = _make_config()
        results = [_make_result(f"DOC{i}", i) for i in range(10)]
        retriever.search.return_value = results
        reranker.rerank.return_value = results

        with (
            patch("zotpilot.tools.search._get_retriever", return_value=retriever),
            patch("zotpilot.tools.search._get_reranker", return_value=reranker),
            patch("zotpilot.tools.search._get_config", return_value=config),
        ):
            minimal = search_topic("topic", num_papers=10)
            full = search_topic("topic", num_papers=10, verbosity="full")

        assert len(minimal) == 10
        assert "doc_id" in minimal[0]
        assert "doc_title" in minimal[0]
        assert "year" in minimal[0]
        assert "best_passage" not in minimal[0]
        assert "best_passage_chunk_index" in minimal[0]
        assert "best_passage_context" not in minimal[0]
        assert "authors" not in minimal[0]
        assert "best_passage" in full[0]
        assert "authors" in full[0]
        assert "tags" in full[0]
        assert retriever.search.call_args.kwargs["context_window"] == 0
        assert retriever.search.call_args.kwargs["top_k"] == 100
        assert len(json.dumps(minimal, ensure_ascii=False)) < 4000

    def test_search_boolean_doc_id_and_metadata_gating(self):
        from zotpilot.tools.search import search_boolean

        zotero = MagicMock()
        zotero.search_fulltext.return_value = {"KEY1"}
        zotero.get_all_items_with_pdfs.return_value = [_make_item(1)]

        with patch("zotpilot.tools.search._get_zotero", return_value=zotero):
            minimal = search_boolean("exact terms")
            full = search_boolean("exact terms", verbosity="full")

        assert minimal[0]["doc_id"] == "KEY1"
        assert minimal[0]["authors"] == "Auth"
        assert "item_key" not in minimal[0]
        assert "citation_key" not in minimal[0]
        assert full[0]["authors"] == "Auth"
        assert "doi" in full[0]
        assert "tags" in full[0]

    def test_search_tables_and_figures_keep_content_fields(self):
        from zotpilot.tools.search import search_figures, search_tables

        config = _make_config()
        store = MagicMock()
        reranker = MagicMock()

        table_chunk = MagicMock()
        table_chunk.id = "T1"
        table_chunk.text = "| a | b |"
        table_chunk.score = 0.9
        table_chunk.metadata = {
            "doc_id": "DOC1",
            "doc_title": "Paper 1",
            "authors": "Auth",
            "year": 2024,
            "page_num": 2,
            "chunk_index": 0,
            "citation_key": "auth2024",
            "publication": "Nature",
            "section": "table",
            "section_confidence": 1.0,
            "journal_quartile": "Q1",
            "table_index": 1,
            "table_caption": "Table 1",
            "table_num_rows": 3,
            "table_num_cols": 4,
        }
        table_rr = _make_result("DOC1", 0)
        table_rr = RetrievalResult(**{**table_rr.__dict__, "chunk_id": "T1", "text": "| a | b |"})

        figure_chunk = MagicMock()
        figure_chunk.metadata = {
            "doc_id": "DOC2",
            "doc_title": "Paper 2",
            "authors": "Auth",
            "year": 2023,
            "citation_key": "auth2023",
            "publication": "Cell",
            "page_num": 7,
            "figure_index": 2,
            "caption": "Figure caption",
            "image_path": "/tmp/fig.png",
        }
        figure_chunk.score = 0.88

        store.search.side_effect = [[table_chunk], [table_chunk], [figure_chunk]]
        reranker.rerank.return_value = [table_rr]

        with (
            patch("zotpilot.tools.search._get_store", return_value=store),
            patch("zotpilot.tools.search._get_reranker", return_value=reranker),
            patch("zotpilot.tools.search._get_config", return_value=config),
        ):
            minimal_table = search_tables("tables")
            full_table = search_tables("tables", verbosity="full")
            minimal_figure = search_figures("figures")

        assert minimal_table[0]["doc_id"] == "DOC1"
        assert minimal_table[0]["doc_title"] == "Paper DOC1"
        assert minimal_table[0]["year"] == 2024
        assert minimal_table[0]["table_markdown"] == "| a | b |"
        assert "authors" not in minimal_table[0]
        assert "authors" in full_table[0]
        assert minimal_figure[0]["doc_title"] == "Paper 2"
        assert minimal_figure[0]["year"] == 2023
        assert minimal_figure[0]["image_path"] == "/tmp/fig.png"
        assert "authors" not in minimal_figure[0]


class TestContextAndIndexingContracts:
    def test_get_passage_context_include_merged_flag(self):
        from zotpilot.tools.context import get_passage_context

        store = MagicMock()
        chunks = []
        for idx in range(5):
            chunk = MagicMock()
            chunk.text = f"text {idx}"
            chunk.metadata = {
                "chunk_index": idx,
                "page_num": idx + 1,
                "section": "results",
                "section_confidence": 0.9,
                "doc_title": "Paper",
                "citation_key": "auth2024",
            }
            chunks.append(chunk)
        store.get_adjacent_chunks.return_value = chunks
        config = _make_config()

        with (
            patch("zotpilot.tools.context._get_config", return_value=config),
            patch("zotpilot.tools.context._get_store", return_value=store),
        ):
            compact = get_passage_context("DOC1", 2)
            merged = get_passage_context("DOC1", 2, include_merged=True)

        assert "merged_text" not in compact
        assert "text" in compact["passages"][0]
        assert "merged_text" in merged
        assert "text" not in merged["passages"][0]
        assert compact["passages"][0]["section"] == "results"
        assert compact["passages"][0]["section_confidence"] == 0.9
        assert len(json.dumps(compact, ensure_ascii=False)) < 2000

    def test_table_context_early_return_keeps_empty_merged_text(self):
        from zotpilot.tools.context import get_passage_context

        config = _make_config()
        store = MagicMock()
        store.collection.get.side_effect = [
            {
                "ids": ["DOC1_table_0001_01"],
                "metadatas": [{
                    "doc_title": "Paper",
                    "citation_key": "auth2024",
                    "table_caption": "Table 1",
                }],
            },
            {
                "ids": [],
                "documents": [],
                "metadatas": [],
            },
        ]

        with (
            patch("zotpilot.tools.context._get_config", return_value=config),
            patch("zotpilot.tools.context._get_store", return_value=store),
        ):
            result = get_passage_context("DOC1", 0, table_page=1, table_index=1)

        assert result["passages"] == []
        assert result["merged_text"] == ""
        assert result["note"] == "No text chunks found for this document"

    def test_get_index_stats_samples_unindexed(self):
        from zotpilot.tools.indexing import get_index_stats

        store = MagicMock()
        store.get_indexed_doc_ids.return_value = {"KEY0"}
        store.count.return_value = 120
        store.collection.get.return_value = {"metadatas": []}
        zotero = MagicMock()
        zotero.get_all_items_with_pdfs.return_value = [_make_item(i) for i in range(200)]
        config = _make_config()
        config.embedding_provider = "gemini"
        config.stats_sample_limit = 100

        with (
            patch("zotpilot.tools.indexing._get_config", return_value=config),
            patch("zotpilot.tools.indexing._get_retriever"),
            patch("zotpilot.tools.indexing._get_store", return_value=store),
            patch("zotpilot.tools.indexing._get_zotero", return_value=zotero),
        ):
            result = get_index_stats(limit=5)

        assert result["unindexed_count"] == 199
        assert len(result["sample_unindexed"]) == 5
        assert len(result["unindexed_papers"]) == 5
        assert len(json.dumps(result, ensure_ascii=False)) < 2000

    def test_index_library_summary_is_opt_in(self):
        from zotpilot.tools.indexing import index_library

        index_result = {
            "results": [],
            "indexed": 1,
            "failed": 0,
            "empty": 0,
            "skipped": 0,
            "already_indexed": 0,
            "quality_distribution": {"A": 1},
            "extraction_stats": {"total_pages": 10},
            "long_documents": [{"item_key": "KEY9"}],
            "skipped_long": 1,
            "total_to_index": 5,
            "has_more": False,
        }
        config = MagicMock()
        config.validate.return_value = []
        config.max_pages = 40
        config.vision_enabled = True

        with (
            patch("zotpilot.tools.indexing._get_config", return_value=config),
            patch("zotpilot.tools.indexing._get_store") as mock_store,
            patch("zotpilot.indexer.Indexer") as mock_indexer_cls,
            patch("dataclasses.replace", side_effect=lambda obj, **kwargs: obj),
        ):
            mock_store.return_value.clear_query_cache = MagicMock()
            mock_indexer_cls.return_value.index_all.return_value = index_result
            compact = index_library(batch_size=0)
            full = index_library(batch_size=0, include_summary=True)

        assert "quality_distribution" not in compact
        assert "quality_distribution" in full

    def test_index_library_exposes_vision_budget_summary_when_requested(self):
        from zotpilot.tools.indexing import index_library

        index_result = {
            "results": [],
            "indexed": 0,
            "failed": 0,
            "empty": 0,
            "skipped": 0,
            "already_indexed": 0,
            "quality_distribution": {},
            "extraction_stats": {},
            "long_documents": [],
            "skipped_long": 0,
            "total_to_index": 3,
            "has_more": False,
            "vision_pending_tables": 12,
            "vision_estimated_cost_usd": 0.12,
            "vision_budget_skipped": True,
            "vision_skip_reason": "table cap 5",
        }
        config = MagicMock()
        config.validate.return_value = []
        config.max_pages = 40
        config.vision_enabled = True

        with (
            patch("zotpilot.tools.indexing._get_config", return_value=config),
            patch("zotpilot.tools.indexing._get_store") as mock_store,
            patch("zotpilot.indexer.Indexer") as mock_indexer_cls,
            patch("dataclasses.replace", side_effect=lambda obj, **kwargs: obj),
        ):
            mock_store.return_value.clear_query_cache = MagicMock()
            mock_indexer_cls.return_value.index_all.return_value = index_result
            result = index_library(batch_size=0, include_summary=True)

        assert result["vision_pending_tables"] == 12
        assert result["vision_estimated_cost_usd"] == 0.12
        assert result["vision_budget_skipped"] is True
        assert result["vision_skip_reason"] == "table cap 5"

    def test_get_index_stats_paginates_unindexed_papers(self):
        from zotpilot.tools.indexing import get_index_stats

        store = MagicMock()
        store.get_indexed_doc_ids.return_value = {"KEY0"}
        store.count.return_value = 10
        store.collection.get.return_value = {"metadatas": []}
        zotero = MagicMock()
        zotero.get_all_items_with_pdfs.return_value = [_make_item(i) for i in range(6)]
        config = _make_config()
        config.stats_sample_limit = 10

        with (
            patch("zotpilot.tools.indexing._get_config", return_value=config),
            patch("zotpilot.tools.indexing._get_retriever"),
            patch("zotpilot.tools.indexing._get_store", return_value=store),
            patch("zotpilot.tools.indexing._get_zotero", return_value=zotero),
        ):
            result = get_index_stats(limit=2, offset=1)

        assert result["unindexed_count"] == 5
        assert result["offset"] == 1
        assert result["limit"] == 2
        assert len(result["unindexed_papers"]) == 2
        assert result["unindexed_papers"][0]["doc_id"] == "KEY2"


class TestLibraryIdentifierContracts:
    @patch("zotpilot.tools.library._get_store_optional")
    @patch("zotpilot.tools.library._get_zotero")
    def test_get_library_overview_uses_doc_id(self, mock_get_zotero, mock_store_opt):
        from zotpilot.tools.library import browse_library

        item = _make_item(1)
        mock_client = MagicMock()
        mock_client.get_all_items_with_pdfs.return_value = [item]
        mock_get_zotero.return_value = mock_client

        mock_store = MagicMock()
        mock_store.get_indexed_doc_ids.return_value = {"KEY1"}
        mock_store_opt.return_value = mock_store

        minimal = browse_library(view="overview", limit=1)
        full = browse_library(view="overview", limit=1, verbosity="full")

        assert minimal["papers"][0]["doc_id"] == "KEY1"
        assert "key" not in minimal["papers"][0]
        assert "authors" not in minimal["papers"][0]
        assert "authors" in full["papers"][0]
        assert "citation_key" in full["papers"][0]

class TestIngestionPreflightContracts:
    def test_preflight_blocked_returns_failed_results(self):
        """Preflight blocked URLs appear as failed in simplified result format."""
        from zotpilot.tools.ingestion import ingest_papers

        report = {
            "checked": 2,
            "accessible": [{"url": "https://arxiv.org/abs/2401.0001"}],
            "blocked": [{"url": "https://arxiv.org/abs/2401.0002", "error": "anti-bot"}],
            "skipped": [],
            "errors": [],
            "all_clear": False,
        }

        mock_config = type("C", (), {"preflight_enabled": True, "zotero_api_key": None})()
        with (
            patch("zotpilot.tools.ingestion._get_config", return_value=mock_config),
            patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True),
            patch("zotpilot.tools.ingestion.ingestion_bridge.get_extension_status", return_value={"extension_connected": True}),
            patch("zotpilot.tools.ingestion.ingestion_bridge.preflight_urls", return_value=report),
            patch("zotpilot.tools.ingestion._lookup_local_item_key_by_doi", return_value=None),
        ):
            result = ingest_papers([
                {"arxiv_id": "2401.0001"},
                {"arxiv_id": "2401.0002"},
            ])

        assert result["failed"] >= 1
        failed_results = [r for r in result["results"] if r["status"] == "failed"]
        assert any("anti-bot" in r.get("error", "") for r in failed_results)
