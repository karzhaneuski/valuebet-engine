"""Search client with quota tracking, Tavily/Brave fallback, and window deduplication.

SPEC §6, decisions S5:
- Tavily API is primary search client; Brave Search is fallback.
- All external calls go through core/http.py:safe_request.
- Monthly quota tracked in api_quota_usage and quota_counters tables.
- Window deduplication: searches for a match occur strictly within T-24h and T-3h windows.
  Repeated searches within the same window are skipped to preserve quota.
"""

from __future__ import annotations

import datetime as dt
import logging
from abc import ABC, abstractmethod
from typing import Any

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import Secrets
from core.http import HttpError, safe_request
from core.orm import ApiQuotaUsage, QuotaCounter

logger = logging.getLogger(__name__)


class SearchQuotaExceededError(RuntimeError):
    """Raised when provider monthly quota is exhausted."""


class SearchError(RuntimeError):
    """Base error for news search failures."""


class SearchResult(BaseModel):
    """Normalized search result item from any provider."""

    title: str = ""
    url: str = ""
    content: str = ""
    published_date: str | None = None
    raw_provider: str = Field(default="unknown")


def get_current_period_utc() -> str:
    """Format current UTC month as YYYY-MM."""
    return dt.datetime.now(dt.UTC).strftime("%Y-%m")


def check_and_increment_quota(
    session: Session,
    provider: str,
    endpoint: str,
    credits: int = 1,
    max_monthly_credits: int | None = None,
) -> None:
    """Track quota consumption in api_quota_usage and quota_counters.

    Raises SearchQuotaExceededError if monthly cap would be breached.
    """
    now = dt.datetime.now(dt.UTC)
    period = get_current_period_utc()

    # Query or create counter for this provider and period
    counter = session.scalar(
        select(QuotaCounter).where(
            QuotaCounter.provider == provider,
            QuotaCounter.period == period,
        )
    )

    if counter is None:
        counter = QuotaCounter(
            provider=provider,
            period=period,
            credits_used=0,
            updated_at_utc=now,
        )
        session.add(counter)

    if max_monthly_credits is not None and counter.credits_used + credits > max_monthly_credits:
        msg = (
            f"Monthly search quota exceeded for {provider}: "
            f"{counter.credits_used}/{max_monthly_credits} used."
        )
        raise SearchQuotaExceededError(msg)

    counter.credits_used += credits
    counter.updated_at_utc = now

    usage_record = ApiQuotaUsage(
        provider=provider,
        endpoint=endpoint,
        credits=credits,
        remaining_header=None,
        status="settled",
        created_at_utc=now,
    )
    session.add(usage_record)
    session.flush()


def has_searched_window(
    session: Session,
    match_id: int,
    window: str,
) -> bool:
    """Check if news search has already been executed for match in specified window."""
    endpoint_marker = f"news_search/match_{match_id}_{window}"
    existing = session.scalar(
        select(ApiQuotaUsage.id).where(
            ApiQuotaUsage.endpoint == endpoint_marker,
            ApiQuotaUsage.status == "settled",
        )
    )
    return existing is not None


def record_search_window(
    session: Session,
    provider: str,
    match_id: int,
    window: str,
) -> None:
    """Record that a news search window has been satisfied for a match without charging credits."""
    endpoint_marker = f"news_search/match_{match_id}_{window}"
    now = dt.datetime.now(dt.UTC)
    usage = ApiQuotaUsage(
        provider=provider,
        endpoint=endpoint_marker,
        credits=0,
        status="settled",
        created_at_utc=now,
    )
    session.add(usage)
    session.flush()


class SearchClient(ABC):
    """Abstract search client interface."""

    @abstractmethod
    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """Perform web search and return structured search results."""


