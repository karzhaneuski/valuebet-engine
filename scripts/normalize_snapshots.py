"""S13: normalize restored odds_snapshots.

- bookmaker: display name -> TOA bookmaker KEY (odds_api.json names);
  unmapped names are written to the report and left as-is;
- tier: recomputed from actual_offset_min against the config tier windows
  ([40;56]h -> T-48, [18;30]h -> T-24, [1;4]h -> T-3, [0.5;2]h -> T-1);
  out of every window -> off_window; manual tier is kept.

Idempotent: re-running reports 0 changes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from core.config import load_config
from core.db import session_scope
from core.orm import OddsSnapshot

# display names seen in The Odds API responses -> bookmaker keys
DISPLAY_TO_KEY = {
    "pinnacle": "pinnacle", "william hill": "williamhill",
    "william hill (us)": "williamhill_us", "1xbet": "onexbet",
    "matchbook": "matchbook", "mybookie.ag": "mybookieag",
    "betonline.ag": "betonlineag", "gtbets": "gtbets",
    "betsson": "betsson", "tipico": "tipico", "coolbet": "coolbet",
    "codere (it)": "codere_it", "leovegas (se)": "leovegas_se",
    "marathon bet": "marathonbet", "nordic bet": "nordicbet",
    "betanything": "betanything", "betclic (fr)": "betclic_fr",
    "betfair": "betfair", "888sport": "888sport",
    "unibet (fr)": "unibet_fr", "unibet (nl)": "unibet_nl",
    "unibet (se)": "unibet_se", "winamax (fr)": "winamax_fr",
    "winamax (de)": "winamax_de", "pmu (fr)": "pmu_fr",
}

OUT = Path("reports/ingest/normalize_snapshots_report.json")

KNOWN_KEYS = {v for v in DISPLAY_TO_KEY.values()} | {
    # keys observed in phase-0 odds_api.json that never had a display-name
    # variant in our stored snapshots
    "betclic_fr", "unibet_fr", "winamax_fr", "winamax_de",
}


def tier_for_lead(lead_hours: float | None, config) -> str | None:
    if lead_hours is None:
        return None
    w = config.odds_policy.tier_windows
    for tier, (lo, hi) in (("T-48", w.t48), ("T-24", w.t24),
                           ("T-3", w.t3), ("T-1", (0.5, 2.0))):
        if lo <= lead_hours <= hi:
            return tier
    return "off_window"


def main() -> None:
    config = load_config()
    renamed: dict[str, int] = {}
    unmapped: set[str] = set()
    tier_changes: dict[str, int] = {"off_window": 0, "reassigned": 0}
    with session_scope(config.database.url) as s:
        snaps = s.scalars(select(OddsSnapshot)).all()
        for snap in snaps:
            # 1. bookmaker key
            key = DISPLAY_TO_KEY.get(snap.bookmaker.strip().lower())
            if snap.bookmaker in KNOWN_KEYS:
                continue  # already a TOA key (previous run)
            if key is None:
                unmapped.add(snap.bookmaker)
            elif key != snap.bookmaker:
                renamed[snap.bookmaker] = renamed.get(snap.bookmaker, 0) + 1
                snap.bookmaker = key
            # 2. tier by actual lead
            if snap.tier == "manual":
                continue
            lead_h = (snap.actual_offset_min / 60
                      if snap.actual_offset_min is not None else None)
            new_tier = tier_for_lead(lead_h, config)
            if new_tier is not None and new_tier != snap.tier:
                if new_tier == "off_window":
                    tier_changes["off_window"] += 1
                else:
                    tier_changes["reassigned"] += 1
                snap.tier = new_tier
    already = not renamed and not tier_changes["off_window"] \
        and not tier_changes["reassigned"]
    report = {"already_normalized": already,
              "renamed_bookmakers": renamed, "unmapped_bookmakers":
              sorted(unmapped), "tier_changes": tier_changes}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
