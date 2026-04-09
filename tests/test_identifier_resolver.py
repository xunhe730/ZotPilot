"""Tests for IdentifierResolver."""
from unittest.mock import MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from zotpilot.crossref_client import CrossRefWork
from zotpilot.identifier_resolver import IdentifierResolver

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_crossref_work(**kwargs):
    defaults = dict(
        doi="10.1038/test",
        title="Test Paper",
        authors=[{"creatorType": "author", "firstName": "Jane", "lastName": "Doe"}],
        year=2023,
        item_type="journalArticle",
        abstract="An abstract.",
        journal="Nature",
        volume="12",
        issue="3",
        pages="100-110",
        publisher="Springer",
        url="https://doi.org/10.1038/test",
        oa_url="https://example.com/paper.pdf",
        raw={"title": ["Test Paper"]},
    )
    defaults.update(kwargs)
    return CrossRefWork(**defaults)


ARXIV_ATOM_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <title>Deep Learning Survey</title>
    <summary>A comprehensive survey of deep learning methods.</summary>
    <published>2023-01-15T00:00:00Z</published>
    <author><name>John Smith</name></author>
    <author><name>Jane Doe</name></author>
    <arxiv:doi>10.9999/dl-survey</arxiv:doi>
  </entry>
</feed>"""

ARXIV_ATOM_EMPTY = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
</feed>"""


# ---------------------------------------------------------------------------
# Identifier format detection
# ---------------------------------------------------------------------------

