"""Recon: Gemini — available models and structured output on the SPEC schema.

- Lists models reachable with the key (names only, no secrets).
- Runs one structured-output extraction (response_schema) on a real news
  snippet saved by check_search.py, using the exact JSON shape from
  SPEC section 6: [{player, team, status, source_url, quote_summary}]
  with status in {out, doubtful, suspended}.
- Validates the response against the schema (strict check).

Writes reports/recon/gemini.json. Phase 0 recon only.
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

BASE = "https://generativelanguage.googleapis.com/v1beta"
SAMPLE = Path("reports/recon/news_sample.json")
OUT = Path("reports/recon/gemini.json")

SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "player": {"type": "STRING"},
            "team": {"type": "STRING"},
            "status": {"type": "STRING",
                       "enum": ["out", "doubtful", "suspended"]},
            "source_url": {"type": "STRING"},
            "quote_summary": {"type": "STRING"},
        },
        "required": ["player", "team", "status", "source_url", "quote_summary"],
    },
}


def validate(items: object) -> tuple[bool, str]:
    if not isinstance(items, list):
        return False, "response is not a JSON array"
    allowed = {"player", "team", "status", "source_url", "quote_summary"}
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            return False, f"item {i} is not an object"
        missing = allowed - set(item)
        if missing:
            return False, f"item {i} missing fields: {sorted(missing)}"
        if item["status"] not in ("out", "doubtful", "suspended"):
            return False, f"item {i} bad status: {item['status']!r}"
    return True, f"valid array with {len(items)} items"


def main() -> None:
    load_env()
    register_secrets()
    if not has("GEMINI_API_KEY"):
        print("GEMINI_API_KEY is missing in .env — cannot verify, exiting.")
        sys.exit(2)
    if not SAMPLE.is_file():
        print("no news sample — run check_search.py first.")
        sys.exit(2)

    session = requests.Session()
    session.headers["x-goog-api-key"] = os.environ["GEMINI_API_KEY"]
    report: dict = {}

    # 1. Models available to this key
    resp = session.get(f"{BASE}/models", timeout=30)
    models = []
    if resp.status_code == 200:
        for m in resp.json().get("models", []):
            if "generateContent" in m.get("supportedGenerationMethods", []):
                models.append(m["name"].replace("models/", ""))
    report["models_available"] = sorted(models)
    print(f"models with generateContent: {len(models)}")
    print("  ", [m for m in models if "flash" in m][:6], "...")

    # pick a fast model for extraction, a strong one for reasoning (SPEC config roles)
    extraction = next((m for m in models if "flash" in m and "lite" not in m), None)
    reasoning = next((m for m in models if "pro" in m), None)
    report["picked"] = {"extraction_model": extraction, "reasoning_model": reasoning}
    if not extraction:
        print("no flash model available — aborting structured test.")
        OUT.write_text(json.dumps(report, indent=2))
        sys.exit(1)

    # 2. Structured output on the real news snippet
    news = json.loads(SAMPLE.read_text())
    prompt = (
        "You extract football player availability facts from a news article.\n"
        "Return ONLY a JSON array. Each item must have exactly these fields:\n"
        "- player: player name\n"
        "- team: team name\n"
        "- status: one of 'out', 'doubtful', 'suspended'\n"
        "- source_url: the article URL given below\n"
        "- quote_summary: one short English sentence summarizing the reason\n"
        "Include only players explicitly mentioned as injured, doubtful or "
        "suspended. If there are none, return [].\n\n"
        f"Article title: {news['title']}\n"
        f"Article URL: {news['url']}\n"
        "Article text:\n<<<\n" + news["content"] + "\n>>>\n"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
            "temperature": 0,
        },
    }
    time.sleep(1.0)
    resp = session.post(
        f"{BASE}/models/{extraction}:generateContent", json=body, timeout=60
    )
    entry: dict = {"model": extraction, "http": resp.status_code}
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != 200:
        entry["error"] = scrub(json.dumps(data)[:400])
        print(scrub(f"structured call FAILED: {resp.status_code} {entry['error']}"))
    else:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        usage = data.get("usageMetadata", {})
        entry["usage"] = usage
        try:
            items = json.loads(text)
        except json.JSONDecodeError:
            items = None
        entry["raw_response_head"] = text[:500]
        ok, detail = validate(items)
        entry["schema_valid"] = ok
        entry["validation"] = detail
        entry["extracted_n"] = len(items) if isinstance(items, list) else None
        entry["extracted_sample"] = items[:3] if isinstance(items, list) else None
        print(f"structured output: valid={ok} ({detail})")
        if isinstance(items, list) and items:
            print("  e.g.", json.dumps(items[0], ensure_ascii=False)[:160])
        print(f"  tokens: prompt={usage.get('promptTokenCount')} "
              f"candidates={usage.get('candidatesTokenCount')}")

    report["structured_output_test"] = entry
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str))
    print(f"saved {OUT}")


if __name__ == "__main__":
    main()
