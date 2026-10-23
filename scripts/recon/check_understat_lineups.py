"""Recon: can Understat replace API-Football lineups for the current season?

API-Football free tier has no access to the current season (verified), so
this probe checks whether soccerdata's Understat reader provides per-match
player data (starters vs substitutes) for a played 2026/27 match.

Writes reports/recon/understat_lineups.json. Phase 0 recon only.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import soccerdata

OUT = Path("reports/recon/understat_lineups.json")
CACHE = Path(".cache/understat")


def main() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    reader = soccerdata.Understat(
        leagues=["ENG-Premier League"],
        seasons=[2026],
        data_dir=CACHE,
    )
    report: dict = {}
    sched = reader.read_schedule()
    played = sched[sched["is_result"]].sort_values("date")
    last = played.iloc[-1]
    report["match"] = {
        "game_id": int(last["game_id"]),
        "date": str(last["date"]),
        "home_team": str(last["home_team"]),
        "away_team": str(last["away_team"]),
    }

    time.sleep(1.0)
    players = reader.read_player_match_stats(match_id=report["match"]["game_id"])
    flat = players.reset_index()
    report["n_player_rows"] = int(len(flat))
    report["columns"] = [str(c) for c in flat.columns]
    starter_col = next((c for c in flat.columns if "position" in str(c).lower()), None)
    if starter_col:
        report["position_values"] = sorted(flat[starter_col].astype(str).unique().tolist())
    team_col = next((c for c in flat.columns if str(c) == "team"), None)
    if team_col:
        report["players_per_team"] = flat.groupby(team_col).size().to_dict()
    time_col = next((c for c in flat.columns if "time" in str(c).lower()
                     and "minute" not in str(c).lower()), None)
    report["sample"] = flat.head(3).astype(str).to_dict(orient="records")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps({k: v for k, v in report.items() if k != "sample"}, indent=2,
                     default=str))
    print(f"saved {OUT}")


if __name__ == "__main__":
    main()
