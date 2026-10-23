"""Recon follow-up (review B2/S4/S10): The Odds API — event-level markets,
Ligue 1 totals under the correct key, UCL /scores cost, Fortuna presence.

Idempotent: probes whose results already exist in odds_api.json are skipped
(pass --redo to force re-spending credits). Replaces the hand-written
`followup_probes` block (this script performs the removal) and migrates
bodies that an earlier version mislabeled as "error".

TOA credit budget (fresh full run): <= 5
  - /events                          : 0 credits (verified by delta)
  - event odds x3 markets            : 3 credits (1 each, regions=eu)
  - Ligue 1 totals                   : 1 credit
  - UCL /scores                      : measured (expected 0)
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
EVENT_MARKETS = ("btts", "correct_score", "alternate_totals")


def summarize_event(body: dict) -> dict:
    bookmakers = sorted({bk.get("title") or bk.get("key")
                         for bk in body.get("bookmakers", [])})
    markets_seen = sorted({m.get("key")
                           for bk in body.get("bookmakers", [])
                           for m in bk.get("markets", [])})
    points = sorted({o.get("point")
                     for bk in body.get("bookmakers", [])
                     for m in bk.get("markets", []) for o in m.get("outcomes", [])
                     if o.get("point") is not None})
    n_outcomes = sum(len(m.get("outcomes", []))
                     for bk in body.get("bookmakers", [])
                     for m in bk.get("markets", []))
    return {"n_bookmakers": len(bookmakers), "bookmakers": bookmakers,
            "markets_returned": markets_seen, "points": points,
            "n_outcomes": n_outcomes}


def migrate_mislabeled(report: dict) -> dict:
    """Older runs put successful event bodies under 'error'; fix in place."""
    probes = report.get("followup", {}).get("event_odds_probes", {})
    for market, entry in probes.items():
        body = entry.get("error")
        if isinstance(body, dict) and "bookmakers" in body:
            entry.pop("error")
            entry["event"] = {
                "commence_time": body.get("commence_time"),
                "home_team": body.get("home_team"),
                "away_team": body.get("away_team"),
                **summarize_event(body),
            }
    return report


def main() -> None:
    load_env()
    register_secrets()
    if not has("ODDS_API_KEY"):
        print("ODDS_API_KEY is missing in .env — cannot verify, exiting.")
        sys.exit(2)
    only_scores = "--only" in sys.argv and "scores" in sys.argv
    redo = "--redo" in sys.argv

    session = requests.Session()
    key = os.environ["ODDS_API_KEY"]
    report = migrate_mislabeled(json.loads(OUT.read_text()))
    followup = report.setdefault("followup", {})
    used_before: int | None = None
    spent_total = 0

    def get(path: str, params: dict, label: str) -> tuple[object, int]:
        nonlocal used_before, spent_total
        params = {**params, "apiKey": key}
        time.sleep(1.0)
        try:
            resp = session.get(f"{BASE}{path}", params=params, timeout=30)
        except requests.RequestException as exc:
            print(scrub(f"{label}: REQUEST FAILED {type(exc).__name__}: {exc}"))
            return None, 0
        after = resp.headers.get("x-requests-used")
        delta = 0
        if after is not None:
            if used_before is not None:
                delta = int(after) - used_before
            used_before = int(after)
            spent_total += delta
        try:
            body = (resp.json()
                    if resp.headers.get("content-type", "").startswith("application/json")
                    else resp.text[:200])
        except ValueError:
            body = resp.text[:200]
        print(scrub(f"{label}: http={resp.status_code} credits_delta={delta} "
                    f"used_after={after} "
                    f"remaining={resp.headers.get('x-requests-remaining')}"))
        return body, delta

    # 0. Free /events call: calibration that its delta is 0 + an event id
    if not only_scores:
        events, delta = get("/sports/soccer_epl/events", {"regions": "eu"},
                            "epl events (delta calibration)")
        followup["events_call"] = {
            "n_events": len(events) if isinstance(events, list) else None,
            "credits_delta": delta,
            "note": "must be 0 — /events is free",
        }
        event_id = events[0]["id"] if events else None
        existing = followup.get("event_odds_probes", {})

        # 1. Event-level odds probes (skipped if already present)
        probes = followup.setdefault("event_odds_probes", {})
        for market in EVENT_MARKETS:
            if not redo and market in existing:
                print(f"event odds {market}: already probed, skipping")
                continue
            if not event_id:
                continue
            body, credits = get(
                f"/sports/soccer_epl/events/{event_id}/odds",
                {"regions": "eu", "markets": market, "oddsFormat": "decimal"},
                f"event odds {market}",
            )
            entry: dict = {"credits": credits}
            if isinstance(body, dict) and "bookmakers" in body:
                entry["event"] = {"commence_time": body.get("commence_time"),
                                  "home_team": body.get("home_team"),
                                  "away_team": body.get("away_team"),
                                  **summarize_event(body)}
            elif isinstance(body, dict):
                entry["error"] = body
            probes[market] = entry

        # 2. Ligue 1 totals under the correct sport key
        if redo or "ligue_one_totals" not in followup:
            body, credits = get(
                "/sports/soccer_france_ligue_one/odds",
                {"regions": "eu", "markets": "totals", "oddsFormat": "decimal"},
                "ligue one totals",
            )
            entry = {"credits": credits}
            if isinstance(body, list):
                lines = sorted({o["point"] for ev in body
                                for bk in ev.get("bookmakers", [])
                                for m in bk.get("markets", [])
                                for o in m.get("outcomes", [])
                                if o.get("point") is not None})
                bks = sorted({bk.get("title") for ev in body
                              for bk in ev.get("bookmakers", [])})
                entry.update({"n_events": len(body), "totals_lines": lines,
                              "n_bookmakers": len(bks)})
            followup["ligue_one_totals"] = entry
        else:
            print("ligue one totals: already probed, skipping")

    # 3. UCL /scores (daysFrom must be >= 1; earlier run failed with 0)
    body, credits = get("/sports/soccer_uefa_champs_league/scores",
                        {"daysFrom": 1, "daysTo": 7}, "ucl scores")
    entry = {"credits": credits, "params": {"daysFrom": 1, "daysTo": 7}}
    if isinstance(body, list):
        completed = [e for e in body if e.get("completed")]
        entry.update({
            "n_events": len(body),
            "n_completed": len(completed),
            "sample": [{"home": e.get("home_team"), "away": e.get("away_team"),
                        "scores": e.get("scores")} for e in completed[:3]],
        })
    elif isinstance(body, dict):
        entry["error"] = body
    followup["ucl_scores"] = entry

    # 4. Fortuna presence across everything recorded from region eu
    names: set[str] = set()
    for section in ("epl_market_probes", "totals_other_sports"):
        for e in report.get(section, {}).values():
            names.update(e.get("bookmakers") or [])
    for e in followup.get("event_odds_probes", {}).values():
        names.update((e.get("event") or {}).get("bookmakers") or [])
    names.update(followup.get("ligue_one_totals", {}).get("bookmakers") or [])
    fortuna = sorted(n for n in names if n and "fortuna" in n.lower())
    followup["fortuna_check"] = {
        "present_in_eu": bool(fortuna),
        "matches": fortuna,
        "all_eu_bookmakers_seen": sorted(n for n in names if n),
    }

    # 5. Drop the hand-written block, record final quota state
    report.pop("followup_probes", None)
    report["quota_state_after_followup"] = {
        "x_requests_used": used_before,
        "credits_spent_this_run": spent_total,
        "note": "monthly quota 500; reset date not verifiable via API "
                "(dashboard only)",
    }

    OUT.write_text(json.dumps(report, indent=2, default=str))
    print(f"saved {OUT} (spent {spent_total} credits this run)")


if __name__ == "__main__":
    main()