class TavilySearchClient(SearchClient):
    """Tavily search API client (primary)."""

    def __init__(
        self,
        api_key: str | None = None,
        db_session: Session | None = None,
        max_monthly_quota: int = 1000,
        timeout: float = 15.0,
    ) -> None:
        self.api_key = api_key if api_key is not None else Secrets().tavily_api_key
        self.db_session = db_session
        self.max_monthly_quota = max_monthly_quota
        self.timeout = timeout
        self.endpoint_url = "https://api.tavily.com/search"

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        if not self.api_key:
            raise SearchError("TAVILY_API_KEY is not set.")

        if self.db_session is not None:
            check_and_increment_quota(
                session=self.db_session,
                provider="tavily",
                endpoint="/search",
                credits=1,
                max_monthly_credits=self.max_monthly_quota,
            )

        payload = {
            "api_key": self.api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_answer": False,
        }

        with httpx.Client(timeout=self.timeout) as client:
            try:
                resp = safe_request(
                    client=client,
                    method="POST",
                    url=self.endpoint_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                data: dict[str, Any] = resp.json()
            except HttpError as exc:
                raise SearchError(f"Tavily search HTTP error: {exc}") from exc
            except Exception as exc:
                raise SearchError(f"Tavily search connection error: {exc}") from exc

        results_data = data.get("results", [])
        results: list[SearchResult] = []
        for r in results_data:
            results.append(
                SearchResult(
                    title=str(r.get("title", "")),
                    url=str(r.get("url", "")),
                    content=str(r.get("content", "")),
                    published_date=r.get("published_date"),
                    raw_provider="tavily",
                )
            )
        return results


class BraveSearchClient(SearchClient):
    """Brave Web Search API client (fallback)."""

    def __init__(
        self,
        api_key: str | None = None,
        db_session: Session | None = None,
        max_monthly_quota: int = 2000,
        timeout: float = 15.0,
    ) -> None:
        self.api_key = api_key if api_key is not None else Secrets().brave_search_api_key
        self.db_session = db_session
        self.max_monthly_quota = max_monthly_quota
        self.timeout = timeout
        self.endpoint_url = "https://api.search.brave.com/res/v1/web/search"

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        if not self.api_key:
            raise SearchError("BRAVE_SEARCH_API_KEY is not set.")

        if self.db_session is not None:
            check_and_increment_quota(
                session=self.db_session,
                provider="brave",
                endpoint="/res/v1/web/search",
                credits=1,
                max_monthly_credits=self.max_monthly_quota,
            )

        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self.api_key,
        }
        params = {
            "q": query,
            "count": str(max_results),
        }

        with httpx.Client(timeout=self.timeout) as client:
            try:
                resp = safe_request(
                    client=client,
                    method="GET",
                    url=self.endpoint_url,
                    params=params,
                    headers=headers,
                )
                data: dict[str, Any] = resp.json()
            except HttpError as exc:
                raise SearchError(f"Brave search HTTP error: {exc}") from exc
            except Exception as exc:
                raise SearchError(f"Brave search connection error: {exc}") from exc

        web_results = data.get("web", {}).get("results", [])
        results: list[SearchResult] = []
        for r in web_results:
            results.append(
                SearchResult(
                    title=str(r.get("title", "")),
                    url=str(r.get("url", "")),
                    content=str(r.get("description", "")),
                    published_date=r.get("page_age"),
                    raw_provider="brave",
                )
            )
        return results


class FallbackSearchClient(SearchClient):
    """Tries primary search client; falls back to secondary on failure or quota exhaustion."""

    def __init__(self, primary: SearchClient, secondary: SearchClient) -> None:
        self.primary = primary
        self.secondary = secondary

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        try:
            return self.primary.search(query, max_results=max_results)
        except Exception as exc:
            logger.warning(
                "Primary search provider failed (%s), falling back to secondary",
                exc,
            )
            return self.secondary.search(query, max_results=max_results)


def search_news_for_match(
    session: Session,
    search_client: SearchClient,
    match_id: int,
    home_team: str,
    away_team: str,
    window: str,
    max_results: int = 5,
) -> list[SearchResult]:
    """Execute news search for match with window deduplication.

    Returns empty list if search has already been performed in this window.
    """
    if has_searched_window(session, match_id, window):
        logger.info(
            "Match %s news search already completed for window %s. Skipping.",
            match_id,
            window,
        )
        return []

    query = f"{home_team} {away_team} injury news lineup"
    results = search_client.search(query, max_results=max_results)

    provider_name = "tavily"
    if results and results[0].raw_provider != "unknown":
        provider_name = results[0].raw_provider

    record_search_window(session, provider_name, match_id, window)
    session.commit()
    return results
