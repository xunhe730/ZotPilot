"""OpenAlex API client for citation data."""
import logging
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

OPENALEX_API = "https://api.openalex.org"


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

    def get_work_by_doi(self, doi: str) -> CitationData | None:
        """Get citation data for a DOI.

        Args:
            doi: The DOI to look up (with or without https://doi.org/ prefix)

        Returns:
            CitationData if found, None otherwise
        """
        self._rate_limit()

        # Normalize DOI - remove common prefixes
        if doi.startswith("https://doi.org/"):
            doi = doi[16:]
        elif doi.startswith("http://doi.org/"):
            doi = doi[15:]

        try:
            url = f"{OPENALEX_API}/works/doi:{doi}"
            resp = httpx.get(url, headers=self.headers, timeout=10.0)

            if resp.status_code == 404:
                return None
            resp.raise_for_status()

            data = resp.json()
            return CitationData(
                openalex_id=data["id"],
                doi=doi,
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
        self._rate_limit()

        try:
            url = f"{OPENALEX_API}/works"
            params = {"filter": f"cites:{openalex_id}", "per-page": min(limit, 200)}
            resp = httpx.get(url, params=params, headers=self.headers, timeout=10.0)
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
        self._rate_limit()

        # OpenAlex stores references as a list of OpenAlex IDs
        # We need to fetch those works
        try:
            # First get the work to get its references
            url = f"{OPENALEX_API}/works/{openalex_id}"
            resp = httpx.get(url, headers=self.headers, timeout=10.0)
            resp.raise_for_status()
            work = resp.json()

            referenced_works = work.get("referenced_works", [])
            if not referenced_works:
                return []

            # Fetch details for the referenced works (up to limit)
            referenced_works = referenced_works[:limit]
            self._rate_limit()

            # Use filter to get multiple works at once
            ids_filter = "|".join(referenced_works)
            url = f"{OPENALEX_API}/works"
            params = {"filter": f"openalex_id:{ids_filter}", "per-page": min(limit, 200)}
            resp = httpx.get(url, params=params, headers=self.headers, timeout=10.0)
            resp.raise_for_status()
            return resp.json().get("results", [])
        except Exception as e:
            logger.warning(f"Failed to get references: {e}")
            return []

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
