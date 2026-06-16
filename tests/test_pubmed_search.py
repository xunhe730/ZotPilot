"""Tests for PubMed academic search provider."""
from unittest.mock import MagicMock, patch

import pytest

from zotpilot.academic_search import (
    AcademicSearchResult,
    merge_results,
)
from zotpilot.academic_search.pubmed_client import PubMedClient
from zotpilot.academic_search.pubmed_provider import PubMedSearchProvider, _format_pubmed_article
import xml.etree.ElementTree as ET


MOCK_ESearch_XML = """<?xml version="1.0" ?>
<eSearchResult>
    <Count>42</Count>
    <IdList>
        <Id>12345</Id>
        <Id>67890</Id>
    </IdList>
</eSearchResult>"""

MOCK_EFetch_XML = """<?xml version="1.0" ?>
<PubmedArticleSet>
    <PubmedArticle>
        <MedlineCitation>
            <PMID Version="1">12345</PMID>
            <Article>
                <ArticleTitle>Test Paper Title</ArticleTitle>
                <AuthorList>
                    <Author>
                        <LastName>Smith</LastName>
                        <ForeName>John A</ForeName>
                    </Author>
                    <Author>
                        <LastName>Doe</LastName>
                        <ForeName>Jane B</ForeName>
                    </Author>
                </AuthorList>
                <Journal>
                    <Title>Journal of Testing</Title>
                    <JournalIssue>
                        <PubDate>
                            <Year>2024</Year>
                        </PubDate>
                    </JournalIssue>
                </Journal>
                <Abstract>
                    <AbstractText>This is the abstract.</AbstractText>
                </Abstract>
            </Article>
        </MedlineCitation>
        <PubmedData>
            <ArticleIdList>
                <ArticleId IdType="doi">10.1234/test.2024.001</ArticleId>
            </ArticleIdList>
        </PubmedData>
    </PubmedArticle>
</PubmedArticleSet>"""


class TestPubMedClient:
    def test_esearch(self):
        client = PubMedClient()
        mock_response = MagicMock()
        mock_response.text = MOCK_ESearch_XML
        mock_response.raise_for_status = MagicMock()

        with patch("zotpilot.academic_search.pubmed_client.httpx.Client") as mock_httpx:
            mock_httpx.return_value.__enter__.return_value.get.return_value = mock_response
            pmids, total = client.esearch("cancer treatment")

        assert pmids == ["12345", "67890"]
        assert total == 42

    def test_esearch_empty_results(self):
        client = PubMedClient()
        mock_response = MagicMock()
        mock_response.text = '<?xml version="1.0" ?><eSearchResult><Count>0</Count><IdList/></eSearchResult>'
        mock_response.raise_for_status = MagicMock()

        with patch("zotpilot.academic_search.pubmed_client.httpx.Client") as mock_httpx:
            mock_httpx.return_value.__enter__.return_value.get.return_value = mock_response
            pmids, total = client.esearch("nonexistent query")

        assert pmids == []
        assert total == 0

    def test_efetch(self):
        client = PubMedClient()
        mock_response = MagicMock()
        mock_response.text = MOCK_EFetch_XML
        mock_response.raise_for_status = MagicMock()

        with patch("zotpilot.academic_search.pubmed_client.httpx.Client") as mock_httpx:
            mock_httpx.return_value.__enter__.return_value.get.return_value = mock_response
            articles = client.efetch(["12345"])

        assert len(articles) == 1
        article = articles[0]
        assert article["title"] == "Test Paper Title"
        assert article["authors"] == ["John A Smith", "Jane B Doe"]
        assert article["year"] == 2024
        assert article["doi"] == "10.1234/test.2024.001"
        assert article["pmid"] == "12345"
        assert article["journal"] == "Journal of Testing"

    def test_efetch_empty(self):
        client = PubMedClient()
        articles = client.efetch([])
        assert articles == []

    def test_rate_limiting(self):
        client = PubMedClient()
        assert client._min_interval == 0.34

    def test_rate_limiting_with_api_key(self):
        client = PubMedClient(api_key="test-key")
        assert client._min_interval == 0.1

    def test_retry_on_server_error(self):
        client = PubMedClient(max_retries=2)
        mock_response = MagicMock()
        mock_response.text = MOCK_ESearch_XML
        mock_response.raise_for_status = MagicMock()

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                import httpx
                exc = httpx.HTTPStatusError(
                    "Server Error",
                    request=MagicMock(),
                    response=MagicMock(status_code=500),
                )
                raise exc
            return mock_response

        with patch("zotpilot.academic_search.pubmed_client.httpx.Client") as mock_httpx:
            mock_httpx.return_value.__enter__.return_value.get.side_effect = side_effect
            with patch("zotpilot.academic_search.pubmed_client.time.sleep"):
                pmids, total = client.esearch("test")

        assert pmids == ["12345", "67890"]
        assert call_count == 2


