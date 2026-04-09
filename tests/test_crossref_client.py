"""Tests for CrossRef API client."""
from unittest.mock import MagicMock, patch

import httpx

from zotpilot.crossref_client import (
    CrossRefClient,
    _crossref_type_to_zotero,
    _extract_authors,
    _extract_year,
)

# ---------------------------------------------------------------------------
# Type mapping
# ---------------------------------------------------------------------------

class TestCrossRefTypeMapping:
    def test_journal_article(self):
        assert _crossref_type_to_zotero("journal-article") == "journalArticle"

    def test_proceedings_article(self):
        assert _crossref_type_to_zotero("proceedings-article") == "conferencePaper"

    def test_posted_content_is_preprint(self):
        assert _crossref_type_to_zotero("posted-content") == "preprint"

    def test_preprint_is_preprint(self):
        assert _crossref_type_to_zotero("preprint") == "preprint"

    def test_unknown_falls_back_to_journal_article(self):
        assert _crossref_type_to_zotero("unknown-weird-type") == "journalArticle"

    def test_book(self):
        assert _crossref_type_to_zotero("book") == "book"

    def test_book_chapter(self):
        assert _crossref_type_to_zotero("book-chapter") == "bookSection"


# ---------------------------------------------------------------------------
# Author extraction
# ---------------------------------------------------------------------------

class TestExtractAuthors:
    def test_given_family(self):
        data = {"author": [{"given": "Jane", "family": "Doe"}]}
        authors = _extract_authors(data)
        assert len(authors) == 1
        assert authors[0] == {"creatorType": "author", "firstName": "Jane", "lastName": "Doe"}

    def test_family_only(self):
        data = {"author": [{"family": "Smith"}]}
        authors = _extract_authors(data)
        assert len(authors) == 1
        assert authors[0]["lastName"] == "Smith"
        assert authors[0]["firstName"] == ""

    def test_empty_author_skipped(self):
        data = {"author": [{}]}
        assert _extract_authors(data) == []

    def test_no_author_field(self):
        assert _extract_authors({}) == []

    def test_multiple_authors(self):
        data = {
            "author": [
                {"given": "A", "family": "Alpha"},
                {"given": "B", "family": "Beta"},
            ]
        }
        authors = _extract_authors(data)
        assert len(authors) == 2


# ---------------------------------------------------------------------------
# Year extraction
# ---------------------------------------------------------------------------

class TestExtractYear:
    def test_published_print(self):
        data = {"published-print": {"date-parts": [[2023, 5, 1]]}}
        assert _extract_year(data) == 2023

    def test_published_online_fallback(self):
        data = {"published-online": {"date-parts": [[2022, 1]]}}
        assert _extract_year(data) == 2022

    def test_no_date_returns_none(self):
        assert _extract_year({}) is None

    def test_empty_parts_returns_none(self):
        data = {"published-print": {"date-parts": [[]]}}
        assert _extract_year(data) is None


# ---------------------------------------------------------------------------
# CrossRefClient.get_by_doi
# ---------------------------------------------------------------------------

