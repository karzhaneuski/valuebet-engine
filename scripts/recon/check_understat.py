"""Recon: verify Understat per-match xG via the soccerdata library.

soccerdata 2026.43.0 API notes (verified in this session):
- league codes are 'ENG-Premier League', 'ESP-La Liga', 'ITA-Serie A',
  'GER-Bundesliga', 'FRA-Ligue 1';
- seasons are passed as ints (2022 = 2022/23) and normalized internally;
- read_schedule() returns per-match rows with home_xg / away_xg, but its
  season discovery (understat.com statData) only lists the LAST 5 seasons;
- older seasons are still served by understat.com's JSON API
  (getLeagueData/{slug}/{season}), which this script probes directly for
  2021/22 via the reader's private _read_league_season (recon-only usage).

Writes reports/recon/understat.json. Phase 0 recon only, not production code.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
import soccerdata

OUT = Path("reports/recon/understat.json")
CACHE = Path(".cache/understat")

LEAGUES = [
    "ENG-Premier League",
    "ESP-La Liga",
    "ITA-Serie A",
    "GER-Bundesliga",
    "FRA-Ligue 1",
]
# Understat slugs for the direct 2021/22 probe (from soccerdata LEAGUE_DICT).
SLUGS = {
    "ENG-Premier League": "EPL",
    "ESP-La Liga": "La_liga",
    "ITA-Serie A": "Serie_A",
    "GER-Bundesliga": "Bundesliga",
    "FRA-Ligue 1": "Ligue_1",
}
# Seasons discoverable by soccerdata (4 past + current) ...
SCHEDULE_SEASONS = [2022, 2023, 2024, 2025, 2026]
# ... plus the 6th past season probed directly.
DIRECT_SEASON = 2021  # 2021/22


def season_stats(group: pd.DataFrame) -> dict:
    results = group[group["is_result"]]
    xg_ok = results["home_xg"].notna() & results["away_xg"].notna()
    return {
        "n_matches": int(len(group)),
        "n_results": int(len(results)),
        "xg_fill_among_results": round(float(xg_ok.mean()), 3) if len(results) else None,
        "first_game": str(group["date"].min()),
        "last_game": str(group["date"].max()),
    }


def direct_old_season(reader: soccerdata.Understat, league: str, league_id: int) -> dict:
    """Probe an old season via understat.com's JSON API directly."""
    t0 = time.perf_counter()
    url = f"https://understat.com/league/{SLUGS[league]}/{DIRECT_SEASON}"
    data = reader._read_league_season(url, league_id, DIRECT_SEASON)  # noqa: SLF001
    elapsed = round(time.perf_counter() - t0, 1)
    matches = data["datesData"]
    results = [m for m in matches if m.get("isResult")]
    with_xg = [
        m for m in results
        if m.get("xG", {}).get("h") not in (None, "0") and m.get("xG", {}).get("a") not in (None, "0")
    ]
    dates = sorted(str(m["datetime"]) for m in matches)
    return {
        "ok": True,
        "source": "direct getLeagueData API",
        "elapsed_s": elapsed,
        "n_matches": len(matches),
        "n_results": len(results),
        "xg_fill_among_results": round(len(with_xg) / len(results), 3) if results else None,
        "first_game": dates[0] if dates else None,
        "last_game": dates[-1] if dates else None,
    }


def main() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    report: dict[str, dict] = {}
    all_columns: list[str] | None = None

    for league in LEAGUES:
        entry: dict = {"league": league}
        reader = soccerdata.Understat(
            leagues=[league],
            seasons=SCHEDULE_SEASONS,
            data_dir=CACHE,
        )
        try:
            t0 = time.perf_counter()
            sched = reader.read_schedule()
            entry["read_schedule_total_s"] = round(time.perf_counter() - t0, 1)
            entry["ok"] = True
            if all_columns is None:
                all_columns = [str(c) for c in sched.reset_index().columns]
            stats = {
                str(season): season_stats(group)
                for season, group in sched.groupby(level="season")
            }
            entry["seasons"] = stats
        except Exception as exc:  # noqa: BLE001 — record and continue
            entry["ok"] = False
            entry["error"] = f"{type(exc).__name__}: {exc}"

        # league_id is only used for cache file naming in _read_league_season
        league_ids = {
            "ENG-Premier League": 1, "ITA-Serie A": 2, "GER-Bundesliga": 3,
            "ESP-La Liga": 4, "FRA-Ligue 1": 5,
        }
        try:
            entry["seasons"] = entry.get("seasons") or {}
            entry["seasons"][f"{DIRECT_SEASON}/22 (direct)"] = direct_old_season(
                reader, league, league_ids[league]
            )
        except Exception as exc:  # noqa: BLE001
            entry.setdefault("seasons", {})[f"{DIRECT_SEASON}/22 (direct)"] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

        report[league] = entry
        print(json.dumps({league: entry}, indent=2, default=str), flush=True)
        time.sleep(2.0)

    if all_columns:
        report["_schedule_columns"] = all_columns
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str))
    print(f"saved {OUT}")


if __name__ == "__main__":
    main()
