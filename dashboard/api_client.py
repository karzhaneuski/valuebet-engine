"""REST API client for Streamlit dashboard.

Strict architectural rule (SPEC v3 §9):
The dashboard NEVER accesses SQLite directly; ALL data flows through the REST API.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")


class ApiClient:
    """Client for communicating with ValueBet Engine REST API."""

    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        raw_url = base_url or os.getenv("API_BASE_URL", DEFAULT_BASE_URL)
        self.base_url = (raw_url or "http://localhost:8000").rstrip("/")
        self.token = token or os.getenv("API_SECRET_TOKEN") or os.getenv("API_TOKEN", "")

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get_health(self) -> dict[str, Any]:
        """Check API and database health (public, unauthenticated)."""
        url = f"{self.base_url}/health"
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.json()

    def get_matches(
        self,
        days: int = 7,
        league: str | None = None,
        status: str = "scheduled",
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List matches with filtering."""
        url = f"{self.base_url}/matches"
        params: dict[str, Any] = {
            "days": days,
            "status": status,
            "limit": limit,
            "offset": offset,
        }
        if league:
            params["league"] = league
        if search:
            params["search"] = search

        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, params=params, headers=self.headers)
            resp.raise_for_status()
            return resp.json()

    def get_fair_prices(self, match_id: int) -> dict[str, Any]:
        """Get fair prices and methodological caveats for a match."""
        url = f"{self.base_url}/matches/{match_id}/fair-prices"
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers=self.headers)
            resp.raise_for_status()
            return resp.json()

    def evaluate_fortuna(
        self,
        raw_text: str,
        match_id: int | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        """Evaluate manual Fortuna text lines against fair matrix."""
        url = f"{self.base_url}/fortuna-eval"
        payload: dict[str, Any] = {"raw_text": raw_text}
        if match_id is not None:
            payload["match_id"] = match_id
        if query:
            payload["query"] = query

        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, json=payload, headers=self.headers)
            resp.raise_for_status()
            return resp.json()

    def record_user_bet(
        self,
        match_id: int,
        market: str,
        outcome: str,
        odds: float,
        stake: float,
        bookmaker: str = "Fortuna",
    ) -> dict[str, Any]:
        """Record user bet."""
        url = f"{self.base_url}/user-bets"
        payload = {
            "match_id": match_id,
            "market": market,
            "outcome": outcome,
            "odds": odds,
            "stake": stake,
            "bookmaker": bookmaker,
        }
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=payload, headers=self.headers)
            resp.raise_for_status()
            return resp.json()

    def get_user_bets(
        self,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List tracked user bets."""
        url = f"{self.base_url}/user-bets"
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, params=params, headers=self.headers)
            resp.raise_for_status()
            return resp.json()

    def get_performance(self, period: str = "all") -> dict[str, Any]:
        """Get transparent portfolio performance and CLV statistics."""
        url = f"{self.base_url}/performance"
        params = {"period": period}
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, params=params, headers=self.headers)
            resp.raise_for_status()
            return resp.json()
