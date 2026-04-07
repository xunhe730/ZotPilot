"""OpenAlex API client for citation data."""
import logging
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

OPENALEX_API = "https://api.openalex.org"
WORK_SEARCH_SELECT = (
    "id,doi,title,display_name,publication_year,cited_by_count,type,"
    "is_retracted,authorships,primary_location,abstract_inverted_index,"
    "open_access,ids,relevance_score"
)


@dataclass
class CitationData:
    """Citation information for a paper."""
    openalex_id: str
    doi: str | None
    cited_by_count: int
    references: list[str]


class OpenAlexClient:
    """Client for OpenAlex API.

    Rate limits:
    - Anonymous: 1 request/second
    - Polite pool (with email): 10 requests/second
    """

    def __init__(self, email: str | None = None):
        """Initialize client.

        Args:
            email: Optional email for polite pool (faster rate limits).
                   Set via config.openalex_email or OPENALEX_EMAIL env var.
        """
        self.email = email
        self.headers = {}
        if email:
            self.headers["User-Agent"] = f"mailto:{email}"
            self._rate_limit_delay = 0.1  # 10 req/sec
        else:
            self._rate_limit_delay = 1.0  # 1 req/sec
        self._last_request = 0.0

    def _rate_limit(self):
        """Enforce rate limiting."""
        elapsed = time.time() - self._last_request
        if elapsed < self._rate_limit_delay:
            time.sleep(self._rate_limit_delay - elapsed)
        self._last_request = time.time()

    def _request(self, path: str, *, params: dict | None = None, timeout: float = 10.0) -> httpx.Response:
        """Issue a rate-limited GET request to the OpenAlex API."""
        self._rate_limit()
        request_params = dict(params or {})
        if self.email:
            request_params.setdefault("mailto", self.email)
        return httpx.get(
            f"{OPENALEX_API}{path}",
            params=request_params or None,
            headers=self.headers,
            timeout=timeout,
        )

    @staticmethod
    def _normalize_doi(doi: str) -> str:
        """Normalize a DOI by removing common URL prefixes."""
        if doi.startswith("https://doi.org/"):
            return doi[16:]
        if doi.startswith("http://doi.org/"):
            return doi[15:]
        return doi

    @staticmethod
    def _split_author_query(query: str) -> tuple[str | None, str]:
        """Parse the optional author-prefixed query form used by ingestion search."""
        author_filter: str | None = None
        search_query = query
        if query.lower().startswith("author:"):
            remainder = query[len("author:"):].strip()
            if not remainder:
                return None, ""
            if "|" in remainder:
                author, search_query = remainder.split("|", 1)
                author_filter = author.strip()
                search_query = search_query.strip()
            else:
                author_filter = remainder
                search_query = ""
        return author_filter, search_query

    def get_work_details_by_doi(self, doi: str, *, select: str = WORK_SEARCH_SELECT) -> dict | None:
        """Fetch a raw OpenAlex work payload by DOI."""
        normalized_doi = self._normalize_doi(doi)
        response = self._request(
            f"/works/doi:{normalized_doi}",
            params={"select": select},
            timeout=10.0,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def get_work_by_doi(self, doi: str) -> CitationData | None:
        """Get citation data for a DOI.

        Args:
            doi: The DOI to look up (with or without https://doi.org/ prefix)

        Returns:
            CitationData if found, None otherwise
        """
        try:
            normalized_doi = self._normalize_doi(doi)
            data = self.get_work_details_by_doi(normalized_doi)
            if data is None:
                return None
            return CitationData(
                openalex_id=data["id"],
                doi=normalized_doi,
                cited_by_count=data.get("cited_by_count", 0),
                references=[ref for ref in data.get("referenced_works", []) if ref],
            )
        except Exception as e:
            logger.warning(f"OpenAlex lookup failed for {doi}: {e}")
            return None

    def get_citing_works(self, openalex_id: str, limit: int = 100) -> list[dict]:
        """Get works that cite a given paper.

        Args:
            openalex_id: The OpenAlex ID of the paper
            limit: Maximum number of citing works to return

        Returns:
            List of work dictionaries with metadata
        """
        try:
            params = {"filter": f"cites:{openalex_id}", "per-page": min(limit, 200)}
            resp = self._request("/works", params=params, timeout=10.0)
            resp.raise_for_status()
            return resp.json().get("results", [])
        except Exception as e:
            logger.warning(f"Failed to get citing works: {e}")
            return []

    def get_references(self, openalex_id: str, limit: int = 100) -> list[dict]:
        """Get works that a paper references (its bibliography).

        Args:
            openalex_id: The OpenAlex ID of the paper
            limit: Maximum number of references to return

        Returns:
            List of work dictionaries with metadata
        """
        # OpenAlex stores references as a list of OpenAlex IDs
        # We need to fetch those works
        try:
            # First get the work to get its references
            resp = self._request(f"/works/{openalex_id}", timeout=10.0)
            resp.raise_for_status()
            work = resp.json()

            referenced_works = work.get("referenced_works", [])
            if not referenced_works:
                return []

            # Fetch details for the referenced works (up to limit)
            referenced_works = referenced_works[:limit]
            # Use filter to get multiple works at once
            ids_filter = "|".join(referenced_works)
            params = {"filter": f"openalex_id:{ids_filter}", "per-page": min(limit, 200)}
            resp = self._request("/works", params=params, timeout=10.0)
            resp.raise_for_status()
            return resp.json().get("results", [])
        except Exception as e:
            logger.warning(f"Failed to get references: {e}")
            return []

    def search_works(
        self,
        query: str | None,
        *,
        per_page: int = 25,
        high_quality: bool = True,
        year_min: int | None = None,
        year_max: int | None = None,
        sort: str | None = None,
    ) -> list[dict]:
        """Search OpenAlex works using top-level search plus hard quality filters."""
        author_filter: str | None = None
        search_query = query
        if query:
            author_filter, search_query = self._split_author_query(query)

        filters = ["is_retracted:false", "type:article|review", "has_doi:true"]
        if author_filter is not None:
            filters.append(f"raw_author_name.search:{author_filter}")
        if high_quality:
            filters.append("cited_by_count:>10")
        if year_min is not None:
            filters.append(f"from_publication_date:{year_min}-01-01")
        if year_max is not None:
            filters.append(f"to_publication_date:{year_max}-12-31")

        params = {
            "filter": ",".join(filters),
            "per-page": str(min(per_page, 200)),
            "select": WORK_SEARCH_SELECT,
        }
        if search_query:
            params["search"] = search_query
        if sort:
            params["sort"] = sort

        response = self._request("/works", params=params, timeout=15.0)
        response.raise_for_status()
        return response.json().get("results", [])

    @staticmethod
    def format_work(work: dict) -> dict:
        """Format an OpenAlex work into a simpler structure.

        Args:
            work: Raw OpenAlex work dictionary

        Returns:
            Simplified work dictionary
        """
        authors = []
        for authorship in work.get("authorships", [])[:3]:
            author = authorship.get("author", {})
            name = author.get("display_name", "")
            if name:
                authors.append(name)

        return {
            "title": work.get("title", ""),
            "authors": ", ".join(authors),
            "year": work.get("publication_year"),
            "doi": work.get("doi"),
            "cited_by_count": work.get("cited_by_count", 0),
            "openalex_id": work.get("id"),
        }
