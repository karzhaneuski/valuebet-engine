"""Team-name matching across sources (review B7 policy).

Automatic (deterministic) rules only:
1. exact alias within the source;
2. normalization (fixed abbreviation table + stop tokens; "club"/"de" are
   meaningful) — cross-source, deterministic;
3. contiguous containment ("Brighton" ⊂ "Brighton and Hove Albion") — ONLY
   when a league-season roster is supplied, and only if exactly ONE roster
   team matches AND that team has no alias of this source yet.

Anything similarity-based is NEVER applied automatically — fuzzy candidates
go to the report (the phase-0 mistake: Racing Santander vs Athletic Club
matched on the token "Club").

A normalized-name collision between two different teams raises
AliasCollisionError at matcher construction instead of silently picking one.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field

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
    "juve": "juventus",
    "w bremen": "werder bremen",
    "f dortmund": "borussia dortmund",
    "b dortmund": "borussia dortmund",
    "gladbach": "borussia monchengladbach",
    "b monchengladbach": "borussia monchengladbach",
    "e frankfurt": "eintracht frankfurt",
    "mainz 05": "mainz",
    "1 fc union berlin": "union berlin",
    "stade de reims": "reims",
    "stade brestois": "brest",
    "stade rennais": "rennes",
    "paris sg": "paris saint germain",
    "espanol": "espanyol",
    "greuther furth": "greuther fuerth",
    "hamburg": "hamburger sv",
    "milan": "ac milan",
    "st etienne": "saint etienne",
    "koln": "cologne",
}

# "club"/"de" are NOT stop tokens: Athletic Club vs Racing Club depend on them
STOP_TOKENS = {"fc", "cf", "sc", "afc", "acd", "cd", "the"}


class AliasCollisionError(RuntimeError):
    """Two different teams normalize to the same name — must be resolved
    manually, never silently."""


def normalize(name: str) -> str:
    """Deterministic normalization: lowercase, strip accents/punct, drop
    stop tokens, apply known abbreviations."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    tokens = [t for t in s.split() if t and t not in STOP_TOKENS]
    joined = " ".join(tokens)
    return FD_KNOWN_ABBREVIATIONS.get(joined, joined)


def _is_subsequence(short: str, long: str) -> bool:
    """True when short's tokens appear in long as a contiguous block,
    where a short token may also be a PREFIX of the long token ("inter"
    matches both "inter milan" and "internacional" — the ambiguity is
    handled by the single-candidate rule)."""
    s_tokens = short.split()
    l_tokens = long.split()
    n = len(s_tokens)

    def block_ok(start: int) -> bool:
        for j, tok in enumerate(s_tokens):
            lt = l_tokens[start + j]
            if lt != tok and not lt.startswith(tok):
                return False
        return True

    return any(block_ok(i) for i in range(len(l_tokens) - n + 1))


def token_set(name: str) -> set[str]:
    return set(normalize(name).split())


@dataclass
class AliasMatch:
    matched: str | None          # canonical team name or None
    method: str                  # exact | normalized | contained | fuzzy:none
    candidates: list[tuple[str, float]] = field(default_factory=list)


class AliasMatcher:
    """Match external names against team_aliases in the DB session."""

    def __init__(self, aliases: dict[tuple[str, str], int],
                 names_by_id: dict[int, str]) -> None:
        self._aliases = dict(aliases)
        self._names = dict(names_by_id)
        by_norm: dict[str, int] = {}
        for (_source, alias), team_id in aliases.items():
            key = normalize(alias)
            if key in by_norm and by_norm[key] != team_id:
                raise AliasCollisionError(
                    f"normalized collision on {key!r}: teams "
                    f"{self._names.get(by_norm[key])!r} and "
                    f"{self._names.get(team_id)!r}")
            by_norm[key] = team_id
        self._by_norm = by_norm

    def match(self, source: str, name: str,
              roster: set[int] | None = None,
              allow_containment: bool = True) -> AliasMatch:
        """roster: team ids of the league-season; containment is restricted
        to roster teams that have no alias of ``source`` yet."""
        exact = self._aliases.get((source, name))
        if exact is not None:
            return AliasMatch(matched=self._names[exact], method="exact")
        norm = normalize(name)
        hit = self._by_norm.get(norm)
        if hit is not None:
            return AliasMatch(matched=self._names[hit], method="normalized")
        if allow_containment and roster and len(norm) >= 4:
            have_source_alias = {team_id for (src, _a), team_id
                                 in self._aliases.items() if src == source}
            supers: list[int] = []
            for norm_key, team_id in self._by_norm.items():
                if team_id in roster and team_id not in have_source_alias \
                        and norm != norm_key \
                        and (_is_subsequence(norm, norm_key)
                             or _is_subsequence(norm_key, norm)):
                    supers.append(team_id)
            if len(set(supers)) == 1:
                return AliasMatch(matched=self._names[supers[0]],
                                  method="contained")
        # fuzzy: report only, never auto-apply
        tokens = token_set(name)
        scored: list[tuple[str, float]] = []
        for norm_key, team_id in self._by_norm.items():
            other = set(norm_key.split())
            distinctive = (tokens - STOP_TOKENS) & (other - STOP_TOKENS)
            if not distinctive:
                continue
            ratio = difflib.SequenceMatcher(None, norm, norm_key).ratio()
            if ratio >= 0.5:
                scored.append((self._names[team_id], round(ratio, 3)))
        scored.sort(key=lambda x: -x[1])
        return AliasMatch(matched=None, method="fuzzy:none",
                          candidates=scored[:3])
