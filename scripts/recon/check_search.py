"""Recon: news search APIs (Tavily, optionally Brave) on real upcoming matches.

Takes 3 nearest upcoming events from reports/recon/odds_api.json (real
fixtures from The Odds API), runs a "<team> injury news team news" query on
each search provider, and saves result quality evidence. Also saves one
article snippet as input for the Gemini structured-output probe.

Writes reports/recon/search.json and reports/recon/news_sample.json (no secrets).
Phase 0 recon only.
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

ODDS_JSON = Path("reports/recon/odds_api.json")
OUT = Path("reports/recon/search.json")
SAMPLE_OUT = Path("reports/recon/news_sample.json")


def pick_events() -> list[dict]:
    data = json.loads(ODDS_JSON.read_text())
    events = data.get("events_epl", {}).get("sample", [])
    # nearest 3 by commence time
    events = sorted(events, key=lambda e: e.get("commence") or "")
    return events[:3]


def tavily_search(session: requests.Session, query: str) -> dict:
    key = os.environ["TAVILY_API_KEY"]
    body = {
        "query": query,
        "search_depth": "basic",
        "max_results": 5,
        "include_answer": False,
    }
    try:
        resp = session.post(
            "https://api.tavily.com/search",
            json=body,
            headers={"Authorization": f"Bearer {key}"},
            timeout=30,
        )
        if resp.status_code in (401, 403):
            # fall back to the legacy body auth
            resp = session.post(
                "api.tavily.com/search".join(["https://", ""]),
                json={**body, "api_key": key},
                timeout=30,
            )
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if resp.status_code != 200:
        return {"ok": False, "http": resp.status_code, "error": data}
    results = [
        {"title": r.get("title"), "url": r.get("url"),
         "content_snippet": (r.get("content") or "")[:300], "score": r.get("score")}
        for r in data.get("results", [])
    ]
    return {"ok": True, "http": resp.status_code, "results": results}


def brave_search(session: requests.Session, query: str) -> dict:
    key = os.environ["BRAVE_SEARCH_API_KEY"]
    try:
        resp = session.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": 5},
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
            timeout=30,
        )
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if resp.status_code != 200:
        return {"ok": False, "http": resp.status_code, "error": data}
    web = data.get("web", {}).get("results", [])
    results = [
        {"title": r.get("title"), "url": r.get("url"),
         "content_snippet": (r.get("description") or "")[:300]}
        for r in web
    ]
    return {"ok": True, "http": resp.status_code, "n_results": len(results),
            "results": results}


def main() -> None:
    load_env()
    register_secrets()
    events = pick_events()
    if not events:
        print("no upcoming events in odds_api.json — run check_odds_api.py first.")
        sys.exit(2)
    report: dict = {"events": events, "tavily": {}, "brave": {}}
    session = requests.Session()

    for event in events:
        query = f"{event['home']} injury news team news"
        label = f"{event['home']} vs {event['away']} ({event['commence'][:10]})"
        if has("TAVILY_API_KEY"):
            report["tavily"][label] = {"query": query, **tavily_search(session, query)}
            ok = report["tavily"][label].get("ok")
            print(scrub(f"tavily [{label}]: ok={ok} "
                        f"n={len(report['tavily'][label].get('results', []))}"))
            time.sleep(1.5)
        if has("BRAVE_SEARCH_API_KEY"):
            report["brave"][label] = {"query": query, **brave_search(session, query)}
            ok = report["brave"][label].get("ok")
            print(scrub(f"brave  [{label}]: ok={ok} "
                        f"n={len(report['brave'][label].get('results', []))}"))
            time.sleep(1.5)

    # Save the best article snippet (Tavily first, else Brave) for the Gemini probe
    sample = None
    for provider in ("tavily", "brave"):
        for label, entry in report[provider].items():
            for r in entry.get("results", []):
                if r.get("content_snippet") and len(r["content_snippet"]) > 200:
                    sample = {
                        "query": entry["query"],
                        "match": label,
                        "title": r["title"],
                        "url": r["url"],
                        "content": r["content_snippet"],
                    }
                    break
            if sample:
                break
        if sample:
            break

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str))
    if sample:
        SAMPLE_OUT.write_text(json.dumps(sample, indent=2, default=str))
        print(f"saved {OUT} and news sample: {sample['title'][:60]}")
    else:
        print(f"saved {OUT}; NO snippet long enough for the Gemini probe")


if __name__ == "__main__":
    main()