class TestIdentifierDetection:
    def _resolver_with_mock_crossref(self, work=None):
        resolver = IdentifierResolver()
        resolver._crossref = MagicMock()
        resolver._crossref.get_by_doi.return_value = work or _make_crossref_work()
        return resolver

    def test_bare_doi(self):
        resolver = self._resolver_with_mock_crossref()
        meta = resolver.resolve("10.1038/s41586-024-00001-0")
        assert meta.doi is not None
        resolver._crossref.get_by_doi.assert_called_once()

    def test_doi_org_url(self):
        resolver = self._resolver_with_mock_crossref()
        resolver.resolve("https://doi.org/10.1038/test")
        resolver._crossref.get_by_doi.assert_called_once()
        args = resolver._crossref.get_by_doi.call_args[0][0]
        assert args == "10.1038/test"

    def test_http_doi_org_url(self):
        resolver = self._resolver_with_mock_crossref()
        resolver.resolve("http://doi.org/10.1038/test")
        args = resolver._crossref.get_by_doi.call_args[0][0]
        assert args == "10.1038/test"

    def test_arxiv_prefix(self):
        resolver = IdentifierResolver()
        with patch("zotpilot.identifier_resolver.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = ARXIV_ATOM_RESPONSE
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            meta = resolver.resolve("arxiv:2301.00001")
        assert meta.arxiv_id == "2301.00001"
        assert meta.item_type == "preprint"

    def test_arxiv_url_abs(self):
        resolver = IdentifierResolver()
        with patch("zotpilot.identifier_resolver.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = ARXIV_ATOM_RESPONSE
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            meta = resolver.resolve("https://arxiv.org/abs/2301.00001")
        assert meta.arxiv_id == "2301.00001"

    def test_arxiv_url_pdf(self):
        resolver = IdentifierResolver()
        with patch("zotpilot.identifier_resolver.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = ARXIV_ATOM_RESPONSE
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            meta = resolver.resolve("https://arxiv.org/pdf/2301.00001.pdf")
        assert meta.arxiv_id == "2301.00001"

    def test_bare_arxiv_id(self):
        resolver = IdentifierResolver()
        with patch("zotpilot.identifier_resolver.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = ARXIV_ATOM_RESPONSE
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            meta = resolver.resolve("2301.00001")
        assert meta.arxiv_id == "2301.00001"

    def test_unknown_input_raises_tool_error(self):
        resolver = IdentifierResolver()
        with pytest.raises(ToolError, match="Unrecognized"):
            resolver.resolve("not-a-valid-identifier")

    def test_empty_string_raises_tool_error(self):
        resolver = IdentifierResolver()
        with pytest.raises(ToolError):
            resolver.resolve("")

    def test_random_string_raises_tool_error(self):
        resolver = IdentifierResolver()
        with pytest.raises(ToolError):
            resolver.resolve("hello world this is gibberish")


# ---------------------------------------------------------------------------
# DOI resolution path
# ---------------------------------------------------------------------------

class TestDOIResolution:
    def test_crossref_success_returns_metadata(self):
        resolver = IdentifierResolver()
        resolver._crossref = MagicMock()
        resolver._crossref.get_by_doi.return_value = _make_crossref_work(
            oa_url="https://example.com/paper.pdf"
        )
        meta = resolver.resolve("10.1038/test")

        assert meta.title == "Test Paper"
        assert meta.doi == "10.1038/test"
        assert meta.oa_url == "https://example.com/paper.pdf"
        assert meta.year == 2023
        assert meta.item_type == "journalArticle"

    def test_crossref_none_raises_tool_error(self):
        resolver = IdentifierResolver()
        resolver._crossref = MagicMock()
        resolver._crossref.get_by_doi.return_value = None

        with pytest.raises(ToolError, match="not found"):
            resolver.resolve("10.9999/missing")

    def test_last_crossref_metadata_set(self):
        resolver = IdentifierResolver()
        resolver._crossref = MagicMock()
        work = _make_crossref_work(raw={"title": ["Test Paper"], "DOI": "10.1038/test"})
        resolver._crossref.get_by_doi.return_value = work

        resolver.resolve("10.1038/test")
        assert resolver.last_crossref_metadata is not None
        assert resolver.last_crossref_metadata == work.raw

    def test_doi_normalization_strips_prefix(self):
        resolver = IdentifierResolver()
        resolver._crossref = MagicMock()
        resolver._crossref.get_by_doi.return_value = _make_crossref_work()

        resolver.resolve("https://doi.org/10.1038/test")
        called_doi = resolver._crossref.get_by_doi.call_args[0][0]
        assert called_doi == "10.1038/test"


# ---------------------------------------------------------------------------
# arXiv resolution path
# ---------------------------------------------------------------------------

class TestArXivResolution:
    def _mock_get(self, text=ARXIV_ATOM_RESPONSE):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = text
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    def test_parses_title(self):
        resolver = IdentifierResolver()
        with patch("zotpilot.identifier_resolver.httpx.get") as mock_get:
            mock_get.return_value = self._mock_get()
            meta = resolver.resolve("arxiv:2301.00001")
        assert meta.title == "Deep Learning Survey"

    def test_parses_authors(self):
        resolver = IdentifierResolver()
        with patch("zotpilot.identifier_resolver.httpx.get") as mock_get:
            mock_get.return_value = self._mock_get()
            meta = resolver.resolve("arxiv:2301.00001")
        assert len(meta.authors) == 2
        assert meta.authors[0]["lastName"] == "Smith"

    def test_parses_year(self):
        resolver = IdentifierResolver()
        with patch("zotpilot.identifier_resolver.httpx.get") as mock_get:
            mock_get.return_value = self._mock_get()
            meta = resolver.resolve("arxiv:2301.00001")
        assert meta.year == 2023

    def test_item_type_is_preprint(self):
        resolver = IdentifierResolver()
        with patch("zotpilot.identifier_resolver.httpx.get") as mock_get:
            mock_get.return_value = self._mock_get()
            meta = resolver.resolve("arxiv:2301.00001")
        assert meta.item_type == "preprint"

    def test_arxiv_id_set(self):
        resolver = IdentifierResolver()
        with patch("zotpilot.identifier_resolver.httpx.get") as mock_get:
            mock_get.return_value = self._mock_get()
            meta = resolver.resolve("arxiv:2301.00001")
        assert meta.arxiv_id == "2301.00001"

    def test_oa_url_points_to_pdf(self):
        resolver = IdentifierResolver()
        with patch("zotpilot.identifier_resolver.httpx.get") as mock_get:
            mock_get.return_value = self._mock_get()
            meta = resolver.resolve("arxiv:2301.00001")
        assert "arxiv.org/pdf" in meta.oa_url
        assert "2301.00001" in meta.oa_url

    def test_version_suffix_stripped(self):
        resolver = IdentifierResolver()
        with patch("zotpilot.identifier_resolver.httpx.get") as mock_get:
            mock_get.return_value = self._mock_get()
            meta = resolver.resolve("arxiv:2301.00001v3")
        assert meta.arxiv_id == "2301.00001"

    def test_empty_feed_raises_tool_error(self):
        resolver = IdentifierResolver()
        with patch("zotpilot.identifier_resolver.httpx.get") as mock_get:
            mock_get.return_value = self._mock_get(ARXIV_ATOM_EMPTY)
            with pytest.raises(ToolError, match="not found"):
                resolver.resolve("arxiv:9999.99999")

    def test_timeout_raises_tool_error(self):
        import httpx as _httpx
        resolver = IdentifierResolver()
        with patch("zotpilot.identifier_resolver.httpx.get") as mock_get:
            mock_get.side_effect = _httpx.TimeoutException("timeout")
            with pytest.raises(ToolError, match="timed out"):
                resolver.resolve("arxiv:2301.00001")

    def test_doi_from_arxiv_stored(self):
        resolver = IdentifierResolver()
        with patch("zotpilot.identifier_resolver.httpx.get") as mock_get:
            mock_get.return_value = self._mock_get()
            meta = resolver.resolve("arxiv:2301.00001")
        # The atom response includes arxiv:doi → should be parsed
        assert meta.doi == "10.9999/dl-survey"

    def test_http_status_error_raises_tool_error(self):
        import httpx as _httpx
        resolver = IdentifierResolver()
        with patch("zotpilot.identifier_resolver.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 503
            mock_get.side_effect = _httpx.HTTPStatusError(
                "Service unavailable", request=MagicMock(), response=mock_resp
            )
            with pytest.raises(ToolError, match="503"):
                resolver.resolve("arxiv:2301.00001")

    def test_malformed_xml_raises_tool_error(self):
        resolver = IdentifierResolver()
        with patch("zotpilot.identifier_resolver.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = "this is not xml at all <<<>>>"
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            with pytest.raises(ToolError, match="Failed to parse"):
                resolver.resolve("arxiv:2301.00001")


# ---------------------------------------------------------------------------
# S2 resolution path
# ---------------------------------------------------------------------------

S2_PAPER_RESPONSE = {
    "paperId": "a" * 40,
    "title": "S2 Paper Title",
    "authors": [{"name": "Alice Brown"}, {"name": "Bob Green"}],
    "year": 2022,
    "externalIds": {"DOI": None, "ArXiv": None},
    "abstract": "S2 abstract text.",
    "openAccessPdf": {"url": "https://example.com/s2paper.pdf"},
    "journal": {"name": "S2 Journal", "volume": "5", "pages": "1-10"},
}


class TestS2Resolution:
    def _mock_s2_resp(self, data=None, status=200):
        mock_resp = MagicMock()
        mock_resp.status_code = status
        mock_resp.json.return_value = data or S2_PAPER_RESPONSE
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    def test_s2_id_resolves_to_metadata(self):
        s2_id = "a" * 40
        resolver = IdentifierResolver()
        with patch("zotpilot.identifier_resolver.httpx.get") as mock_get:
            mock_get.return_value = self._mock_s2_resp()
            meta = resolver.resolve(s2_id)

        assert meta.title == "S2 Paper Title"
        assert meta.year == 2022
        assert meta.oa_url == "https://example.com/s2paper.pdf"

    def test_s2_404_raises_tool_error(self):
        s2_id = "b" * 40
        resolver = IdentifierResolver()
        with patch("zotpilot.identifier_resolver.httpx.get") as mock_get:
            mock_get.return_value = self._mock_s2_resp(status=404)
            with pytest.raises(ToolError, match="not found"):
                resolver.resolve(s2_id)

    def test_s2_with_doi_prefers_crossref(self):
        s2_id = "c" * 40
        data = {**S2_PAPER_RESPONSE, "externalIds": {"DOI": "10.1038/test", "ArXiv": None}}
        resolver = IdentifierResolver()
        resolver._crossref = MagicMock()

        from zotpilot.crossref_client import CrossRefWork
        work = CrossRefWork(
            doi="10.1038/test", title="CrossRef Title", authors=[],
            year=2023, item_type="journalArticle", abstract=None,
            journal="Nature", volume=None, issue=None, pages=None,
            publisher=None, url="https://doi.org/10.1038/test", oa_url=None,
            raw={"title": ["CrossRef Title"]},
        )
        resolver._crossref.get_by_doi.return_value = work

        with patch("zotpilot.identifier_resolver.httpx.get") as mock_get:
            mock_get.return_value = self._mock_s2_resp(data=data)
            meta = resolver.resolve(s2_id)

        assert meta.title == "CrossRef Title"

    def test_s2_with_doi_crossref_fails_falls_back(self):
        s2_id = "d" * 40
        data = {
            **S2_PAPER_RESPONSE,
            "externalIds": {"DOI": "10.1038/test", "ArXiv": None},
            "title": "Fallback Title",
        }
        resolver = IdentifierResolver()
        resolver._crossref = MagicMock()
        resolver._crossref.get_by_doi.return_value = None  # CrossRef fails

        with patch("zotpilot.identifier_resolver.httpx.get") as mock_get:
            mock_get.return_value = self._mock_s2_resp(data=data)
            meta = resolver.resolve(s2_id)

        assert meta.title == "Fallback Title"

    def test_s2_arxiv_fallback_oa_url(self):
        s2_id = "e" * 40
        data = {
            **S2_PAPER_RESPONSE,
            "externalIds": {"ArXiv": "2301.00001"},
            "openAccessPdf": None,
        }
        resolver = IdentifierResolver()
        with patch("zotpilot.identifier_resolver.httpx.get") as mock_get:
            mock_get.return_value = self._mock_s2_resp(data=data)
            meta = resolver.resolve(s2_id)

        assert "2301.00001" in (meta.oa_url or "")

    def test_s2_network_error_raises_tool_error(self):
        s2_id = "f" * 40
        resolver = IdentifierResolver()
        with patch("zotpilot.identifier_resolver.httpx.get") as mock_get:
            mock_get.side_effect = Exception("network error")
            with pytest.raises(ToolError, match="lookup failed"):
                resolver.resolve(s2_id)
