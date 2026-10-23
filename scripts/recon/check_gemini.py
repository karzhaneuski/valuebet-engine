"""Recon (review S6/S7): Gemini — explicit model selection + structured output.

- Lists models reachable with the key (names only, no secrets), fetches
  GET /v1beta/models/{name} metadata for stable-looking candidates
  (no -latest / -preview / -experimental suffixes in the main loop).
- extraction_model = newest stable flash; reasoning_model = newest stable
  pro, falling back to the same flash with an elevated thinking budget,
  then to gemini-2.5-pro. Selection is by factual metadata, not by name
  guesses from the review.
- Re-runs the structured-output test on the chosen models with the exact
  SPEC §6 schema, now with the match date in the prompt and a stale-news
  case: a player "expected back" BEFORE the match date must not be listed
  as out/doubtful/suspended (the first phase-0 run wrongly returned
  "suspended" for exactly this case — recorded in gemini.json).
- Token usage incl. thoughtsTokenCount is recorded for cost estimates.

Writes reports/recon/gemini.json. Phase 0 recon only.
"""

from __future__ import annotations

import json
import os
import re
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

STABLE_RE = re.compile(r"^gemini-(\d+)(?:\.(\d+))?-flash$|^gemini-(\d+)(?:\.(\d+))?-pro$")


def version_of(name: str) -> tuple[int, int]:
    m = STABLE_RE.match(name)
    major = int(m.group(1) or m.group(3))
    minor = int(m.group(2) or m.group(4) or 0)
    return major, minor


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

    # 1. Model list + metadata for stable candidates (explicit selection)
    resp = session.get(f"{BASE}/models", timeout=30)
    models = []
    if resp.status_code == 200:
        for m in resp.json().get("models", []):
            if "generateContent" in m.get("supportedGenerationMethods", []):
                models.append(m["name"].replace("models/", ""))
    stable = sorted(m for m in models if STABLE_RE.match(m))
    report["models_available_n"] = len(models)
    report["stable_candidates"] = {}
    for name in stable:
        r = session.get(f"{BASE}/models/{name}", timeout=30)
        meta = {}
        if r.status_code == 200:
            body = r.json()
            meta = {k: body.get(k) for k in
                    ("displayName", "description", "inputTokenLimit",
                     "outputTokenLimit", "version")}
            meta["description_head"] = (body.get("description") or "")[:160]
            meta.pop("description", None)
        report["stable_candidates"][name] = meta
        time.sleep(0.4)

    flashes = [m for m in stable if m.endswith("-flash")]
    pros = sorted((m for m in stable if m.endswith("-pro")),
                  key=version_of, reverse=True)
    extraction = max(flashes, key=version_of) if flashes else None

    def probe_available(model: str) -> tuple[bool, str]:
        body = {"contents": [{"parts": [{"text": "ping"}]}],
                "generationConfig": {"maxOutputTokens": 1}}
        r = session.post(f"{BASE}/models/{model}:generateContent",
                         json=body, timeout=60)
        return r.status_code == 200, f"http={r.status_code}"

    pro_probes = {}
    reasoning, reasoning_mode = None, None
    for cand in pros:  # newest stable pro that actually answers
        ok, detail = probe_available(cand)
        pro_probes[cand] = detail
        time.sleep(0.5)
        if ok:
            reasoning, reasoning_mode = cand, "pro"
            break
    if reasoning is None and extraction:
        reasoning, reasoning_mode = extraction, "flash+thinking_budget"
    report["picked"] = {"extraction_model": extraction,
                        "reasoning_model": reasoning,
                        "reasoning_mode": reasoning_mode,
                        "pro_availability_probes": pro_probes}
    print(f"picked: extraction={extraction} reasoning={reasoning} "
          f"({reasoning_mode}) pro_probes={pro_probes}")

    # 2. Structured output on the real news snippet, with match context
    news = json.loads(SAMPLE.read_text())
    match_date = "2026-10-23"  # the fixture the sample query was built from
    article_text = news.get("content", "")
    prompt = (
        "You extract football player availability facts from a news article, "
        f"for the match on {match_date}.\n"
        "Return ONLY a JSON array. Each item must have exactly these fields:\n"
        "- player, team\n"
        "- status: one of 'out', 'doubtful', 'suspended' — FOR THIS MATCH\n"
        "- source_url: the article URL given below\n"
        "- quote_summary: one short English sentence summarizing the reason\n"
        "Rules:\n"
        "- Include only players explicitly mentioned as injured, doubtful or "
        "suspended.\n"
        "- If a player is 'expected back' on a date BEFORE the match date, he "
        "is available: do NOT include him.\n"
        "If there are none, return [].\n\n"
        f"Article title: {news.get('title')}\n"
        f"Article URL: {news.get('url')}\n"
        "Article text:\n<<<\n" + article_text + "\n>>>\n"
    )

    def run_structured(model: str, thinking_budget: int | None) -> dict:
        gen_cfg: dict = {"responseMimeType": "application/json",
                         "responseSchema": SCHEMA, "temperature": 0}
        if thinking_budget is not None:
            gen_cfg["thinkingConfig"] = {"thinkingBudget": thinking_budget}
        body = {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": gen_cfg}
        r = session.post(f"{BASE}/models/{model}:generateContent",
                         json=body, timeout=90)
        entry: dict = {"model": model, "http": r.status_code,
                       "thinking_budget": thinking_budget}
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code != 200:
            entry["error"] = scrub(json.dumps(data)[:300])
            return entry
        usage = data.get("usageMetadata", {})
        entry["usage"] = usage
        entry["total_tokens_incl_thoughts"] = (
            usage.get("promptTokenCount", 0)
            + usage.get("candidatesTokenCount", 0)
            + usage.get("thoughtsTokenCount", 0))
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        try:
            items = json.loads(text)
        except json.JSONDecodeError:
            items = None
        ok, detail = validate(items)
        entry.update({"schema_valid": ok, "validation": detail,
                      "extracted_n": len(items) if isinstance(items, list) else None,
                      "extracted": items})
        # stale-news check: Issahaku is "Expected back 12 Oct 2026", i.e.
        # before the 23 Oct match -> he must NOT appear; the other players
        # in the snippet have no return date, so 'out' entries for them are
        # legitimate and the expected result is not necessarily []
        entry["stale_case_expectation"] = (
            "no item for A. Fatawu Issahaku (expected-back date precedes "
            "the match); players without a return date may stay 'out'")
        items_list = items if isinstance(items, list) else []
        entry["stale_case_pass"] = all(
            "issahaku" not in (item.get("player") or "").lower()
            for item in items_list)
        return entry

    tests: dict = {}
    if extraction:
        time.sleep(1.0)
        tests["extraction"] = run_structured(extraction, None)
        print("extraction test:", tests["extraction"].get("schema_valid"),
              "stale_pass:", tests["extraction"].get("stale_case_pass"))
    if reasoning:
        time.sleep(1.0)
        budget = None if reasoning_mode == "pro" else 4096
        tests["reasoning"] = run_structured(reasoning, budget)
        print("reasoning test:", tests["reasoning"].get("schema_valid"),
              "stale_pass:", tests["reasoning"].get("stale_case_pass"))

    report["structured_output_tests"] = tests
    report["prior_run_note"] = (
        "The first phase-0 run (same snippet, no match date in the prompt) "
        "returned status='suspended' for A. Fatawu Issahaku whose 'Expected "
        "back 12 Oct 2026' precedes the 23 Oct 2026 match — a stale-data "
        "false positive. The date-aware prompt above is the fix being tested.")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str))
    print(f"saved {OUT}")


if __name__ == "__main__":
    main()
