"""Recon (review N3): map football-data.co.uk column codes to bookmaker names.

Fetches the official notes file (https://www.football-data.co.uk/notes.txt)
and extracts the odds-column-code -> bookmaker mapping for the codes we
actually observed in football_data.json. Writes reports/recon/fd_notes.json.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import requests

OUT = Path("reports/recon/fd_notes.json")
FD_JSON = Path("reports/recon/football_data.json")
NOTES_URL = "https://www.football-data.co.uk/notes.txt"


def main() -> None:
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (valuebet-engine phase-0 recon)"
    resp = session.get(NOTES_URL, timeout=30)
    text = resp.text

    # notes.txt format: "B365H = Bet365 home win odds", "PSH and PH = Pinnacle home win odds"
    mapping: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"\s*((?:[A-Za-z0-9]{1,5}[HDA<>](?:,| and )?\s*)+)=\s*"
                     r"(.+?)\s+(?:home|away|draw|over|under|odds|maximum|average|market)",
                     line, re.IGNORECASE)
        if m:
            codes = re.findall(r"([A-Za-z0-9]{1,5})[HDA<>]", m.group(1))
            name = m.group(2).strip(" .-")
            for code in codes:
                base = re.sub(r"[HDA<>]$", "", code).upper()
                if base and len(base) <= 5:
                    mapping.setdefault(base, name)
    # explicit "XX = Name Bookmaker" lines without the odds suffix
    for line in text.splitlines():
        m = re.match(r"\s{0,4}([A-Za-z0-9]{2,5})\s*=\s*([A-Z][A-Za-z0-9 .'\-]+)$",
                     line)
        if m:
            mapping.setdefault(m.group(1).upper(),
                               m.group(2).strip(" .-"))

    observed: set[str] = set()
    for rec in json.loads(FD_JSON.read_text()):
        for bk in rec.get("bookmakers_1x2", []):
            base = re.match(r"^([A-Za-z0-9]+?)(?:C?AH|AH|C)?$", bk)
            if base and base.group(1) not in ("Avg", "Max"):
                observed.add(base.group(1))

    result = {
        "source": NOTES_URL,
        "fetched_http": resp.status_code,
        "observed_codes": sorted(observed),
        "code_to_name": {c: mapping.get(c, "UNKNOWN — not found in notes.txt")
                         for c in sorted(observed)},
    }
    OUT.write_text(json.dumps(result, indent=2))
    print(json.dumps(result["code_to_name"], indent=2))


if __name__ == "__main__":
    main()

