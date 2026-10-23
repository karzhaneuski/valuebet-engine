"""Recon: API-Football free tier — real limits, season 2026 access.

Checks (all with real requests):
- /status: plan and the actual daily request limit,
- fixtures for the next 7 days for the top-5 leagues + UCL, season 2026
  (proves current-season access),
- lineups for a recently finished fixture,
- injuries (fixture-level and league-window),
- odds: which bet types are visible to the plan, incl. exact score.

Writes reports/recon/api_football.json (no secrets). Upcoming fixtures are
saved for the search-API recon script. Phase 0 recon only.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from envutil import has, load_env, register_secrets, scrub  # noqa: E402

OUT = Path("reports/recon/api_football.json")
BASE = "https://v3.football.api-sports.io"
LEAGUES = {
    39: "Premier League",
    140: "La Liga",
    135: "Serie A",
    78: "Bundesliga",
    61: "Ligue 1",
    2: "UEFA Champions League",
}
SEASON = 2026
TODAY = date.today()


def main() -> None:
    load_env()
    register_secrets()
    if not has("API_FOOTBALL_KEY"):
        print("API_FOOTBALL_KEY is missing in .env — cannot verify, exiting.")
        sys.exit(2)

    session = requests.Session()
    session.headers["x-apisports-key"] = os.environ["API_FOOTBALL_KEY"]
    report: dict = {"run_date": str(TODAY), "requests": []}

    def call(path: str, params: dict, label: str) -> dict:
        time.sleep(1.1)
        try:
            resp = session.get(f"{BASE}{path}", params=params, timeout=30)
            body = resp.json()
        except (requests.RequestException, ValueError) as exc:
            print(scrub(f"{label}: REQUEST FAILED {type(exc).__name__}: {exc}"))
            report["requests"].append({"label": label, "path": path, "failed": True})
            return {}
        record = {
            "label": label,
            "path": path,
            "http": resp.status_code,
            "api_errors": body.get("errors") or None,
            "results": body.get("results"),
            # per-MINUTE rate limit (10/min on the free plan); the daily
            # limit (100/day) comes from /status -> report["status"]
            "ratelimit_limit_per_min": resp.headers.get("x-ratelimit-limit"),
        }
        report["requests"].append(record)
        status = "OK" if not body.get("errors") else f"errors={body.get('errors')}"
        print(scrub(f"{label}: http={resp.status_code} {status} "
                    f"results={body.get('results')}"))
        return body

    # 1. Account status: plan + factual daily limit
    status_body = call("/status", {}, "plan status")
    if status_body.get("response"):
        r0 = status_body["response"]
        report["status"] = {
            "plan": r0.get("subscription", {}).get("plan"),
            "daily_limit": r0.get("requests", {}).get("limit_day"),
        }

    # 2. Fixtures next 7 days, season 2026, per league
    upcoming: list[dict] = []
    report["leagues"] = []
    for league_id, name in LEAGUES.items():
        body = call(
            "/fixtures",
            {"league": league_id, "season": SEASON,
             "from": str(TODAY), "to": str(TODAY + timedelta(days=7))},
            f"fixtures 7d {name}",
        )
        fixtures = body.get("response", [])
        samples = [
            {
                "fixture_id": f["fixture"]["id"],
                "date": f["fixture"]["date"],
                "home": f["teams"]["home"]["name"],
                "away": f["teams"]["away"]["name"],
                "status": f["fixture"]["status"]["short"],
            }
            for f in fixtures[:5]
        ]
        league_name_resp = fixtures[0]["league"]["name"] if fixtures else None
        report["leagues"].append({
            "id": league_id,
            "name_expected": name,
            "name_returned": league_name_resp,
            "season": SEASON,
            "n_fixtures_next7": len(fixtures),
            "samples": samples,
        })
        upcoming.extend(samples)

    # 3. A recently finished fixture for lineups/injuries/odds
    finished: dict = {}
    for league_id, name in list(LEAGUES.items())[:2]:
        body = call(
            "/fixtures",
            {"league": league_id, "season": SEASON,
             "from": str(TODAY - timedelta(days=7)), "to": str(TODAY), "status": "FT"},
            f"finished fixtures {name}",
        )
        if body.get("response"):
            last = body["response"][-1]
            finished = {
                "league": name,
                "fixture_id": last["fixture"]["id"],
                "date": last["fixture"]["date"],
                "home": last["teams"]["home"]["name"],
                "away": last["teams"]["away"]["name"],
            }
            break
    report["finished_fixture"] = finished

    # 4. Lineups for that fixture
    if finished:
        body = call("/fixtures/lineups", {"fixture": finished["fixture_id"]},
                    "lineups (finished fixture)")
        report["lineups"] = [
            {
                "team": t["team"]["name"],
                "coach": t.get("coach", {}).get("name"),
                "formation": t.get("formation"),
                "n_start_xi": len(t.get("startXI", [])),
                "n_substitutes": len(t.get("substitutes", [])),
            }
            for t in body.get("response", [])
        ]

        # 5. Injuries: fixture-level and league-window
        body = call("/injuries", {"fixture": finished["fixture_id"]},
                    "injuries (fixture)")
        report["injuries_fixture"] = {
            "count": len(body.get("response", [])),
            "samples": [
                {"player": i["player"]["name"], "type": i["player"]["type"],
                 "reason": i["player"]["reason"]}
                for i in body.get("response", [])[:3]
            ],
        }
        body = call(
            "/injuries",
            {"league": 39, "season": SEASON,
             "from": str(TODAY - timedelta(days=3)), "to": str(TODAY + timedelta(days=7))},
            "injuries (PL window)",
        )
        report["injuries_league_window"] = {
            "count": len(body.get("response", [])),
            "samples": [
                {"player": i["player"]["name"], "team": i["team"]["name"],
                 "reason": i["player"]["reason"]}
                for i in body.get("response", [])[:3]
            ],
        }

        # 6. Odds for that fixture: which bet types, bookmakers
        body = call("/odds/bets", {}, "odds bet types")
        report["odds_bet_types_visible"] = sorted(
            {b["name"] for b in body.get("response", [])}
        )
        body = call("/odds", {"fixture": finished["fixture_id"]}, "odds (fixture)")
        bets: set[str] = set()
        bookmakers: list[str] = []
        for entry in body.get("response", []):
            for bk in entry.get("bookmakers", []):
                bookmakers.append(bk["name"])
                for bet in bk.get("bets", []):
                    bets.add(bet["name"])
        report["odds_fixture"] = {
            "n_bookmakers": len(set(bookmakers)),
            "bookmakers": sorted(set(bookmakers)),
            "bet_names": sorted(bets),
        }

    report["upcoming_for_search"] = upcoming
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str))
    print(f"saved {OUT}")


if __name__ == "__main__":
    main()
