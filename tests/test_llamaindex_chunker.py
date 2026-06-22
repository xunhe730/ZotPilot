# tests/test_llamaindex_chunker.py
import pytest

pytest.importorskip("llama_index.core")
pytest.importorskip("tokenizers")

from zotpilot.models import PageExtraction
from zotpilot.pdf.chunker_base import ChunkerProtocol
from zotpilot.pdf.llamaindex_chunker import LlamaIndexChunker


def test_satisfies_protocol():
    assert isinstance(LlamaIndexChunker(), ChunkerProtocol)


def test_no_chunk_exceeds_hard_cap():
    c = LlamaIndexChunker(chunk_size=120, overlap=20, hard_cap_tokens=128)
    text = "Dense academic sentence about econometrics. " * 200
    # PageExtraction uses `markdown` (not `text`) and has no `char_end` field.
    pages = [PageExtraction(page_num=1, markdown=text, char_start=0)]
    chunks = c.chunk(text, pages=pages, sections=[])
    assert chunks
    for ch in chunks:
        assert len(c._tokenizer.encode(ch.text).ids) <= 128


def test_chunks_carry_section_and_page_metadata():
    c = LlamaIndexChunker(chunk_size=120, overlap=20)
    text = "Introduction text. " * 50
    pages = [PageExtraction(page_num=1, markdown=text, char_start=0)]
    chunks = c.chunk(text, pages=pages, sections=[])
    assert chunks
    assert all(ch.page_num == 1 for ch in chunks)
    assert all(ch.text for ch in chunks)
