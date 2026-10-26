"""Anti-stub verification script (SPEC v3, prompt 0.2).

Verifies:
1. No stub patterns across core/, api/, mcp_server/, dashboard/:
   - 'In production, invokes' / 'In production' replacing implementation
   - 'snapshots_taken': 0 without active polling logic
   - 'TODO', 'FIXME', 'not implemented'
2. Verifies presence and external usage of all 11 live contour functions:
   - core.scheduler.tasks:
       job_poll_odds_snapshots, job_poll_events, job_refresh_history,
       job_settle_matches_and_clv, job_prefilter_candidates, job_search_news_windows
   - core.news:
       search_news_for_match, extract_player_news, generate_match_brief
   - core.tracking:
       settle_user_bet, calculate_user_bet_clv
   Each function must be defined AND called outside of tests/ (in scheduler, API, CLI, or MCP).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SEARCH_DIRS = ["core", "api", "mcp_server", "dashboard"]

FORBIDDEN_PATTERNS = [
    (
        re.compile(r"In production,\s*invokes", re.IGNORECASE),
        "Stub phrase 'In production, invokes'",
    ),
    (re.compile(r"\"snapshots_taken\":\s*0\b"), "Literal hardcoded stub '\"snapshots_taken\": 0'"),
    (re.compile(r"\bTODO\b"), "TODO marker in non-test source code"),
    (re.compile(r"\bFIXME\b"), "FIXME marker in non-test source code"),
    (re.compile(r"\bnot implemented\b", re.IGNORECASE), "Not implemented stub phrase"),
]

LIVE_CONTOUR_FUNCTIONS = [
    ("core/scheduler/tasks.py", "job_poll_odds_snapshots"),
    ("core/scheduler/tasks.py", "job_poll_events"),
    ("core/scheduler/tasks.py", "job_refresh_history"),
    ("core/scheduler/tasks.py", "job_settle_matches_and_clv"),
    ("core/scheduler/tasks.py", "job_prefilter_candidates"),
    ("core/scheduler/tasks.py", "job_search_news_windows"),
    ("core/news/search.py", "search_news_for_match"),
    ("core/news/extractor.py", "extract_player_news"),
    ("core/news/brief.py", "generate_match_brief"),
    ("core/tracking/settlement.py", "settle_user_bet"),
    ("core/tracking/clv.py", "calculate_user_bet_clv"),
    ("core/ingest/odds_api.py", "snapshot_sport_odds"),
    ("core/value/persistence.py", "load_market_inputs_for_match"),
    ("core/news/freshness.py", "get_freshness_warnings"),
    ("core/ingest/odds_api.py", "apply_scores"),
    ("core/ingest/odds_api.py", "apply_events"),
]


def check_forbidden_patterns() -> list[str]:
    errors: list[str] = []
    for d in SEARCH_DIRS:
        target_dir = ROOT / d
        if not target_dir.exists():
            continue
        for p in target_dir.rglob("*.py"):
            text = p.read_text(encoding="utf-8")
            for line_no, line in enumerate(text.splitlines(), start=1):
                # Allow docstrings in check_no_stubs itself or comments describing rules
                for pat, desc in FORBIDDEN_PATTERNS:
                    if pat.search(line):
                        errors.append(f"{p.relative_to(ROOT)}:{line_no} - {desc}: '{line.strip()}'")
    return errors


def check_live_contour_functions() -> list[str]:
    errors: list[str] = []
    # Collect all python source files outside tests/
    non_test_files: list[Path] = []
    for d in [*SEARCH_DIRS, "scripts"]:
        td = ROOT / d
        if td.exists():
            non_test_files.extend(td.rglob("*.py"))

    for def_file_rel, fn_name in LIVE_CONTOUR_FUNCTIONS:
        def_file = ROOT / def_file_rel
        if not def_file.exists():
            errors.append(f"Definition file missing for {fn_name}: {def_file_rel}")
            continue

        def_text = def_file.read_text(encoding="utf-8")
        # Check function definition
        if not re.search(rf"\bdef {fn_name}\b", def_text):
            errors.append(f"Function {fn_name} is not defined in {def_file_rel}")
            continue

        # Check call outside definition file in non-test files
        call_found = False
        call_pattern = re.compile(rf"\b{fn_name}\s*\(")

        for other_p in non_test_files:
            # Calls must strictly come from outside definition file (entry points / callers)
            if other_p.resolve() == def_file.resolve():
                continue

            content = other_p.read_text(encoding="utf-8")
            if call_pattern.search(content):
                call_found = True
                break

        if not call_found:
            errors.append(
                f"Live contour function {fn_name} (from {def_file_rel}) "
                "is never called outside its definition file in non-test source code!"
            )

    return errors


def main() -> int:
    print("=== Running Anti-Stub & Live Contour Checks ===")
    pattern_errors = check_forbidden_patterns()
    contour_errors = check_live_contour_functions()

    all_errors = pattern_errors + contour_errors
    if all_errors:
        print(f"\nFAILED: Found {len(all_errors)} anti-stub / live contour issue(s):")
        for err in all_errors:
            print(f"  ❌ {err}")
        return 1

    print(f"\nPASSED: Checked {len(SEARCH_DIRS)} directories for forbidden stub patterns.")
    print(
        f"PASSED: Verified definition and external call for all {len(LIVE_CONTOUR_FUNCTIONS)} "
        "live contour functions:"
    )
    for def_file, fn in LIVE_CONTOUR_FUNCTIONS:
        print(f"  ✓ {fn:<30} (defined in {def_file})")
    print("\nAll anti-stub verification gates passed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
