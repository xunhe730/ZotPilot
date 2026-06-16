"""Low-level HTTP client for NCBI E-utilities (PubMed)."""
from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DEFAULT_EMAIL = "zotpilot@example.com"


class PubMedClient:
    """NCBI E-utilities client with rate limiting and retries."""

    def __init__(
        self,
        api_key: str | None = None,
        email: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self._api_key = api_key
        self._email = email or DEFAULT_EMAIL
        self._timeout = timeout
        self._max_retries = max_retries
        self._last_request_time = 0.0
        self._min_interval = 0.1 if api_key else 0.34

    def _rate_limit(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.monotonic()

    def _request(self, endpoint: str, params: dict) -> ET.Element:
        self._rate_limit()
        url = f"{BASE_URL}/{endpoint}.fcgi"
        params["retmode"] = "xml"
        if self._api_key:
            params["api_key"] = self._api_key
        params["email"] = self._email

        last_exc = None
        for attempt in range(self._max_retries):
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.get(url, params=params)
                    response.raise_for_status()
                    return ET.fromstring(response.text)
            except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as exc:
                last_exc = exc
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                    retry_after = float(exc.response.headers.get("Retry-After", 1))
                    logger.warning("PubMed rate limited, retrying after %.1fs", retry_after)
                    time.sleep(retry_after)
                elif isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                    raise
                else:
                    wait = 2 ** attempt
                    logger.warning("PubMed request failed (%s), retrying in %ds", exc, wait)
                    time.sleep(wait)
            except Exception:
                raise
        raise last_exc

    def esearch(
        self,
        query: str,
        db: str = "pubmed",
        retmax: int = 20,
        sort: str = "relevance",
        mindate: str | None = None,
        maxdate: str | None = None,
    ) -> tuple[list[str], int]:
        """Search PubMed and return (list of PMIDs, total count)."""
        params: dict = {
            "db": db,
            "term": query,
            "retmax": retmax,
            "sort": sort,
        }
        if mindate:
            params["mindate"] = mindate
            params["datetype"] = "pdat"
        if maxdate:
            params["maxdate"] = maxdate
            params["datetype"] = "pdat"

        root = self._request("esearch", params)
        id_list = [id_elem.text for id_elem in root.findall(".//Id") if id_elem.text]
        count_elem = root.find(".//Count")
        total = int(count_elem.text) if count_elem is not None and count_elem.text else 0
        return id_list, total

    def efetch(self, pmids: list[str], db: str = "pubmed") -> list[dict]:
        """Fetch article details by PMID list. Returns list of article dicts."""
        if not pmids:
            return []
        params = {
            "db": db,
            "id": ",".join(pmids),
            "retmode": "xml",
        }
        root = self._request("efetch", params)
        articles = []
        for article_elem in root.findall(".//PubmedArticle"):
            article = self._parse_article(article_elem)
            if article:
                articles.append(article)
        return articles

    def _parse_article(self, article_elem: ET.Element) -> dict | None:
        """Parse a PubmedArticle XML element into a normalized dict."""
        try:
            medline = article_elem.find(".//MedlineCitation")
            if medline is None:
                return None

            article = medline.find(".//Article")
            if article is None:
                return None

            pmid_elem = medline.find(".//PMID")
            pmid = pmid_elem.text if pmid_elem is not None else None

            title_elem = article.find(".//ArticleTitle")
            title = title_elem.text if title_elem is not None else ""

            authors = []
            author_list = article.find(".//AuthorList")
            if author_list is not None:
                for author_elem in author_list.findall(".//Author")[:5]:
                    last = author_elem.find("LastName")
                    fore = author_elem.find("ForeName")
                    parts = []
                    if fore is not None and fore.text:
                        parts.append(fore.text)
                    if last is not None and last.text:
                        parts.append(last.text)
                    if parts:
                        authors.append(" ".join(parts))

            year = None
            journal = article.find(".//Journal")
            if journal is not None:
                journal_title_elem = journal.find(".//Title")
                journal_title = journal_title_elem.text if journal_title_elem is not None else None
                pub_date = journal.find(".//JournalIssue/PubDate")
                if pub_date is not None:
                    year_elem = pub_date.find("Year")
                    if year_elem is not None and year_elem.text:
                        year = int(year_elem.text)

            abstract_parts = []
            abstract_elem = article.find(".//Abstract")
            if abstract_elem is not None:
                for abs_text in abstract_elem.findall(".//AbstractText"):
                    label = abs_text.get("Label", "")
                    text = abs_text.text or ""
                    if label:
                        abstract_parts.append(f"{label}: {text}")
                    else:
                        abstract_parts.append(text)
            abstract = " ".join(abstract_parts)

            doi = None
            article_ids = article_elem.find(".//PubmedData/ArticleIdList")
            if article_ids is not None:
                for id_elem in article_ids.findall(".//ArticleId"):
                    if id_elem.get("IdType") == "doi" and id_elem.text:
                        doi = id_elem.text
                        break

            return {
                "pmid": pmid,
                "title": title,
                "authors": authors,
                "year": year,
                "doi": doi,
                "journal": journal_title,
                "abstract": abstract,
                "_source": "pubmed",
            }
        except Exception:
            logger.debug("Failed to parse PubMed article", exc_info=True)
            return None
