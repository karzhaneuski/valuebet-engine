"""Comprehensive tests for news search, extraction, briefing, and freshness checks (SPEC §6)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import httpx
import pytest
import respx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from core.llm.provider import LLMError, MockLLMProvider
from core.news.brief import generate_match_brief
from core.news.extractor import (
    NewsExtractionResult,
    NewsItem,
    extract_player_news,
)
from core.news.freshness import (
    get_freshness_warnings,
    is_news_newer_than_market,
)
from core.news.search import (
    BraveSearchClient,
    FallbackSearchClient,
    SearchClient,
    SearchError,
    SearchQuotaExceededError,
    SearchResult,
    TavilySearchClient,
    check_and_increment_quota,
    has_searched_window,
    search_news_for_match,
)
from core.orm import ApiQuotaUsage, Base, QuotaCounter
from core.value.engine import match_key_player


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine)
    session = sm()
    yield session
    session.close()


# --- Search tests -----------------------------------------------------------


@respx.mock
def test_tavily_search_success(db_session: Session) -> None:
    client = TavilySearchClient(api_key="tvly-mock-key", db_session=db_session)
    mock_payload = {
        "results": [
            {
                "title": "Arsenal injury update: Saka out",
                "url": "https://example.com/saka",
                "content": "Bukayo Saka is confirmed out for the upcoming match.",
                "published_date": "2026-10-25T14:00:00Z",
            }
        ]
    }
    respx.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(200, json=mock_payload)
    )

    results = client.search("Arsenal Chelsea news", max_results=3)
    assert len(results) == 1
    assert results[0].title == "Arsenal injury update: Saka out"
    assert results[0].url == "https://example.com/saka"
    assert results[0].raw_provider == "tavily"

    # Check quota logged
    usage = db_session.scalars(select(ApiQuotaUsage)).all()
    assert len(usage) == 1
    assert usage[0].provider == "tavily"
    assert usage[0].credits == 1


def test_tavily_missing_key() -> None:
    client = TavilySearchClient(api_key="")
    with pytest.raises(SearchError, match="TAVILY_API_KEY is not set"):
        client.search("query")


@respx.mock
def test_brave_search_success(db_session: Session) -> None:
    client = BraveSearchClient(api_key="BSA-mock-key", db_session=db_session)
    mock_payload = {
        "web": {
            "results": [
                {
                    "title": "Chelsea squad news",
                    "url": "https://example.com/chelsea",
                    "description": "Enzo Fernandez is doubtful due to a knock.",
                    "page_age": "2026-10-25T12:00:00Z",
                }
            ]
        }
    }
    route = respx.get("https://api.search.brave.com/res/v1/web/search").mock(
        return_value=httpx.Response(200, json=mock_payload)
    )

    results = client.search("Chelsea news")
    assert len(results) == 1
    assert results[0].title == "Chelsea squad news"
    assert results[0].content == "Enzo Fernandez is doubtful due to a knock."
    assert results[0].raw_provider == "brave"
    assert route.calls.last.request.headers["X-Subscription-Token"] == "BSA-mock-key"


def test_fallback_search_client() -> None:
    class FailingClient(SearchClient):
        def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
            raise RuntimeError("Provider network failure")

    class SuccessfulClient(SearchClient):
        def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
            return [
                SearchResult(
                    title="Fallback success",
                    url="https://fallback.com",
                    content="Recovered via fallback",
                    raw_provider="brave",
                )
            ]

    client = FallbackSearchClient(primary=FailingClient(), secondary=SuccessfulClient())
    results = client.search("Test query")
    assert len(results) == 1
    assert results[0].title == "Fallback success"


def test_quota_tracking_and_limit(db_session: Session) -> None:
    check_and_increment_quota(
        session=db_session,
        provider="tavily",
        endpoint="/search",
        credits=5,
        max_monthly_credits=10,
    )
    counter = db_session.scalar(select(QuotaCounter).where(QuotaCounter.provider == "tavily"))
    assert counter is not None
    assert counter.credits_used == 5

    # Consuming 5 more reaches 10
    check_and_increment_quota(
        session=db_session,
        provider="tavily",
        endpoint="/search",
        credits=5,
        max_monthly_credits=10,
    )
    assert counter.credits_used == 10

    # Next attempt breaches cap and raises error
    with pytest.raises(SearchQuotaExceededError, match="Monthly search quota exceeded"):
        check_and_increment_quota(
            session=db_session,
            provider="tavily",
            endpoint="/search",
            credits=1,
            max_monthly_credits=10,
        )


def test_window_deduplication(db_session: Session) -> None:
    class MockSearch(SearchClient):
        def __init__(self) -> None:
            self.count = 0

        def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
            self.count += 1
            return [
                SearchResult(
                    title=f"Result {self.count}",
                    url="https://example.com",
                    content="Content",
                    raw_provider="tavily",
                )
            ]

    searcher = MockSearch()
    match_id = 42

    assert not has_searched_window(db_session, match_id, "T-24h")

    # First call: executes search
    res1 = search_news_for_match(
        session=db_session,
        search_client=searcher,
        match_id=match_id,
        home_team="Arsenal",
        away_team="Chelsea",
        window="T-24h",
    )
    assert len(res1) == 1
    assert searcher.count == 1
    assert has_searched_window(db_session, match_id, "T-24h")

    # Second call in same window: skipped (returns empty list, searcher not called)
    res2 = search_news_for_match(
        session=db_session,
        search_client=searcher,
        match_id=match_id,
        home_team="Arsenal",
        away_team="Chelsea",
        window="T-24h",
    )
    assert len(res2) == 0
    assert searcher.count == 1

    # Call in a different window (T-3h): executes search
    res3 = search_news_for_match(
        session=db_session,
        search_client=searcher,
        match_id=match_id,
        home_team="Arsenal",
        away_team="Chelsea",
        window="T-3h",
    )
    assert len(res3) == 1
    assert searcher.count == 2
    assert has_searched_window(db_session, match_id, "T-3h")


# --- Extraction tests -------------------------------------------------------


def test_news_extraction_success() -> None:
    expected_item = NewsItem(
        player="Bukayo Saka",
        team="Arsenal",
        status="out",
        source_url="https://bbc.com/sport/football",
        quote_summary="Saka suffered a hamstring injury and is unavailable.",
        published_at=dt.datetime(2026, 10, 25, 12, 0, tzinfo=dt.UTC),
    )
    provider = MockLLMProvider(
        default_structured=NewsExtractionResult(items=[expected_item])
    )

    search_results = [
        SearchResult(
            title="Injury report",
            url="https://bbc.com/sport/football",
            content="Saka suffered a hamstring injury.",
        )
    ]

    result = extract_player_news(provider, "Arsenal", "Liverpool", search_results)
    assert len(result.items) == 1
    assert result.items[0].player == "Bukayo Saka"
    assert result.items[0].status == "out"

    # Verify prompt injection defense wrapping
    prompt_used = provider.calls[0]["prompt"]
    assert "<untrusted_news_data>" in prompt_used
    assert "</untrusted_news_data>" in prompt_used
    assert "CRITICAL SECURITY DIRECTIVE (PROMPT INJECTION DEFENSE)" in prompt_used


def test_news_extraction_empty_inputs() -> None:
    provider = MockLLMProvider()
    result = extract_player_news(provider, "Arsenal", "Liverpool", [])
    assert result.items == []
    assert len(provider.calls) == 0


def test_news_extraction_llm_failure_handled_gracefully() -> None:
    provider = MockLLMProvider()
    provider.raise_error = LLMError("API connection timeout")

    search_results = [
        SearchResult(
            title="Report",
            url="https://example.com",
            content="Some text",
        )
    ]
    result = extract_player_news(provider, "Arsenal", "Liverpool", search_results)
    assert result.items == []


# --- Briefing tests ---------------------------------------------------------


def test_user_brief_generation() -> None:
    provider = MockLLMProvider(
        default_text=(
            "У Арсенала подтверждена потеря Букайо Сака из-за травмы подколенного сухожилия. "
            "Ливерпуль подходит к матчу в оптимальном составе без дисквалификаций."
        )
    )

    news_items = [
        NewsItem(
            player="Bukayo Saka",
            team="Arsenal",
            status="out",
            source_url="https://bbc.com",
            quote_summary="Hamstring tear in training.",
        )
    ]

    brief = generate_match_brief(provider, "Arsenal", "Liverpool", news_items)
    assert "У Арсенала подтверждена потеря Букайо Сака" in brief

    # Verify prohibition directives exist in prompt
    prompt_sent = provider.calls[0]["prompt"]
    assert "ЗАПРЕЩЕНО давать любые рекомендации по ставкам" in prompt_sent
    assert "ЗАПРЕЩЕНО выдумывать, называть или оценивать вероятности" in prompt_sent


def test_user_brief_empty_news() -> None:
    provider = MockLLMProvider()
    brief = generate_match_brief(provider, "Arsenal", "Liverpool", [])
    assert "штатных составах" in brief
    assert len(provider.calls) == 0


# --- Freshness checks -------------------------------------------------------


def test_is_news_newer_than_market() -> None:
    t_market = dt.datetime(2026, 10, 25, 14, 0, tzinfo=dt.UTC)
    t_news_after = dt.datetime(2026, 10, 25, 14, 30, tzinfo=dt.UTC)
    t_news_before = dt.datetime(2026, 10, 25, 13, 30, tzinfo=dt.UTC)

    assert is_news_newer_than_market(t_news_after, t_market) is True
    assert is_news_newer_than_market(t_news_before, t_market) is False
    assert is_news_newer_than_market(t_market, t_market) is False

    # None handling
    assert is_news_newer_than_market(None, t_market) is False
    assert is_news_newer_than_market(t_news_after, None) is False

    # Naive datetime handling
    t_naive_after = dt.datetime(2026, 10, 25, 15, 0)
    assert is_news_newer_than_market(t_naive_after, t_market) is True


def test_get_freshness_warnings() -> None:
    snapshot_time = dt.datetime(2026, 10, 25, 12, 0, tzinfo=dt.UTC)

    items = [
        # Newer and out -> warning
        NewsItem(
            player="Martin Odegaard",
            team="Arsenal",
            status="out",
            source_url="https://news.com",
            quote_summary="Ankle sprain",
            published_at=dt.datetime(2026, 10, 25, 13, 0, tzinfo=dt.UTC),
        ),
        # Newer but returning -> NO warning
        NewsItem(
            player="Jurrien Timber",
            team="Arsenal",
            status="returning",
            source_url="https://news.com",
            quote_summary="Back to training",
            published_at=dt.datetime(2026, 10, 25, 13, 0, tzinfo=dt.UTC),
        ),
        # Older and out -> NO warning (market already had time to price it)
        NewsItem(
            player="Gabriel Jesus",
            team="Arsenal",
            status="out",
            source_url="https://news.com",
            quote_summary="Knee injury",
            published_at=dt.datetime(2026, 10, 25, 10, 0, tzinfo=dt.UTC),
        ),
    ]

    warnings = get_freshness_warnings(items, snapshot_time)
    assert len(warnings) == 1
    assert "Martin Odegaard" in warnings[0]
    assert "⚡ Свежая новость: рынок мог не успеть учесть отсутствие игрока" in warnings[0]


# --- Player matching integration -------------------------------------------


def test_player_name_matching_integration() -> None:
    # Set of key players with accents / casing
    key_players = {"Kévin De Bruyne", "Erling Haaland", "Rúben Dias"}

    # Normalized match
    is_key, reason = match_key_player("kevin de bruyne", key_players, has_lineups=True)
    assert is_key is True
    assert reason == "ключевой"

    # Non-key player
    is_key, reason = match_key_player("Scott Carson", key_players, has_lineups=True)
    assert is_key is False
    assert reason == "не ключевой"

    # Missing lineups
    is_key, reason = match_key_player("Erling Haaland", None, has_lineups=False)
    assert is_key is False
    assert reason == "нет данных о составе"
