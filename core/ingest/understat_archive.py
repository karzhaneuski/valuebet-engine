"""One-time Understat archive adapter for pre-window seasons (2021/22).

soccerdata's season discovery covers only the last 5 seasons (phase-0
finding), but Understat's public JSON API serves older seasons:
``GET https://understat.com/getLeagueData/{league_slug}/{season}``.
This module downloads such a season ONCE through the reader's public
``get()`` method (which reuses the pinned library's TLS-impersonating
session — plain requests get blocked) and stores the raw JSON forever under
``data/raw/understat/``. It deliberately does NOT depend on the private
``_read_league_season``; the payload parsing lives here.

Isolated on purpose (docs/decisions.md, phase 2): this is the only module
allowed to know about the raw endpoint payload shape.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

UNDERSTAT_URL = "https://understat.com/getLeagueData/{league}/{season}"


@dataclass
class ArchiveRow:
    understat_id: str
    home: str
    away: str
    naive_start: dt.datetime
    is_result: bool
    home_goals: int | None
    away_goals: int | None
    home_xg: float | None
    away_xg: float | None


def archive_path(archive_dir: Path, league: str, season: int) -> Path:
    return archive_dir / f"{league}_{season}.json"


def is_archived(archive_dir: Path, league: str | None, season: int) -> bool:
    if league is None:
        return False
    return archive_path(archive_dir, league, season).is_file()


def store_archive(archive_dir: Path, league: str, season: int,
                  cache_dir: Path) -> None:
    """Download the season payload once and store it permanently."""
    path = archive_path(archive_dir, league, season)
    if path.is_file():
        return
    import soccerdata

    reader = soccerdata.Understat(data_dir=cache_dir)
    url = UNDERSTAT_URL.format(league=league, season=season)
    # league_id is only used by the library for cache file naming; using a
    # stable pseudo-id keeps the request cache predictable
    payload = reader.get(url, cache_dir / f"archive_{league}_{season}.json")
    data = json.load(payload)
    archive_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return None


def archive_rows(archive_dir: Path, league: str,
                 season: int) -> list[ArchiveRow]:
    """Parse the stored payload into rows (same shape as schedule rows)."""
    data = json.loads(archive_path(archive_dir, league, season).read_text())
    rows: list[ArchiveRow] = []
    for m in data.get("dates", []):
        xg = m.get("xG") or {}
        goals = m.get("goals") or {}
        rows.append(ArchiveRow(
            understat_id=str(m["id"]),
            home=str(m["h"]["title"]), away=str(m["a"]["title"]),
            naive_start=dt.datetime.strptime(m["datetime"],
                                             "%Y-%m-%d %H:%M:%S"),
            is_result=bool(m.get("isResult")),
            home_goals=int(goals["h"]) if goals.get("h") is not None else None,
            away_goals=int(goals["a"]) if goals.get("a") is not None else None,
            home_xg=float(xg["h"]) if xg.get("h") not in (None, "0") else None,
            away_xg=float(xg["a"]) if xg.get("a") not in (None, "0") else None,
        ))
    return rows
