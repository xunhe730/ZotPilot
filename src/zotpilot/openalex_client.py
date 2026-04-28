"""OpenAlex API client for citation data."""
import logging
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

OPENALEX_API = "https://api.openalex.org"
WORK_SEARCH_SELECT = (
    "id,doi,title,display_name,publication_year,cited_by_count,type,"
    "is_retracted,authorships,primary_location,best_oa_location,"
    "abstract_inverted_index,open_access,ids,relevance_score"
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
            sleep_time = self._rate_limit_delay - elapsed
            logger.debug("Rate limit sleep: %.2fs", sleep_time)
            time.sleep(sleep_time)
        self._last_request = time.time()

    def _request(self, path: str, *, params: dict | None = None, timeout: float = 10.0) -> httpx.Response:
        """Issue a rate-limited GET request to the OpenAlex API."""
        request_params = dict(params or {})
        if self.email:
            request_params.setdefault("mailto", self.email)

        max_retries = 3
        backoff = 1.0

        response: httpx.Response | None = None
        for attempt in range(max_retries + 1):
            self._rate_limit()
            try:
                response = httpx.get(
                    f"{OPENALEX_API}{path}",
                    params=request_params or None,
                    headers=self.headers,
                    timeout=timeout,
                )
            except httpx.RequestError as exc:
                # Covers ConnectError (incl. SSL EOF on first TLS handshake),
                # ReadError, TimeoutException, and other transport-level faults.
                if attempt < max_retries:
                    logger.warning(
                        "OpenAlex network error (%s), retry %d/%d in %.1fs",
                        type(exc).__name__,
                        attempt + 1,
                        max_retries,
                        backoff,
                    )
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                logger.error(
                    "OpenAlex network error (%s), all %d retries exhausted",
                    type(exc).__name__,
                    max_retries,
                )
                raise

            if response.status_code == 429:
                if attempt < max_retries:
                    logger.warning(
                        "Rate limited (429), retry %d/%d in %.1fs",
                        attempt + 1,
                        max_retries,
                        backoff,
                    )
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                logger.error("Rate limited (429), all %d retries exhausted", max_retries)

            return response

        # Unreachable (loop always returns), satisfies type checker
        return response  # type: ignore[return-value]

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
        min_citations: int | None = None,
        concepts: list[str] | None = None,
        institutions: list[str] | None = None,
        source: str | None = None,
        oa_only: bool = False,
        type_filter: str | None = None,
        year_min: int | None = None,
        year_max: int | None = None,
        sort: str | None = None,
        cursor: str | None = None,
    ) -> dict:
        """Search OpenAlex works using top-level search plus quality filters.

        Returns a dict: {"results": [...], "next_cursor": str | None, "total_count": int}
        """
        author_filter: str | None = None
        search_query = query
        if query:
            author_filter, search_query = self._split_author_query(query)

        filters = ["is_retracted:false", "has_doi:true"]
        if type_filter:
            filters.append(f"type:{type_filter}")
        else:
            filters.append("type:article|review")

        if oa_only:
            filters.append("open_access.is_oa:true")
        if min_citations is not None:
            filters.append(f"cited_by_count:>{min_citations}")
        if source:
            filters.append(f"primary_location.source.id:{source}")
        if concepts:
            filters.append(f"concepts.id:{'|'.join(concepts)}")
        if institutions:
            filters.append(f"institutions.id:{'|'.join(institutions)}")

        if author_filter is not None:
            filters.append(f"raw_author_name.search:{author_filter}")
        if year_min is not None:
            filters.append(f"from_publication_date:{year_min}-01-01")
        if year_max is not None:
            filters.append(f"to_publication_date:{year_max}-12-31")

        params = {
            "filter": ",".join(filters),
            "per-page": str(min(per_page, 200)),
            "select": WORK_SEARCH_SELECT,
        }
        if cursor is not None:
            params["cursor"] = cursor
        if search_query:
            params["search"] = search_query
        if sort:
            params["sort"] = sort

        response = self._request("/works", params=params, timeout=15.0)
        response.raise_for_status()
        data = response.json()
        return {
            "results": data.get("results", []),
            "next_cursor": data.get("meta", {}).get("next_cursor"),
            "total_count": data.get("meta", {}).get("count", 0),
        }

    def get_related_works(self, openalex_id: str, limit: int = 20) -> list[dict]:
        """获取相关论文，用于 seed-expansion 搜索策略。"""
        try:
            params = {"filter": f"related_to:{openalex_id}", "per-page": min(limit, 200)}
            resp = self._request("/works", params=params, timeout=10.0)
            resp.raise_for_status()
            return resp.json().get("results", [])
        except Exception as e:
            logger.warning(f"Failed to get related works: {e}")
            return []

    def resolve_concept(self, name: str) -> str | None:
        """自然语言概念名 → OpenAlex concept ID。调用 /concepts?search=name。"""
        try:
            resp = self._request("/concepts", params={"search": name, "per-page": 1}, timeout=5.0)
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if results:
                return results[0].get("id")
        except Exception as e:
            logger.debug(f"Concept resolve failed for {name}: {e}")
        return None

    def resolve_institution(self, name: str) -> str | None:
        """机构名 → OpenAlex institution ID。调用 /institutions?search=name。"""
        try:
            resp = self._request("/institutions", params={"search": name, "per-page": 1}, timeout=5.0)
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if results:
                return results[0].get("id")
        except Exception as e:
            logger.debug(f"Institution resolve failed for {name}: {e}")
        return None

    def resolve_source(self, name: str) -> str | None:
        """期刊/会议/来源名 → OpenAlex source ID。调用 /sources?search=name。

        支持常见别名如 "CVPR" / "NeurIPS" / "IEEE TPAMI" / "Nature"。
        返回形如 "https://openalex.org/S123456789"。
        """
        try:
            resp = self._request(
                "/sources",
                params={"search": name, "per-page": 1, "select": "id,display_name"},
                timeout=5.0,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if results:
                return results[0].get("id")
        except Exception as e:
            logger.debug(f"Source resolve failed for {name}: {e}")
        return None

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
