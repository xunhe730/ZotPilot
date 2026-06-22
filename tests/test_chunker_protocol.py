from zotpilot.pdf.chunker_base import ChunkerProtocol
from zotpilot.pdf.chunker import Chunker


def test_chunker_satisfies_protocol():
    c = Chunker(chunk_size=400, overlap=100)
    assert isinstance(c, ChunkerProtocol)


def test_chunker_chunks_simple_text():
    c = Chunker(chunk_size=50, overlap=10)
    chunks = c.chunk("Sentence one. Sentence two. " * 20, pages=[], sections=[])
    assert chunks and all(ch.text for ch in chunks)
