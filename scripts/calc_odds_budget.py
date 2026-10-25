"""Offline credit budget simulation for The Odds API (Phase 4b B1).

Reads measured call counts from reports/recon/odds_budget.json (policy d_snapshots,
28-day no-break window) and projects 30-day consumption across policies, candidate
volume variations, and the degradation ladder.

Does NOT make network requests.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def calculate_30d_budget(input_path: Path) -> dict[str, Any]:
    with open(input_path, encoding="utf-8") as f:
        raw = json.load(f)

    # 1. Base measured calls from 28-day no-break window across all 6 sports
    # (EPL, La Liga, Serie A, Bundesliga, Ligue 1, UCL)
    d_snap = raw["policies"]["d_snapshots"]["no_break_28d"]["per_tier"]
    d_day = raw["policies"]["d_snapshots_day_slots"]["no_break_28d"]["per_tier"]
    scores_28d_credits = raw["budget_scenarios"]["scores"]["total_28d"]  # 8

    t48_calls_28d = int(d_snap["48"])        # 64
    t24_calls_28d = int(d_snap["24"])        # 68
    t3_slot_calls_28d = int(d_snap["3"])     # 91
    t3_day_calls_28d = int(d_day["3"])       # 68

    scale_30_28 = 30.0 / 28.0
    scale_week_to_month = 30.0 / 7.0

    # 30-day scaled calls (1 credit/call if 1 market, 2 credits if totals+h2h)
    t48_calls_30d = t48_calls_28d * scale_30_28
    t24_calls_30d = t24_calls_28d * scale_30_28
    t3_slot_calls_30d = t3_slot_calls_28d * scale_30_28
    t3_day_calls_30d = t3_day_calls_28d * scale_30_28
    scores_credits_30d = scores_28d_credits * scale_30_28

    # Scenario: Infeasible "all tiers on" with totals + h2h (2 credits per call)
    all_tiers_calls_28d = t48_calls_28d + t24_calls_28d + t3_slot_calls_28d  # 223
    all_tiers_credits_30d = (all_tiers_calls_28d * 2 * scale_30_28) + scores_credits_30d

    # Adopted default policy tier costs
    # T-48: OFF
    # T-24: totals + h2h (2 credits)
    # T-3: slot-based totals + h2h (2 credits)
    t24_credits_30d = t24_calls_30d * 2.0
    t3_slot_2m_credits_30d = t3_slot_calls_30d * 2.0
    sport_snapshots_subtotal_30d = t24_credits_30d + t3_slot_2m_credits_30d

    def candidate_cs_credits_30d(candidates_per_week: int) -> float:
        return candidates_per_week * scale_week_to_month * 1.0

    variations_by_cs = {}
    for weekly_cs in (0, 5, 10, 15):
        cs_cost = candidate_cs_credits_30d(weekly_cs)
        total = sport_snapshots_subtotal_30d + scores_credits_30d + cs_cost
        variations_by_cs[f"cs_{weekly_cs}_per_week"] = {
            "pinnacle_correct_score_weekly": weekly_cs,
            "correct_score_credits_30d": round(cs_cost, 2),
            "total_credits_30d": round(total, 2),
            "fits_hard_stop_450": total <= 450.0,
            "headroom_to_450": round(450.0 - total, 2),
        }

    # Default policy with Pinnacle correct_score <= 10 matches/week
    default_total = sport_snapshots_subtotal_30d + scores_credits_30d + candidate_cs_credits_30d(10)

    # Degradation ladder
    # (a) correct_score -> 5/week
    step_a_total = sport_snapshots_subtotal_30d + scores_credits_30d + candidate_cs_credits_30d(5)

    # (b) h2h on T-3 OFF -> T-3 has totals only (1 credit/call); home goal share from T-24
    t3_slot_1m_credits_30d = t3_slot_calls_30d * 1.0
    step_b_total = (
        t24_credits_30d + t3_slot_1m_credits_30d + scores_credits_30d + candidate_cs_credits_30d(5)
    )

    # (c) T-3 slot -> day (68 calls instead of 91, totals only at 1 credit/call)
    t3_day_1m_credits_30d = t3_day_calls_30d * 1.0
    step_c_total = (
        t24_credits_30d + t3_day_1m_credits_30d + scores_credits_30d + candidate_cs_credits_30d(5)
    )

    # (d) T-3 is never disabled (invariant)
    step_d_total = step_c_total

    ladder = [
        {
            "step": "default",
            "description": "T-48 OFF, T-24 (totals+h2h), T-3 slot (totals+h2h), CS <= 10/wk",
            "total_credits_30d": round(default_total, 2),
            "headroom_to_450": round(450.0 - default_total, 2),
        },
        {
            "step": "a_cap_correct_score_5_week",
            "description": "Cap candidate Pinnacle correct_score to 5/week",
            "total_credits_30d": round(step_a_total, 2),
            "headroom_to_450": round(450.0 - step_a_total, 2),
        },
        {
            "step": "b_disable_t3_h2h",
            "description": "Disable h2h on T-3 (totals only, home share taken from T-24)",
            "total_credits_30d": round(step_b_total, 2),
            "headroom_to_450": round(450.0 - step_b_total, 2),
        },
        {
            "step": "c_t3_slot_to_day",
            "description": "Switch T-3 from kickoff slots to day slots (1 call/day)",
            "total_credits_30d": round(step_c_total, 2),
            "headroom_to_450": round(450.0 - step_c_total, 2),
        },
        {
            "step": "d_t3_never_disabled",
            "description": "T-3 is never disabled (floor maintained)",
            "total_credits_30d": round(step_d_total, 2),
            "headroom_to_450": round(450.0 - step_d_total, 2),
        },
    ]

    return {
        "source": {
            "measured_file": str(input_path),
            "policy": "policies.d_snapshots.no_break_28d",
            "measured_calls_28d": {
                "t48": t48_calls_28d,
                "t24": t24_calls_28d,
                "t3_slot": t3_slot_calls_28d,
                "t3_day": t3_day_calls_28d,
                "total_calls_1market": all_tiers_calls_28d,
            },
            "scaled_calls_30d": {
                "t48": round(t48_calls_30d, 2),
                "t24": round(t24_calls_30d, 2),
                "t3_slot": round(t3_slot_calls_30d, 2),
                "t3_day": round(t3_day_calls_30d, 2),
            },
            "scores_ucl_30d_credits": round(scores_credits_30d, 2),
        },
        "all_tiers_on_infeasible_overflow": {
            "description": "All tiers (T-48, T-24, T-3 slot) with totals+h2h (2 credits/call)",
            "sport_calls_30d": round(all_tiers_calls_28d * scale_30_28, 2),
            "sport_credits_30d": round(all_tiers_calls_28d * 2 * scale_30_28, 2),
            "scores_credits_30d": round(scores_credits_30d, 2),
            "total_credits_30d": round(all_tiers_credits_30d, 2),
            "hard_stop_ceiling": 450,
            "overflow_credits": round(all_tiers_credits_30d - 450.0, 2),
            "note": "Exceeds 450 hard stop without any candidate correct_score calls.",
        },
        "default_adopted_policy": {
            "description": "T-48 OFF, T-24 totals+h2h, T-3 slot totals+h2h, CS <= 10/wk",
            "t48_status": "OFF",
            "t24_markets": ["totals", "h2h"],
            "t24_credits_30d": round(t24_credits_30d, 2),
            "t3_granularity": "slot",
            "t3_markets": ["totals", "h2h"],
            "t3_credits_30d": round(t3_slot_2m_credits_30d, 2),
            "scores_credits_30d": round(scores_credits_30d, 2),
            "pinnacle_correct_score_weekly_cap": 10,
            "correct_score_credits_30d": round(candidate_cs_credits_30d(10), 2),
            "total_credits_30d": round(default_total, 2),
            "hard_stop_ceiling": 450,
            "monthly_quota": 500,
            "headroom_to_450": round(450.0 - default_total, 2),
        },
        "candidate_variations": variations_by_cs,
        "degradation_ladder": ladder,
    }


def print_markdown_summary(res: dict[str, Any]) -> None:
    d = res["default_adopted_policy"]
    over = res["all_tiers_on_infeasible_overflow"]
    print("# The Odds API 30-Day Budget Simulation (Phase 4b B1)\n")
    print("## 1. Why All Tiers ON is Infeasible")
    print(
        f"- Sport snapshots (T-48, T-24, T-3 slots) with `totals+h2h`: "
        f"{over['sport_credits_30d']} credits/30d"
    )
    print(f"- UCL `/scores`: {over['scores_credits_30d']} credits/30d")
    print(
        f"- Total: **{over['total_credits_30d']} credits/30d** > 450 hard stop "
        f"(+{over['overflow_credits']} credits overflow!)\n"
    )

    print("## 2. Adopted Default Policy")
    print("- **T-48**: OFF (0 calls)")
    print(f"- **T-24**: ON (`totals,h2h` = 2 credits) -> {d['t24_credits_30d']} credits/30d")
    print(f"- **T-3 (slots)**: ON (`totals,h2h` = 2 credits) -> {d['t3_credits_30d']} credits/30d")
    print(f"- **UCL `/scores`**: {d['scores_credits_30d']} credits/30d")
    print(
        f"- **Pinnacle `correct_score`** (<= 10/wk): "
        f"{d['correct_score_credits_30d']} credits/30d"
    )
    print(
        f"- **Total**: **{d['total_credits_30d']} credits/30d** "
        f"(Headroom to 450 stop: {d['headroom_to_450']} credits)\n"
    )

    print("## 3. Candidate Volume Variations (Default Tiers)")
    print(
        "| Pinnacle CS / week | CS credits / 30d | Total credits / 30d | "
        "Headroom to 450 | Fits <= 450 |"
    )
    print("|---|---|---|---|---|")
    for v in res["candidate_variations"].values():
        w = v["pinnacle_correct_score_weekly"]
        cs = v["correct_score_credits_30d"]
        tot = v["total_credits_30d"]
        hr = v["headroom_to_450"]
        fits = v["fits_hard_stop_450"]
        print(f"| {w} | {cs} | {tot} | {hr} | {fits} |")
    print()

    print("## 4. Degradation Ladder")
    print("| Step | Description | Total credits / 30d | Headroom to 450 |")
    print("|---|---|---|---|")
    for step in res["degradation_ladder"]:
        st = step["step"]
        desc = step["description"]
        tot = step["total_credits_30d"]
        hr = step["headroom_to_450"]
        print(f"| `{st}` | {desc} | **{tot}** | {hr} |")


def main() -> None:
    parser = argparse.ArgumentParser(description="Calculate 30d TOA budget from odds_budget.json")
    parser.add_argument("--input", type=Path, default=Path("reports/recon/odds_budget.json"))
    parser.add_argument("--output", type=Path, default=Path("reports/recon/odds_budget_30d.json"))
    args = parser.parse_args()

    results = calculate_30d_budget(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print(f"Saved artifact to {args.output}\n")
    print_markdown_summary(results)


if __name__ == "__main__":
    main()
