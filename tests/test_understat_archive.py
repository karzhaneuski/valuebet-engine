"""Understat archive adapter: parsing only (no network)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from core.ingest.understat_archive import ArchiveRow, archive_rows

PAYLOAD = {
    "dates": [
        {"id": "111", "datetime": "2021-08-13 19:00:00",
         "h": {"id": 1, "title": "Brentford", "short_title": "BRE"},
         "a": {"id": 2, "title": "Arsenal", "short_title": "ARS"},
         "goals": {"h": "2", "a": "0"}, "xG": {"h": "1.743", "a": "0.981"},
         "isResult": True},
        {"id": "112", "datetime": "2021-08-14 14:00:00",
         "h": {"id": 3, "title": "Man United", "short_title": "MUN"},
         "a": {"id": 4, "title": "Leeds", "short_title": "LEE"},
         "goals": {"h": "5", "a": "1"}, "xG": {"h": "2.05", "a": "0.86"},
         "isResult": True},
        {"id": "113", "datetime": "2021-08-15 15:30:00",
         "h": {"id": 5, "title": "Chelsea", "short_title": "CHE"},
         "a": {"id": 6, "title": "Crystal Palace", "short_title": "CRY"},
         "goals": {"h": "3", "a": "0"}, "xG": {"h": "0", "a": "0.0"},
         "isResult": True},  # missing xG encoded as "0" like understat does
    ],
    "teams": {}, "players": {},
}


def test_archive_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "ENG-Premier League_2021.json"
    path.write_text(json.dumps(PAYLOAD))
    rows: list[ArchiveRow] = archive_rows(tmp_path, "ENG-Premier League", 2021)
    assert len(rows) == 3
    first = rows[0]
    assert first.understat_id == "111"
    assert first.home == "Brentford"
    assert first.naive_start == dt.datetime(2021, 8, 13, 19, 0)
    assert first.home_goals == 2 and first.away_goals == 0
    assert first.home_xg == 1.743
    # xG "0" is treated as missing (library semantics)
    assert rows[2].home_xg is None