class TestPubMedSearchProvider:
    @patch("zotpilot.academic_search.pubmed_provider.PubMedClient")
    def test_search(self, MockClient):
        mock_client = MockClient.return_value
        mock_client.esearch.return_value = (["12345"], 1)
        mock_client.efetch.return_value = [{
            "pmid": "12345",
            "title": "Test",
            "authors": ["Author One"],
            "year": 2024,
            "doi": "10.1234/test",
            "journal": "Test Journal",
            "abstract": "Abstract text",
            "_source": "pubmed",
        }]

        provider = PubMedSearchProvider()
        result = provider.search(
            "cancer",
            limit=10,
            year_min=2020,
            year_max=None,
            sort_by="relevance",
        )

        assert isinstance(result, AcademicSearchResult)
        assert len(result.results) == 1
        assert result.total_count == 1
        assert result.results[0]["_source"] == "pubmed"

    @patch("zotpilot.academic_search.pubmed_provider.PubMedClient")
    def test_get_by_doi(self, MockClient):
        mock_client = MockClient.return_value
        mock_client.esearch.return_value = (["12345"], 1)
        mock_client.efetch.return_value = [{
            "pmid": "12345",
            "title": "Test",
            "authors": [],
            "year": 2024,
            "doi": "10.1234/test",
            "journal": "Test Journal",
            "abstract": "",
            "_source": "pubmed",
        }]

        provider = PubMedSearchProvider()
        results = provider.get_by_doi("10.1234/test")

        assert len(results) == 1
        assert results[0]["doi"] == "10.1234/test"

    @patch("zotpilot.academic_search.pubmed_provider.PubMedClient")
    def test_get_by_doi_not_found(self, MockClient):
        mock_client = MockClient.return_value
        mock_client.esearch.return_value = ([], 0)

        provider = PubMedSearchProvider()
        results = provider.get_by_doi("10.9999/notfound")

        assert results == []


class TestFormatPubMedArticle:
    def test_format_basic(self):
        article = {
            "pmid": "12345",
            "title": "Test Title",
            "authors": ["Author One", "Author Two"],
            "year": 2024,
            "doi": "10.1234/test",
            "journal": "Test Journal",
            "abstract": "This is an abstract.",
            "_source": "pubmed",
        }
        result = _format_pubmed_article(article)

        assert result["title"] == "Test Title"
        assert result["authors"] == ["Author One", "Author Two"]
        assert result["year"] == 2024
        assert result["doi"] == "10.1234/test"
        assert result["pmid"] == "12345"
        assert result["journal"] == "Test Journal"
        assert result["_source"] == "pubmed"
        assert result["cited_by_count"] == 0

    def test_format_empty_fields(self):
        article = {}
        result = _format_pubmed_article(article)

        assert result["title"] is None
        assert result["authors"] == []
        assert result["year"] is None
        assert result["doi"] is None
        assert result["_source"] == "pubmed"


class TestMergeResults:
    def test_merge_no_duplicates(self):
        r1 = AcademicSearchResult(
            results=[{"doi": "10.1111/a", "title": "Paper A"}],
            total_count=1,
        )
        r2 = AcademicSearchResult(
            results=[{"doi": "10.2222/b", "title": "Paper B"}],
            total_count=1,
        )
        merged = merge_results([r1, r2], limit=10)

        assert len(merged.results) == 2
        assert merged.total_count == 2

    def test_merge_with_duplicates(self):
        r1 = AcademicSearchResult(
            results=[{"doi": "10.1234/same", "title": "Version 1"}],
            total_count=1,
        )
        r2 = AcademicSearchResult(
            results=[{"doi": "10.1234/same", "title": "Version 2"}],
            total_count=1,
        )
        merged = merge_results([r1, r2], limit=10)

        assert len(merged.results) == 1
        assert merged.results[0]["title"] == "Version 1"

    def test_merge_respects_limit(self):
        r1 = AcademicSearchResult(
            results=[{"doi": f"10.1111/{i}", "title": f"Paper {i}"} for i in range(10)],
            total_count=10,
        )
        merged = merge_results([r1], limit=5)

        assert len(merged.results) == 5

    def test_merge_empty(self):
        merged = merge_results([], limit=10)
        assert merged.results == []
        assert merged.total_count == 0
