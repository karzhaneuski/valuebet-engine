"""Recon: verify football-data.co.uk CSV availability and columns.

Downloads one CSV per league-season for the top-5 leagues, checks:
- HTTP availability of 5 past seasons + the current one,
- result columns (teams, score),
- O/U 2.5 columns and specifically CLOSING O/U 2.5 columns,
- which bookmakers are present.

Writes a machine-readable summary to reports/recon/football_data.json
and prints a compact table. Phase 0 recon only, not production code.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
from pathlib import Path

import requests

BASE_URL = "https://www.football-data.co.uk/mmz4281/{season}/{league}.csv"
LEAGUES = {
    "E0": "Premier League",
    "SP1": "La Liga",
    "I1": "Serie A",
    "D1": "Bundesliga",
    "F1": "Ligue 1",
}
SEASONS = {
    "2122": "2021-22",
    "2223": "2022-23",
    "2324": "2023-24",
    "2425": "2024-25",
    "2526": "2025-26",
    "2627": "2026-27 (current)",
}
RESULT_COLS = ["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"]


def parse_csv(raw: bytes) -> tuple[list[str], list[list[str]]]:
    text = raw.decode("utf-8", errors="replace").lstrip("\ufeff")
    rows = [row for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)]
    return rows[0], rows[1:]


def fill_rate(header: list[str], rows: list[list[str]], column: str) -> float | None:
    if column not in header:
        return None
    idx = header.index(column)
    values = [row[idx].strip() for row in rows if len(row) > idx]
    if not values:
        return None
    return round(sum(v != "" for v in values) / len(values), 3)


def summarize(league: str, league_name: str, season: str, season_name: str,
              header: list[str], rows: list[list[str]]) -> dict:
    totals_cols = [c for c in header if "2.5" in c]
    closing_totals_cols = [c for c in totals_cols if c.startswith(("PC", "AvgC", "MaxC"))]
    bookmakers = sorted({m.group(1) for c in header
                         for m in [re.match(r"^([A-Za-z0-9]+)H$", c)] if m})
    return {
        "league": league,
        "league_name": league_name,
        "season": season,
        "season_name": season_name,
        "n_matches": len(rows),
        "missing_result_cols": [c for c in RESULT_COLS if c not in header],
        "n_totals_cols": len(totals_cols),
        "closing_totals_cols": closing_totals_cols,
        "avg_c_over_fill": fill_rate(header, rows, "AvgC>2.5"),
        "pc_over_fill": fill_rate(header, rows, "PC>2.5"),
        "bookmakers_1x2": bookmakers,
    }


def main() -> None:
    out_dir = Path("reports/recon")
    out_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (valuebet-engine phase-0 recon)"

    results = []
    for league, league_name in LEAGUES.items():
        for season, season_name in SEASONS.items():
            url = BASE_URL.format(season=season, league=league)
            record: dict = {"league": league, "season": season,
                            "league_name": league_name, "season_name": season_name}
            try:
                resp = session.get(url, timeout=30)
                record["http_status"] = resp.status_code
                if resp.status_code == 200 and resp.content.strip():
                    header, rows = parse_csv(resp.content)
                    record.update(summarize(league, league_name, season, season_name, header, rows))
                    note = (f"matches={record['n_matches']} "
                            f"closing_totals={record['closing_totals_cols']} "
                            f"AvgC>2.5 fill={record['avg_c_over_fill']}")
                else:
                    note = "empty or non-200 response"
            except requests.RequestException as exc:
                record["http_status"] = None
                record["error"] = f"{type(exc).__name__}: {exc}"
                note = record["error"]
            results.append(record)
            print(f"{league} {season_name:<18} status={record['http_status']} {note}", flush=True)
            time.sleep(1.0)

    out = out_dir / "football_data.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"\nsaved {out}")


if __name__ == "__main__":
    main()
