"""Recon: The Odds API free tier — markets, lines, bookmakers, credit costs.

Probes (all real requests, region eu):
- /sports: find sport keys for top-5 leagues + UCL;
- for soccer_epl: each candidate market separately (h2h, totals, spreads,
  btts, alternate_totals, correct_score) — which are entitled on free tier;
- for the other 5 sports: totals (the market SPEC needs);
- /events for soccer_epl: cheap upcoming-event listing;
- credit cost of every call measured from x-requests-used header deltas.

Writes reports/recon/odds_api.json (no secrets). Upcoming events are saved
for the search-API recon script. Phase 0 recon only.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from envutil import has, load_env, register_secrets, scrub  # noqa: E402

OUT = Path("reports/recon/odds_api.json")
BASE = "https://api.the-odds-api.com/v4"
WANTED = {
    "soccer_epl": "Premier League",
    "soccer_spain_la_liga": "La Liga",
    "soccer_italy_serie_a": "Serie A",
    "soccer_germany_bundesliga": "Bundesliga",
    "soccer_france_ligue_1": "Ligue 1",
    "soccer_uefa_champs_league": "UEFA Champions League",
}
PROBE_MARKETS = ["h2h", "totals", "spreads", "btts", "alternate_totals",
                 "correct_score"]


def main() -> None:
    load_env()
    register_secrets()
    if not has("ODDS_API_KEY"):
        print("ODDS_API_KEY is missing in .env — cannot verify, exiting.")
        sys.exit(2)

    session = requests.Session()
    report: dict = {"calls": []}

    def get(path: str, params: dict, label: str) -> tuple[dict | list, int]:
        """Returns (json body, credits spent). Never logs the key."""
        params = {**params, "apiKey": os.environ["ODDS_API_KEY"]}
        time.sleep(1.0)
        try:
            resp = session.get(f"{BASE}{path}", params=params, timeout=30)
            used_after = resp.headers.get("x-requests-used")
            remaining = resp.headers.get("x-requests-remaining")
            credits = 0
            try:
                credits = int(used_after or 0) - int(report["calls"][-1]["used_after"]
                                                     or 0) if report["calls"] else int(
                    used_after or 0)
            except (TypeError, ValueError):
                credits = -1
            try:
                body = resp.json()
            except ValueError:
                body = {"raw": resp.text[:200]}
            record = {
                "label": label, "http": resp.status_code,
                "used_after": used_after, "remaining_after": remaining,
                "credits_delta": credits,
            }
            if resp.status_code != 200:
                record["error"] = body
            report["calls"].append(record)
            status = "OK" if resp.status_code == 200 else f"error={body}"
            print(scrub(f"{label}: http={resp.status_code} {status} "
                        f"credits={credits} remaining={remaining}"))
            return body, credits
        except requests.RequestException as exc:
            print(scrub(f"{label}: REQUEST FAILED {type(exc).__name__}: {exc}"))
            report["calls"].append({"label": label, "failed": True})
            return {}, 0

    # 1. Sport keys
    sports, _ = get("/sports", {}, "list sports")
    keys = sorted(s["key"] for s in sports) if isinstance(sports, list) else []
    report["sport_keys_found"] = {k: (k in keys) for k in WANTED}
    print("wanted keys present:", report["sport_keys_found"])

    def summarize_odds(body) -> dict:
        """Extract bookmakers, totals lines and outcome names from an odds body."""
        bookmakers: set[str] = set()
        totals_points: set[float] = set()
        outcome_names: set[str] = set()
        for ev in body if isinstance(body, list) else []:
            for bk in ev.get("bookmakers", []):
                bookmakers.add(bk.get("title") or bk.get("key"))
                for mkt in bk.get("markets", []):
                    if mkt.get("key") == "totals":
                        for o in mkt.get("outcomes", []):
                            if o.get("point") is not None:
                                totals_points.add(float(o["point"]))
                    for o in mkt.get("outcomes", []):
                        if o.get("name"):
                            outcome_names.add(str(o["name"]))
        return {
            "n_events": len(body) if isinstance(body, list) else 0,
            "bookmakers": sorted(b for b in bookmakers if b),
            "totals_points": sorted(totals_points),
            "outcome_names_sample": sorted(outcome_names)[:12],
        }

    # 2. Market probes on soccer_epl
    report["epl_market_probes"] = {}
    for market in PROBE_MARKETS:
        body, credits = get(
            "/sports/soccer_epl/odds",
            {"regions": "eu", "markets": market, "oddsFormat": "decimal"},
            f"epl {market} (eu)",
        )
        entry = {"credits": credits}
        if isinstance(body, list):
            entry.update(summarize_odds(body))
        report["epl_market_probes"][market] = entry

    # 3. totals for the other sports
    report["totals_other_sports"] = {}
    for key in WANTED:
        if key == "soccer_epl":
            continue
        body, credits = get(
            f"/sports/{key}/odds",
            {"regions": "eu", "markets": "totals", "oddsFormat": "decimal"},
            f"{key} totals (eu)",
        )
        entry = {"credits": credits}
        if isinstance(body, list):
            entry.update(summarize_odds(body))
        report["totals_other_sports"][key] = entry

    # 4. Events endpoint (cheap upcoming listing) on epl
    events, credits = get("/sports/soccer_epl/events", {}, "epl events")
    if isinstance(events, list):
        report["events_epl"] = {
            "credits": credits,
            "n_events": len(events),
            "sample": [
                {"home": e.get("home_team"), "away": e.get("away_team"),
                 "commence": e.get("commence_time"), "id": e.get("id")}
                for e in events[:8]
            ],
        }

    # 5. Combined markets on one call (cost formula check)
    body, credits = get(
        "/sports/soccer_epl/odds",
        {"regions": "eu", "markets": "h2h,totals", "oddsFormat": "decimal"},
        "epl h2h+totals (eu)",
    )
    report["combined_call"] = {"credits": credits}
    if isinstance(body, list):
        report["combined_call"].update(summarize_odds(body))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str))
    print(f"saved {OUT}")


if __name__ == "__main__":
    main()
