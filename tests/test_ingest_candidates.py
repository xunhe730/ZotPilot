"""Tests for structured ingest candidates and ingest_by_identifiers."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

import zotpilot.tools.ingestion as ingestion_tool
from zotpilot.state import ToolError
from zotpilot.tools.ingestion.models import IngestCandidate


@pytest.fixture
def ingest_env(monkeypatch):
    """Patch external dependencies so ingest tool tests stay local and deterministic."""
    ingestion_tool._RECENT_SAVES.clear()
    ingestion_tool._PREFLIGHT_PASSES.clear()
    zotero = MagicMock()
    zotero.get_item_key_by_doi.return_value = None
    zotero.get_item_key_by_arxiv_id.return_value = None

    monkeypatch.setattr(ingestion_tool, "_ensure_inbox_collection", lambda: "INBOX")
    monkeypatch.setattr(ingestion_tool, "_get_zotero", lambda: zotero)
    monkeypatch.setattr(ingestion_tool, "_get_writer", lambda: MagicMock())
    monkeypatch.setattr(
        ingestion_tool.connector,
        "check_connector_availability",
        lambda *args, **kwargs: (False, None, None),
    )
    monkeypatch.setattr(
        ingestion_tool.connector,
        "run_preflight_check",
        lambda *args, **kwargs: ([], [], False),
    )
    monkeypatch.setattr(
        ingestion_tool.connector,
        "resolve_doi_to_landing_url",
        lambda doi: f"https://resolved.example/{doi}",
    )
    monkeypatch.setattr(
        ingestion_tool.connector,
        "_doi_api_fallback",
        lambda doi, title, **kwargs: {
            "status": "saved_metadata_only",
            "method": "api_fallback",
            "item_key": f"KEY-{doi}",
            "has_pdf": False,
            "title": title or "",
            "action_required": None,
            "warning": "Created via DOI API",
        },
    )
    return zotero


def test_preflight_blocked_items_reported_but_passed_items_still_save(ingest_env, monkeypatch):
    saved_urls: list[str] = []

    monkeypatch.setattr(
        ingestion_tool.connector,
        "check_connector_availability",
        lambda *args, **kwargs: (True, None, None),
    )

    def _run_preflight(candidates, *args, **kwargs):
        assert [c["url"] for c in candidates] == [
            "https://doi.org/10.1000/a",
            "https://doi.org/10.1000/b",
            "https://doi.org/10.1000/c",
        ]
        return (
            [c for c in candidates if c["url"] != "https://doi.org/10.1000/a"],
            [{
                "url": "https://doi.org/10.1000/a",
                "final_url": "https://www.sciencedirect.com/science/article/pii/A",
                "error_code": "anti_bot_detected",
                "error": "Anti-bot protection detected.",
            }],
            {"decision_id": "preflight_blocked"},
            [{
                "publisher": "sciencedirect.com",
                "sample_urls": ["https://www.sciencedirect.com/science/article/pii/A"],
                "error_code": "anti_bot_detected",
                "scope": "publisher",
                "total_affected": 1,
            }],
        )

    monkeypatch.setattr(ingestion_tool.connector, "run_preflight_check", _run_preflight)

    def _save_single(url, doi, title, **kwargs):
        saved_urls.append(url)
        return {
            "status": "saved_metadata_only",
            "method": "connector",
            "item_key": f"KEY-{doi}",
            "has_pdf": False,
            "title": title or "",
            "action_required": None,
            "warning": None,
        }

    monkeypatch.setattr(ingestion_tool.connector, "save_single_and_verify", _save_single)

    result = ingestion_tool.ingest_by_identifiers(
        candidates=[
            IngestCandidate(doi="10.1000/a", title="A", is_oa_published=True),
            IngestCandidate(doi="10.1000/b", title="B", is_oa_published=True),
            IngestCandidate(doi="10.1000/c", title="C", is_oa_published=True),
        ]
    )

    assert saved_urls == ["https://doi.org/10.1000/b", "https://doi.org/10.1000/c"]
    assert [row["status"] for row in result["results"]] == [
        "preflight_blocked", "saved_metadata_only", "saved_metadata_only",
    ]
    assert result["action_required"][0]["type"] == "preflight_blocked"
    assert result["action_required"][0]["blocked_count"] == 1


def test_recent_preflight_pass_skips_recheck(ingest_env, monkeypatch):
    run_preflight_calls = 0

    monkeypatch.setattr(
        ingestion_tool.connector,
        "check_connector_availability",
        lambda *args, **kwargs: (True, None, None),
    )

    def _run_preflight(candidates, *args, **kwargs):
        nonlocal run_preflight_calls
        run_preflight_calls += 1
        return (list(candidates), [], None, [])

    monkeypatch.setattr(ingestion_tool.connector, "run_preflight_check", _run_preflight)
    monkeypatch.setattr(ingestion_tool, "_remember_recent_save", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ingestion_tool.connector,
        "save_single_and_verify",
        lambda url, doi, title, **kwargs: {
            "status": "saved_metadata_only",
            "method": "connector",
            "item_key": f"KEY-{doi}",
            "has_pdf": False,
            "title": title or "",
            "action_required": None,
            "warning": None,
        },
    )

    candidate = IngestCandidate(doi="10.1000/cache", title="Cache", is_oa_published=True)
    first = ingestion_tool.ingest_by_identifiers(candidates=[candidate])
    second = ingestion_tool.ingest_by_identifiers(candidates=[candidate])

    assert first["results"][0]["status"] == "saved_metadata_only"
    assert second["results"][0]["status"] == "saved_metadata_only"
    assert run_preflight_calls == 1


def test_save_stage_anti_bot_does_not_halt_remaining_items(ingest_env, monkeypatch):
    saved_urls: list[str] = []

    monkeypatch.setattr(
        ingestion_tool.connector,
        "check_connector_availability",
        lambda *args, **kwargs: (True, None, None),
    )
    monkeypatch.setattr(
        ingestion_tool.connector,
        "run_preflight_check",
        lambda candidates, *args, **kwargs: (list(candidates), [], None, []),
    )

    def _save_single(url, doi, title, **kwargs):
        saved_urls.append(url)
        if doi == "10.1000/b":
            return {
                "status": "blocked",
                "method": "connector",
                "item_key": None,
                "has_pdf": False,
                "title": title or "",
                "action_required": "需要用户在浏览器中完成验证，然后重试",
                "warning": None,
            }
        return {
            "status": "saved_metadata_only",
            "method": "connector",
            "item_key": f"KEY-{doi}",
            "has_pdf": False,
            "title": title or "",
            "action_required": None,
            "warning": None,
        }

    monkeypatch.setattr(ingestion_tool.connector, "save_single_and_verify", _save_single)

    result = ingestion_tool.ingest_by_identifiers(
        candidates=[
            IngestCandidate(doi="10.1000/a", title="A", is_oa_published=True),
            IngestCandidate(doi="10.1000/b", title="B", is_oa_published=True),
            IngestCandidate(doi="10.1000/c", title="C", is_oa_published=True),
        ]
    )

    assert saved_urls == [
        "https://doi.org/10.1000/a",
        "https://doi.org/10.1000/b",
        "https://doi.org/10.1000/c",
    ]
    assert [row["status"] for row in result["results"]] == [
        "saved_metadata_only", "blocked", "saved_metadata_only",
    ]
    assert result["action_required"] == [{
        "type": "anti_bot_detected",
        "message": "需要用户在浏览器中完成验证，然后重试",
        "identifier": "10.1000/b",
    }]


def test_manual_completion_required_returns_retry_payload(ingest_env, monkeypatch):
    monkeypatch.setattr(
        ingestion_tool.connector,
        "check_connector_availability",
        lambda *args, **kwargs: (True, None, None),
    )
    monkeypatch.setattr(
        ingestion_tool.connector,
        "run_preflight_check",
        lambda candidates, *args, **kwargs: (list(candidates), [], None, []),
    )

    def _save_single(url, doi, title, **kwargs):
        return {
            "status": "__manual_completion_required__",
            "method": "connector",
            "item_key": "KEY-EXISTING",
            "has_pdf": False,
            "title": title or "",
            "resume_action": "reconcile_existing",
            "timeout_stage": "save_confirmation",
            "action_required": None,
            "warning": None,
        }

    monkeypatch.setattr(ingestion_tool.connector, "save_single_and_verify", _save_single)

    result = ingestion_tool.ingest_by_identifiers(
        candidates=[
            IngestCandidate(
                doi="10.1016/a",
                title="A",
                publisher="Elsevier BV",
                needs_manual_verification=True,
                is_oa_published=False,
            ),
            IngestCandidate(
                doi="10.1000/b",
                title="B",
                is_oa_published=True,
            ),
        ]
    )

    assert result["results"] == []
    assert result["completed_count"] == 0
    assert result["action_required"][0]["type"] == "manual_completion_required"
    payload = result["action_required"][0]["retry_payload"]
    assert [row["candidate_index"] for row in payload] == [0, 1]
    assert payload[0]["resume_action"] == "reconcile_existing"
    assert payload[0]["existing_item_key"] == "KEY-EXISTING"


def test_manual_verification_candidates_continue_in_same_call_when_no_manual_stop(ingest_env, monkeypatch):
    monkeypatch.setattr(
        ingestion_tool.connector,
        "check_connector_availability",
        lambda *args, **kwargs: (True, None, None),
    )
    monkeypatch.setattr(
        ingestion_tool.connector,
        "run_preflight_check",
        lambda candidates, *args, **kwargs: (list(candidates), [], None, []),
    )
    seen = []

    def _save_single(url, doi, title, **kwargs):
        seen.append(doi)
        return {
            "status": "saved_metadata_only",
            "method": "connector",
            "item_key": f"KEY-{doi}",
            "has_pdf": False,
            "title": title or "",
            "action_required": None,
            "warning": None,
        }

    monkeypatch.setattr(ingestion_tool.connector, "save_single_and_verify", _save_single)

    result = ingestion_tool.ingest_by_identifiers(
        candidates=[
            IngestCandidate(doi="10.1016/a", title="A", publisher="Elsevier BV", is_oa_published=False),
            IngestCandidate(doi="10.1016/b", title="B", publisher="Elsevier BV", is_oa_published=False),
            IngestCandidate(doi="10.1000/c", title="C", is_oa_published=True),
        ]
    )

    assert seen == ["10.1016/a", "10.1016/b", "10.1000/c"]
    assert result["action_required"] == []
    assert [row["status"] for row in result["results"]] == [
        "saved_metadata_only", "saved_metadata_only", "saved_metadata_only",
    ]


def test_manual_completion_stops_only_after_actual_manual_block(ingest_env, monkeypatch):
    monkeypatch.setattr(
        ingestion_tool.connector,
        "check_connector_availability",
        lambda *args, **kwargs: (True, None, None),
    )
    monkeypatch.setattr(
        ingestion_tool.connector,
        "run_preflight_check",
        lambda candidates, *args, **kwargs: (list(candidates), [], None, []),
    )
    seen = []

    def _save_single(url, doi, title, **kwargs):
        seen.append(doi)
        if doi == "10.1016/b":
            return {
                "status": "__manual_completion_required__",
                "method": "connector",
                "item_key": "KEY-B",
                "has_pdf": False,
                "title": title or "",
                "resume_action": "reconcile_existing",
                "timeout_stage": "manual_completion",
                "action_required": None,
                "warning": None,
            }
        return {
            "status": "saved_metadata_only",
            "method": "connector",
            "item_key": f"KEY-{doi}",
            "has_pdf": False,
            "title": title or "",
            "action_required": None,
            "warning": None,
        }

    monkeypatch.setattr(ingestion_tool.connector, "save_single_and_verify", _save_single)

    result = ingestion_tool.ingest_by_identifiers(
        candidates=[
            IngestCandidate(doi="10.1016/a", title="A", publisher="Elsevier BV", is_oa_published=False),
            IngestCandidate(doi="10.1016/b", title="B", publisher="Elsevier BV", is_oa_published=False),
            IngestCandidate(doi="10.1000/c", title="C", is_oa_published=True),
        ]
    )

    assert seen == ["10.1016/a", "10.1016/b"]
    assert [row["status"] for row in result["results"]] == ["saved_metadata_only"]
    assert result["action_required"][0]["type"] == "manual_completion_required"
    assert [row["candidate_index"] for row in result["action_required"][0]["retry_payload"]] == [1, 2]


def test_candidate_accepts_minimal_doi():
    candidate = IngestCandidate(doi="10.1/x")
    assert candidate.doi == "10.1/x"


def test_candidate_accepts_minimal_arxiv():
    candidate = IngestCandidate(arxiv_id="2301.00001")
    assert candidate.arxiv_id == "2301.00001"


def test_candidate_extras_ignored():
    candidate = IngestCandidate.model_validate(
        {
            "doi": "10.1/x",
            "title": "Paper",
            "cited_by_count": 100,
            "authors": ["Ada"],
            "venue": {"display_name": "Nature"},
            "publisher": "Nature",
            "journal": "Nature",
            "top_venue": True,
            "local_duplicate": False,
            "existing_item_key": None,
        }
    )

    assert candidate.doi == "10.1/x"
    assert set(candidate.model_dump()) == {
        "doi",
        "arxiv_id",
        "landing_page_url",
        "oa_url",
        "is_oa_published",
        "title",
        "openalex_id",
        "publisher",
        "needs_manual_verification",
        "existing_item_key",
        "resume_action",
    }


def test_candidate_empty_is_valid_at_pydantic_layer():
    candidate = IngestCandidate()
    assert candidate.model_dump() == {
        "doi": None,
        "arxiv_id": None,
        "landing_page_url": None,
        "oa_url": None,
        "is_oa_published": False,
        "title": None,
        "openalex_id": None,
        "publisher": None,
        "needs_manual_verification": None,
        "existing_item_key": None,
        "resume_action": None,
    }


def test_save_priority_journal_when_oa_published_true():
    internal = ingestion_tool._candidates_to_internal(
        [
            IngestCandidate(
                doi="10.1234/journal",
                arxiv_id="2301.00001",
                is_oa_published=True,
            )
        ]
    )[0]

    assert internal["doi"] == "10.1234/journal"
    assert internal["url"] == "https://doi.org/10.1234/journal"


def test_save_priority_arxiv_when_oa_published_false():
    internal = ingestion_tool._candidates_to_internal(
        [
            IngestCandidate(
                doi="10.1234/journal",
                arxiv_id="2301.00001",
                is_oa_published=False,
            )
        ]
    )[0]

    assert internal["doi"] == "10.48550/arxiv.2301.00001"
    assert internal["url"] == "https://arxiv.org/abs/2301.00001"
    assert internal["source_doi"] == "10.1234/journal"


def test_save_priority_arxiv_only():
    internal = ingestion_tool._candidates_to_internal(
        [IngestCandidate(arxiv_id="2301.00001")]
    )[0]

    assert internal["doi"] == "10.48550/arxiv.2301.00001"
    assert internal["url"] == "https://arxiv.org/abs/2301.00001"


def test_save_priority_doi_only_non_oa():
    internal = ingestion_tool._candidates_to_internal(
        [IngestCandidate(doi="10.1234/journal", is_oa_published=False)]
    )[0]

    assert internal["doi"] == "10.1234/journal"
    assert internal["url"] == "https://doi.org/10.1234/journal"


def test_save_priority_landing_fallback():
    internal = ingestion_tool._candidates_to_internal(
        [IngestCandidate(landing_page_url="https://publisher.example/paper")]
    )[0]

    assert internal["doi"] is None
    assert internal["url"] == "https://publisher.example/paper"


def test_save_priority_all_empty_fails(ingest_env):
    result = ingestion_tool.ingest_by_identifiers(candidates=[IngestCandidate()])

    assert result["results"][0]["status"] == "failed"
    assert result["results"][0]["error"] == "no_usable_identifier"
    assert result["results"][0]["candidate_index"] == 0


def test_dedup_cross_identifier_journal_in_library_arxiv_input(ingest_env):
    ingest_env.get_item_key_by_doi.side_effect = (
        lambda doi: "ITEMJOURNAL" if doi == "10.1234/journal" else None
    )

    result = ingestion_tool.ingest_by_identifiers(
        candidates=[
            IngestCandidate(
                doi="10.1234/journal",
                arxiv_id="2301.00001",
                is_oa_published=False,
                title="Paper",
            )
        ]
    )

    assert result["results"][0]["status"] == "duplicate"
    assert result["results"][0]["item_key"] == "ITEMJOURNAL"


def test_dedup_cross_identifier_arxiv_in_library_journal_input(ingest_env):
    ingest_env.get_item_key_by_doi.side_effect = (
        lambda doi: "ITEMARXIV" if doi == "10.48550/arxiv.2301.00001" else None
    )

    result = ingestion_tool.ingest_by_identifiers(
        candidates=[
            IngestCandidate(
                doi="10.1234/journal",
                arxiv_id="2301.00001",
                is_oa_published=True,
                title="Paper",
            )
        ]
    )

    assert result["results"][0]["status"] == "duplicate"
    assert result["results"][0]["item_key"] == "ITEMARXIV"


def test_dedup_via_extra_field(ingest_env):
    ingest_env.get_item_key_by_arxiv_id.side_effect = (
        lambda arxiv_id: "ITEMEXTRA" if arxiv_id == "2301.00001" else None
    )

    result = ingestion_tool.ingest_by_identifiers(
        candidates=[
            IngestCandidate(
                doi="10.1234/journal",
                arxiv_id="2301.00001",
                is_oa_published=True,
                title="Paper",
            )
        ]
    )

    assert result["results"][0]["status"] == "duplicate"
    assert result["results"][0]["item_key"] == "ITEMEXTRA"


def test_exactly_one_candidates_xor_identifiers(ingest_env):
    with pytest.raises(ToolError):
        ingestion_tool.ingest_by_identifiers(
            candidates=[IngestCandidate(doi="10.1/x")],
            identifiers=["10.1/x"],
        )


def test_neither_candidates_nor_identifiers_fails():
    with pytest.raises(ToolError):
        ingestion_tool.ingest_by_identifiers()


def test_empty_candidates_list_rejected(ingest_env):
    """Empty list is indistinguishable from 'no useful input' — reject loudly.

    This catches the 'upstream filter wiped every selection' bug where
    agents chain `[r for r in search_results if not r['local_duplicate']]`
    and the filter removes everything. Silent success (`total=0`) would
    hide the bug; loud failure surfaces it.
    """
    with pytest.raises(ToolError, match="at least one candidate"):
        ingestion_tool.ingest_by_identifiers(candidates=[])


def test_empty_identifiers_list_rejected(ingest_env):
    """Same rule for the deprecated str branch — empty list is not a valid call."""
    with pytest.raises(ToolError, match="at least one candidate"):
        ingestion_tool.ingest_by_identifiers(identifiers=[])


def test_empty_candidates_error_mentions_upstream_filter(ingest_env):
    """The error message must coach agents on the common 'filter ate everything'
    failure mode — regression guard on user-facing guidance text."""
    with pytest.raises(ToolError) as exc_info:
        ingestion_tool.ingest_by_identifiers(candidates=[])
    assert "upstream filter" in str(exc_info.value)


def test_str_branch_still_works_with_deprecation_warning(ingest_env, caplog):
    with caplog.at_level(logging.WARNING, logger="zotpilot.tools.ingestion"):
        result = ingestion_tool.ingest_by_identifiers(identifiers=["10.1234/test"])

    assert result["results"][0]["status"] == "saved_metadata_only"
    assert result["results"][0]["identifier"] == "10.1234/test"
    assert "deprecated identifiers=<list[str]>" in caplog.text


# ---------------------------------------------------------------------------
# MCP client compat: list params serialized as JSON strings
# ---------------------------------------------------------------------------
# Some MCP client wrappers (Qwen-based 'Sisyphus' runtimes, older Claude Code
# builds) send list[T] params as JSON strings instead of real arrays. The tool
# must accept both forms transparently via a Pydantic BeforeValidator.

def test_candidates_accepts_json_string(ingest_env):
    """A JSON-string form of candidates must be parsed before Pydantic validation."""
    payload = '[{"doi": "10.1234/test", "title": "JSON-string candidate"}]'
    result = ingestion_tool.ingest_by_identifiers(candidates=payload)

    assert result["total"] == 1
    assert result["results"][0]["status"] == "saved_metadata_only"
    assert result["results"][0]["identifier"] == "10.1234/test"


def test_identifiers_accepts_json_string(ingest_env):
    """A JSON-string form of identifiers must also be accepted via BeforeValidator."""
    payload = '["10.1234/test"]'
    result = ingestion_tool.ingest_by_identifiers(identifiers=payload)

    assert result["total"] == 1
    assert result["results"][0]["status"] == "saved_metadata_only"
    assert result["results"][0]["identifier"] == "10.1234/test"


def test_candidates_malformed_json_raises_type_error(ingest_env):
    """Malformed JSON passes through as str, Pydantic then reports a type error —
    never silently swallow so the caller sees what went wrong."""
    from pydantic import ValidationError

    with pytest.raises((ToolError, ValidationError)):
        ingestion_tool.ingest_by_identifiers(candidates="not json at all")


def test_candidates_empty_json_string_list_rejected(ingest_env):
    """JSON-string `[]` still hits the empty-list rejection (the upstream-filter
    failure mode shouldn't have an escape hatch via string wrapping)."""
    with pytest.raises(ToolError, match="at least one candidate"):
        ingestion_tool.ingest_by_identifiers(candidates="[]")


# ---------------------------------------------------------------------------
# _parse_json_string_list helper — unit tests
# ---------------------------------------------------------------------------
# The helper is attached via Pydantic BeforeValidator to four different params:
# ingest_by_identifiers(candidates, identifiers) and
# search_academic_databases(concepts, institutions). Exercising it in isolation
# covers all four paths without mocking OpenAlex in the search path.

def test_parse_json_string_list_passes_through_real_list():
    from zotpilot.tools.ingestion import _parse_json_string_list
    payload = [{"doi": "10.1/x"}]
    assert _parse_json_string_list(payload) is payload  # identity, no copy


def test_parse_json_string_list_passes_through_none():
    from zotpilot.tools.ingestion import _parse_json_string_list
    assert _parse_json_string_list(None) is None


def test_parse_json_string_list_decodes_json_array_of_dicts():
    from zotpilot.tools.ingestion import _parse_json_string_list
    result = _parse_json_string_list('[{"doi": "10.1/x"}, {"arxiv_id": "2301.00001"}]')
    assert isinstance(result, list)
    assert result == [{"doi": "10.1/x"}, {"arxiv_id": "2301.00001"}]


def test_parse_json_string_list_decodes_json_array_of_strings():
    from zotpilot.tools.ingestion import _parse_json_string_list
    result = _parse_json_string_list('["Computer vision", "NLP"]')
    assert result == ["Computer vision", "NLP"]


def test_parse_json_string_list_malformed_passes_through_string():
    """Malformed input stays as a string so Pydantic surfaces a clear type error
    instead of us silently returning [] and masking the bug."""
    from zotpilot.tools.ingestion import _parse_json_string_list
    bad = "not valid json"
    assert _parse_json_string_list(bad) == bad


def test_parse_json_string_list_scalar_json_passes_through():
    """If the JSON decodes but isn't a list (e.g. '42' or '{}'), return unchanged
    so Pydantic validates it against the declared list[T] type and errors out."""
    from zotpilot.tools.ingestion import _parse_json_string_list
    assert _parse_json_string_list('42') == '42'
    assert _parse_json_string_list('{"doi": "10.1/x"}') == '{"doi": "10.1/x"}'
