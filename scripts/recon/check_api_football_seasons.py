"""Recon follow-up: API-Football free-plan season window and endpoints.

The main probe showed: season 2026 is blocked ("Free plans do not have access
to this season, try from 2022 to 2024"). This script pins the boundary down
(2024 vs 2025) and verifies that lineups / injuries / odds endpoints work at
all on an allowed season. Appends findings to reports/recon/api_football.json.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from envutil import has, load_env, register_secrets, scrub  # noqa: E402

OUT = Path("reports/recon/api_football.json")
BASE = "https://v3.football.api-sports.io"


def main() -> None:
    load_env()
    register_secrets()
    if not has("API_FOOTBALL_KEY"):
        print("API_FOOTBALL_KEY is missing in .env — cannot verify, exiting.")
        sys.exit(2)

    session = requests.Session()
    session.headers["x-apisports-key"] = os.environ["API_FOOTBALL_KEY"]

    def call(path: str, params: dict, label: str) -> dict:
        time.sleep(6.5)  # stay well under the 10 req/min limit
        try:
            resp = session.get(f"{BASE}{path}", params=params, timeout=30)
            body = resp.json()
        except (requests.RequestException, ValueError) as exc:
            print(scrub(f"{label}: REQUEST FAILED {type(exc).__name__}: {exc}"))
            return {}
        err = body.get("errors") or None
        print(scrub(f"{label}: http={resp.status_code} "
                    f"{'OK' if not err else err} results={body.get('results')}"))
        return body

    report = json.loads(OUT.read_text())
    probe: dict = {"probed_on": str(date.today())}

    # 1. Season window boundary: 2024-25 (mid-April 2025) vs 2025-26 (April 2026)
    for season, from_, to_ in [(2024, "2025-04-01", "2025-04-08"),
                               (2025, "2026-04-01", "2026-04-08")]:
        body = call("/fixtures",
                    {"league": 39, "season": season, "from": from_, "to": to_},
                    f"season {season} fixtures")
        probe[f"season_{season}_fixtures"] = {
            "errors": body.get("errors") or None,
            "n_results": body.get("results", 0),
        }

    # 2. Injuries for the blocked current season (league-level, no fixture ids)
    body = call("/injuries",
                {"league": 39, "season": 2026, "from": "2026-10-01", "to": "2026-10-30"},
                "injuries season 2026")
    probe["injuries_season_2026"] = {
        "errors": body.get("errors") or None,
        "n_results": body.get("results", 0),
    }

    # 3. Endpoints on the allowed 2024-25 season
    body = call("/fixtures",
                {"league": 39, "season": 2024, "from": "2025-04-01", "to": "2025-04-03",
                 "status": "FT"},
                "finished fixture 2024-25")
    fixture = None
    if body.get("response"):
        last = body["response"][-1]
        fixture = {
            "fixture_id": last["fixture"]["id"],
            "date": last["fixture"]["date"],
            "home": last["teams"]["home"]["name"],
            "away": last["teams"]["away"]["name"],
        }
        probe["allowed_season_sample_fixture"] = fixture

    if fixture:
        body = call("/fixtures/lineups", {"fixture": fixture["fixture_id"]},
                    "lineups 2024-25")
        probe["lineups_2024"] = [
            {"team": t["team"]["name"], "formation": t.get("formation"),
             "n_start_xi": len(t.get("startXI", [])),
             "n_substitutes": len(t.get("substitutes", []))}
            for t in body.get("response", [])
        ]
        body = call("/injuries", {"fixture": fixture["fixture_id"]},
                    "injuries fixture 2024-25")
        probe["injuries_fixture_2024"] = {
            "errors": body.get("errors") or None,
            "count": len(body.get("response", [])),
        }
        body = call("/odds", {"fixture": fixture["fixture_id"]}, "odds fixture 2024-25")
        bets: set[str] = set()
        bookmakers: set[str] = set()
        for entry in body.get("response", []):
            for bk in entry.get("bookmakers", []):
                bookmakers.add(bk["name"])
                for bet in bk.get("bets", []):
                    bets.add(bet["name"])
        probe["odds_fixture_2024"] = {
            "errors": body.get("errors") or None,
            "n_bookmakers": len(bookmakers),
            "bookmakers": sorted(bookmakers),
            "bet_names": sorted(bets),
        }

    report["season_probe"] = probe
    OUT.write_text(json.dumps(report, indent=2, default=str))
    print(f"saved {OUT}")


if __name__ == "__main__":
    main()
