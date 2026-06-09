import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from zotpilot.config import index_journal_path, index_progress_path
from zotpilot.index_progress import JsonlProgressSink


class CollectingProgressSink:
    def __init__(self):
        self.events = []

    def emit(self, event_type: str, **payload: object) -> None:
        self.events.append({"event": event_type, **payload})


class FailingProgressSink:
    def emit(self, event_type: str, **payload: object) -> None:
        raise OSError("progress path unavailable")


def _make_config(tmp_path: Path):
    config = MagicMock()
    config.zotero_data_dir = tmp_path / "zotero"
    config.chroma_db_path = tmp_path / "chroma"
    config.chroma_db_path.mkdir()
    config.chunk_size = 1000
    config.chunk_overlap = 200
    config.embedding_provider = "local"
    config.embedding_dimensions = 384
    config.embedding_model = "test"
    config.ocr_language = "eng"
    config.vision_enabled = False
    config.anthropic_api_key = None
    config.max_pages = 0
    config.vision_max_tables_per_run = None
    config.vision_max_cost_usd = None
    config.oversample_multiplier = 2
    return config


def _make_item(key: str, title: str | None = None):
    item = MagicMock()
    item.item_key = key
    item.title = title or f"Paper {key}"
    pdf = MagicMock()
    pdf.exists.return_value = True
    pdf.__str__ = lambda self: f"/private/{key}.pdf"
    item.pdf_path = pdf
    return item


def _make_indexer(tmp_path: Path, items):
    from zotpilot.indexer import Indexer

    config = _make_config(tmp_path)
    with patch("zotpilot.indexer.ZoteroClient"), \
         patch("zotpilot.indexer.create_embedder"), \
         patch("zotpilot.indexer.VectorStore"), \
         patch("zotpilot.indexer.JournalRanker"):
        indexer = Indexer(config)
    indexer.zotero.get_all_items_with_pdfs.return_value = items
    indexer.store.get_indexed_doc_ids = MagicMock(return_value=set())
    indexer._load_empty_docs = MagicMock(return_value={})
    indexer._save_empty_docs = MagicMock()
    indexer._pdf_hash = MagicMock(return_value="hash")
    indexer._config_hash_path = MagicMock()
    indexer._config_hash_path.exists.return_value = False
    indexer._config_hash_path.write_text = MagicMock()
    indexer._library_unreachable = MagicMock(return_value=False)
    indexer._sleep = MagicMock()
    return indexer


def _extraction():
    extraction = MagicMock()
    extraction.pages = [MagicMock()]
    extraction.stats = {"total_pages": 1, "text_pages": 1, "ocr_pages": 0, "empty_pages": 0}
    extraction.quality_grade = "A"
    extraction.pending_vision = None
    return extraction


def _success(item, extraction, journal):
    return (5, 0, "", {"total_pages": 1, "text_pages": 1, "ocr_pages": 0, "empty_pages": 0}, "A")


def test_jsonl_progress_sink_appends_utf8_events(tmp_path):
    path = tmp_path / "index_progress.jsonl"
    sink = JsonlProgressSink(path, clock=lambda: 123.5)

    sink.emit("run_started", title="中文标题")
    sink.emit("run_finished", indexed=1)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["schema_version"] == 1
    assert first["event"] == "run_started"
    assert first["timestamp"] == 123.5
    assert first["title"] == "中文标题"
    assert second["event"] == "run_finished"
    assert second["indexed"] == 1


def test_progress_path_is_separate_from_journal(tmp_path):
    config = SimpleNamespace(chroma_db_path=tmp_path / "chroma")

    assert index_journal_path(config).name == "index_journal.json"
    assert index_progress_path(config).name == "index_progress.jsonl"
    assert index_journal_path(config).parent == index_progress_path(config).parent
    assert index_journal_path(config) != index_progress_path(config)