def _mock_crossref_response(doi="10.1038/test", abstract="Normal abstract."):
    data = {
        "message": {
            "DOI": doi,
            "title": ["Test Paper Title"],
            "author": [
                {"given": "Jane", "family": "Doe"},
                {"given": "John", "family": "Smith"},
            ],
            "type": "journal-article",
            "published-print": {"date-parts": [[2023, 5, 1]]},
            "container-title": ["Nature"],
            "volume": "12",
            "issue": "3",
            "page": "100-110",
            "publisher": "Springer",
            "abstract": abstract,
            "link": [
                {"content-type": "application/pdf", "URL": "https://example.com/paper.pdf"}
            ],
        }
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = data
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


class TestCrossRefClientGetByDoi:
    def test_successful_resolution(self):
        with patch("zotpilot.crossref_client.httpx.get") as mock_get:
            mock_get.return_value = _mock_crossref_response()
            client = CrossRefClient()
            work = client.get_by_doi("10.1038/test")

        assert work is not None
        assert work.title == "Test Paper Title"
        assert work.year == 2023
        assert work.item_type == "journalArticle"
        assert work.journal == "Nature"
        assert work.volume == "12"
        assert work.issue == "3"
        assert work.pages == "100-110"
        assert work.publisher == "Springer"
        assert work.oa_url == "https://example.com/paper.pdf"
        assert len(work.authors) == 2

    def test_authors_parsed_correctly(self):
        with patch("zotpilot.crossref_client.httpx.get") as mock_get:
            mock_get.return_value = _mock_crossref_response()
            client = CrossRefClient()
            work = client.get_by_doi("10.1038/test")

        assert work.authors[0]["firstName"] == "Jane"
        assert work.authors[0]["lastName"] == "Doe"
        assert work.authors[0]["creatorType"] == "author"

    def test_doi_prefix_stripped_in_url(self):
        with patch("zotpilot.crossref_client.httpx.get") as mock_get:
            mock_get.return_value = _mock_crossref_response()
            client = CrossRefClient()
            client.get_by_doi("https://doi.org/10.1038/test")

        call_url = mock_get.call_args[0][0]
        assert "https://doi.org/" not in call_url
        assert "10.1038/test" in call_url

    def test_http_doi_prefix_stripped(self):
        with patch("zotpilot.crossref_client.httpx.get") as mock_get:
            mock_get.return_value = _mock_crossref_response()
            client = CrossRefClient()
            client.get_by_doi("http://doi.org/10.1038/test")

        call_url = mock_get.call_args[0][0]
        assert "10.1038/test" in call_url

    def test_404_returns_none(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch("zotpilot.crossref_client.httpx.get") as mock_get:
            mock_get.return_value = mock_resp
            client = CrossRefClient()
            result = client.get_by_doi("10.9999/notfound")

        assert result is None

    def test_timeout_returns_none(self):
        with patch("zotpilot.crossref_client.httpx.get") as mock_get:
            mock_get.side_effect = httpx.TimeoutException("timeout")
            client = CrossRefClient()
            result = client.get_by_doi("10.1038/test")

        assert result is None

    def test_network_error_returns_none(self):
        with patch("zotpilot.crossref_client.httpx.get") as mock_get:
            mock_get.side_effect = Exception("connection error")
            client = CrossRefClient()
            result = client.get_by_doi("10.1038/test")

        assert result is None

    def test_abstract_jats_tags_stripped(self):
        with patch("zotpilot.crossref_client.httpx.get") as mock_get:
            mock_get.return_value = _mock_crossref_response(
                abstract="<jats:p>This is the abstract text.</jats:p>"
            )
            client = CrossRefClient()
            work = client.get_by_doi("10.1038/test")

        assert work.abstract == "This is the abstract text."
        assert "<jats:p>" not in work.abstract

    def test_abstract_nested_jats_stripped(self):
        with patch("zotpilot.crossref_client.httpx.get") as mock_get:
            mock_get.return_value = _mock_crossref_response(
                abstract="<jats:sec><jats:title>Summary</jats:title><jats:p>Content.</jats:p></jats:sec>"
            )
            client = CrossRefClient()
            work = client.get_by_doi("10.1038/test")

        assert "<jats:" not in work.abstract
        assert "Summary" in work.abstract
        assert "Content." in work.abstract

    def test_raw_stored_in_work(self):
        with patch("zotpilot.crossref_client.httpx.get") as mock_get:
            mock_get.return_value = _mock_crossref_response()
            client = CrossRefClient()
            work = client.get_by_doi("10.1038/test")

        assert isinstance(work.raw, dict)
        assert "title" in work.raw

    def test_raw_contains_relation_field_when_present(self):
        resp = _mock_crossref_response()
        resp.json.return_value["message"]["relation"] = {
            "has-preprint": [{"id": "2301.00001", "id-type": "arxiv"}]
        }
        with patch("zotpilot.crossref_client.httpx.get") as mock_get:
            mock_get.return_value = resp
            client = CrossRefClient()
            work = client.get_by_doi("10.1038/test")

        assert "relation" in work.raw
        assert work.raw["relation"]["has-preprint"][0]["id"] == "2301.00001"

    def test_no_pdf_link_oa_url_is_none(self):
        resp = _mock_crossref_response()
        resp.json.return_value["message"]["link"] = [
            {"content-type": "text/html", "URL": "https://example.com/page"}
        ]
        with patch("zotpilot.crossref_client.httpx.get") as mock_get:
            mock_get.return_value = resp
            client = CrossRefClient()
            work = client.get_by_doi("10.1038/test")

        assert work.oa_url is None

    def test_no_container_title_journal_is_none(self):
        resp = _mock_crossref_response()
        resp.json.return_value["message"].pop("container-title", None)
        with patch("zotpilot.crossref_client.httpx.get") as mock_get:
            mock_get.return_value = resp
            client = CrossRefClient()
            work = client.get_by_doi("10.1038/test")

        assert work.journal is None

    def test_url_includes_doi(self):
        with patch("zotpilot.crossref_client.httpx.get") as mock_get:
            mock_get.return_value = _mock_crossref_response()
            client = CrossRefClient()
            work = client.get_by_doi("10.1038/test")

        assert work.url == "https://doi.org/10.1038/test"
