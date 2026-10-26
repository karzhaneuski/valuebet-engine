"""News extraction, search, freshness checks, and user briefing module."""

from core.news.brief import generate_match_brief
from core.news.extractor import (
    NewsExtractionResult,
    NewsItem,
    PlayerStatus,
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
    has_searched_window,
    record_search_window,
    search_news_for_match,
)

__all__ = [
    "BraveSearchClient",
    "FallbackSearchClient",
    "NewsExtractionResult",
    "NewsItem",
    "PlayerStatus",
    "SearchClient",
    "SearchError",
    "SearchQuotaExceededError",
    "SearchResult",
    "TavilySearchClient",
    "extract_player_news",
    "generate_match_brief",
    "get_freshness_warnings",
    "has_searched_window",
    "is_news_newer_than_market",
    "record_search_window",
    "search_news_for_match",
]
