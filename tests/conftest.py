"""Shared test fixtures for ZotPilot tests."""
# Isolate research-session persistence BEFORE any zotpilot module imports.
# Without this, tests share ``~/.local/share/zotpilot/sessions`` with the
# user's real MCP server state: any in-flight research session triggers
# Gate 2 and causes write-operation tests (create_note, manage_tags,
# manage_collections) to fail.  Point the session store at an ephemeral
# temp dir so the test run stays hermetic regardless of host state.
import os
import tempfile

os.environ["ZOTPILOT_SESSIONS_DIR"] = tempfile.mkdtemp(prefix="zotpilot-test-sessions-")

from unittest.mock import MagicMock

import pytest

from zotpilot.models import (
    Chunk,
    PageExtraction,
    RetrievalResult,
    SectionSpan,
    ZoteroItem,
)


def pytest_addoption(parser):
    parser.addoption(
        "--benchmark",
        action="store_true",
        default=False,
        help="run external benchmark tests",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--benchmark"):
        return
    skip_benchmark = pytest.mark.skip(reason="need --benchmark to run")
    for item in items:
        if "benchmark" in item.keywords:
            item.add_marker(skip_benchmark)


@pytest.fixture
def sample_chunks():
    """Create sample chunks for testing."""
    return [
        Chunk(
            text="This is the introduction to our study on neural networks.",
            chunk_index=0,
            page_num=1,
            char_start=0,
            char_end=56,
            section="introduction",
            section_confidence=1.0,
        ),
        Chunk(
            text="We used a transformer architecture with 12 attention heads.",
            chunk_index=1,
            page_num=2,
            char_start=56,
            char_end=114,
            section="methods",
            section_confidence=0.85,
        ),
        Chunk(
            text="Our results show a 15% improvement over the baseline.",
            chunk_index=2,
            page_num=3,
            char_start=114,
            char_end=167,
            section="results",
            section_confidence=1.0,
        ),
    ]


@pytest.fixture
def sample_pages():
    """Create sample page extractions."""
    return [
        PageExtraction(page_num=1, markdown="Introduction text...", char_start=0),
        PageExtraction(page_num=2, markdown="Methods text...", char_start=56),
        PageExtraction(page_num=3, markdown="Results text...", char_start=114),
    ]


@pytest.fixture
def sample_sections():
    """Create sample section spans."""
    return [
        SectionSpan(label="introduction", char_start=0, char_end=56, heading_text="Introduction", confidence=1.0),
        SectionSpan(label="methods", char_start=56, char_end=114, heading_text="Methods", confidence=0.85),
        SectionSpan(label="results", char_start=114, char_end=167, heading_text="Results", confidence=1.0),
    ]


@pytest.fixture
def sample_retrieval_result():
    """Create a sample retrieval result."""
    return RetrievalResult(
        chunk_id="TEST123_chunk_0001",
        text="Transformer models achieve state-of-the-art results.",
        score=0.85,
        doc_id="TEST123",
        doc_title="Attention Is All You Need",
        authors="Vaswani et al.",
        year=2017,
        page_num=5,
        chunk_index=1,
        citation_key="vaswani2017",
        publication="NeurIPS",
        section="results",
        section_confidence=1.0,
        journal_quartile="Q1",
    )


@pytest.fixture
def sample_zotero_item():
    """Create a sample Zotero item."""
    return ZoteroItem(
        item_key="TEST123",
        title="Attention Is All You Need",
        authors="Vaswani et al.",
        year=2017,
        pdf_path=None,
        citation_key="vaswani2017",
        publication="NeurIPS",
        doi="10.1234/test",
        tags="deep-learning; transformers",
        collections="Machine Learning",
    )


@pytest.fixture
def mock_embedder():
    """Create a mock embedder."""
    embedder = MagicMock()
    embedder.dimensions = 768
    embedder.embed.side_effect = lambda texts, **kwargs: [[0.1] * 768 for _ in texts]
    embedder.embed_query.return_value = [0.1] * 768
    return embedder