def test_index_cli_help_exposes_progress_jsonl(capsys):
    from zotpilot.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["index", "--help"])

    assert exc.value.code == 0
    assert "--progress-jsonl" in capsys.readouterr().out


def test_progress_counts_keeps_allowlisted_summary_fields():
    from zotpilot.indexer import _progress_counts

    counts = {
        "indexed": 2,
        "failed": 1,
        "total_to_index": 4,
        "rate_limited_abort": False,
        "quality_distribution": {"A": 2},
        "extraction_stats": {"total_pages": 10},
        "skipped_no_pdf": [{"item_key": "NO1"}, {"item_key": "NO2"}],
        "long_documents": [{"item_key": "LONG1", "pages": 200}],
        "results": ["not-jsonl-summary"],
    }

    assert _progress_counts(counts) == {
        "indexed": 2,
        "failed": 1,
        "total_to_index": 4,
        "rate_limited_abort": False,
        "quality_distribution": {"A": 2},
        "extraction_stats": {"total_pages": 10},
        "skipped_no_pdf_count": 2,
    }


def test_index_all_emits_structured_progress_without_pdf_paths(tmp_path):
    item = _make_item("K1", "Readable Paper")
    indexer = _make_indexer(tmp_path, [item])
    sink = CollectingProgressSink()

    with patch("zotpilot.indexer.extract_document", return_value=_extraction()), \
         patch.object(indexer, "_index_extraction", side_effect=_success):
        result = indexer.index_all(batch_size=None, progress_sink=sink)

    assert result["indexed"] == 1
    event_names = [event["event"] for event in sink.events]
    assert event_names[0] == "run_started"
    assert "plan_ready" in event_names
    indexed_events = [
        event for event in sink.events
        if event["event"] == "paper_finished"
        and event.get("phase") == "indexing"
        and event.get("status") == "indexed"
    ]
    assert indexed_events
    assert sink.events[-1]["event"] == "run_finished"
    assert sink.events[-1]["indexed"] == 1
    assert "/private/K1.pdf" not in json.dumps(sink.events, ensure_ascii=False)


def test_progress_sink_failure_does_not_fail_indexing(tmp_path):
    item = _make_item("K1")
    indexer = _make_indexer(tmp_path, [item])

    with patch("zotpilot.indexer.extract_document", return_value=_extraction()), \
         patch.object(indexer, "_index_extraction", side_effect=_success):
        result = indexer.index_all(batch_size=None, progress_sink=FailingProgressSink())

    assert result["indexed"] == 1


def test_rate_limit_abort_emits_abort_and_unattempted_tail(tmp_path):
    from zotpilot.embeddings.base import RateLimitError

    items = [_make_item("K1"), _make_item("K2"), _make_item("K3")]
    indexer = _make_indexer(tmp_path, items)
    sink = CollectingProgressSink()

    def side_effect(item, extraction, journal):
        if item.item_key == "K2":
            raise RateLimitError("quota", provider="gemini", retry_after=30.0)
        return _success(item, extraction, journal)

    with patch("zotpilot.indexer.extract_document", return_value=_extraction()), \
         patch.object(indexer, "_index_extraction", side_effect=side_effect):
        result = indexer.index_all(batch_size=None, progress_sink=sink)

    assert result["rate_limited_abort"] is True
    abort_events = [event for event in sink.events if event["event"] == "run_aborted"]
    assert abort_events == [
        {
            "event": "run_aborted",
            "run_id": abort_events[0]["run_id"],
            "phase": "indexing",
            "cause": "rate_limit",
            "abort_index": 2,
            "total": 3,
            "not_indexed_due_to_abort": 2,
        }
    ]
    tail_events = [
        event for event in sink.events
        if event["event"] == "paper_finished" and event.get("item_key") == "K3"
    ]
    assert tail_events[-1]["status"] == "failed"
    assert "AbortNotAttempted" in tail_events[-1]["reason"]
