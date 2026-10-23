"""Team-name matching across sources.

Policy (phase-2 review of the phase-0 fuzzy mistake, docs/decisions.md):
- exact alias hit and deterministic normalization hits are applied
  automatically;
- fuzzy candidates are NEVER applied automatically — they go to a report
  file for manual confirmation (example of what fuzzy must not do:
  "Racing Santander" vs "Athletic Club" matched on the token "Club").
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field

# football-data abbreviations vs TOA spellings (verified against the CSVs
# and toa_events.json during phase 2; extendable)
FD_KNOWN_ABBREVIATIONS: dict[str, str] = {
    "man united": "manchester united",
    "man city": "manchester city",
    "nott m forest": "nottingham forest",
    "nottm forest": "nottingham forest",
    "sheffield wed": "sheffield wednesday",
    "wolves": "wolverhampton wanderers",
    "sp gijon": "sporting gijon",
    "ath bilbao": "athletic bilbao",
    "ath madrid": "atletico madrid",
    "atletico mad": "atletico madrid",
    "int milan": "inter milan",
    "inter milan": "inter milan",
    "ac milan": "ac milan",
    "as roma": "as roma",
    "juve": "juventus",
    "w bremen": "werder bremen",
    "f dortmund": "borussia dortmund",
    "b dortmund": "borussia dortmund",
    "bayern munich": "bayern munich",
    "gladbach": "borussia monchengladbach",
    "b monchengladbach": "borussia monchengladbach",
    "freiburg": "freiburg",
    "e frankfurt": "eintracht frankfurt",
    "mainz 05": "mainz",
    "fc koln": "fc cologne",
    "1 fc union berlin": "union berlin",
    "stade de reims": "reims",
    "stade brestois": "brest",
    "stade rennais": "rennes",
    "paris sg": "paris saint germain",
    "ein frankfurt": "eintracht frankfurt",
    "espanol": "espanyol",
    "greuther furth": "greuther fuerth",
    "hamburg": "hamburger sv",
    "milan": "ac milan",
    "st etienne": "saint etienne",
    "koln": "cologne",
}

# "club"/"de" are NOT stop tokens: Athletic Club vs Racing Club
STOP_TOKENS = {"fc", "cf", "sc", "afc", "acd", "cd", "the"}


def normalize(name: str) -> str:
    """Deterministic normalization: lowercase, strip accents/punct, drop
    stop tokens, apply known abbreviations."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    tokens = [t for t in s.split() if t and t not in STOP_TOKENS]
    joined = " ".join(tokens)
    return FD_KNOWN_ABBREVIATIONS.get(joined, joined)


def _is_subsequence(short: str, long: str) -> bool:
    """True when short's tokens appear in long as a contiguous block."""
    s_tokens = short.split()
    l_tokens = long.split()
    n = len(s_tokens)
    return any(l_tokens[i:i + n] == s_tokens
               for i in range(len(l_tokens) - n + 1))


def token_set(name: str) -> set[str]:
    return set(normalize(name).split())


@dataclass
class AliasMatch:
    matched: str | None          # canonical team name or None
    method: str                  # exact | normalized | fuzzy:<score>
    candidates: list[tuple[str, float]] = field(default_factory=list)


class AliasMatcher:
    """Match external names against team_aliases in the DB session."""

    def __init__(self, aliases: dict[tuple[str, str], int],
                 names_by_id: dict[int, str]) -> None:
        # (source, alias) -> team_id and team_id -> canonical name
        self._aliases = aliases
        self._names = names_by_id
        by_norm: dict[str, int] = {}
        for (_source, alias), team_id in aliases.items():
            by_norm.setdefault(normalize(alias), team_id)
        self._by_norm = by_norm

    def match(self, source: str, name: str) -> AliasMatch:
        exact = self._aliases.get((source, name))
        if exact is not None:
            return AliasMatch(matched=self._names[exact], method="exact")
        # deterministic normalization is trusted cross-source (it is a
        # fixed abbreviation table, not similarity); anything weaker is
        # fuzzy and stays report-only
        norm = normalize(name)
        hit = self._by_norm.get(norm)
        if hit is not None:
            return AliasMatch(matched=self._names[hit], method="normalized")
        # deterministic containment: the name's tokens are a contiguous
        # subsequence of exactly ONE known alias (fd shorthands like
        # "Brighton" ⊂ "Brighton and Hove Albion"); ambiguity -> report
        if len(norm) >= 4:
            supers = [team_id for norm_key, team_id
                      in self._by_norm.items()
                      if _is_subsequence(norm, norm_key) and norm != norm_key]
            unique = {t for t in supers}
            if len(unique) == 1:
                return AliasMatch(matched=self._names[supers[0]],
                                  method="contained")
        # fuzzy: report only, never auto-apply
        tokens = token_set(name)
        scored: list[tuple[str, float]] = []
        for norm_key, team_id in self._by_norm.items():
            other = set(norm_key.split())
            overlap = len(tokens & other)
            if not overlap:
                continue
            ratio = difflib.SequenceMatcher(None, norm, norm_key).ratio()
            # require BOTH token overlap and string similarity; single
            # shared stop-ish tokens ("club") must not qualify alone
            distinctive = tokens - STOP_TOKENS & (other - STOP_TOKENS)
            if distinctive and ratio >= 0.5:
                scored.append((self._names[team_id], round(ratio, 3)))
        scored.sort(key=lambda x: -x[1])
        return AliasMatch(matched=None, method="fuzzy:none",
                          candidates=scored[:3])
