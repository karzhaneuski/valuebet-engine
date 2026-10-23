"""Alias matching policy: exact/normalized auto, fuzzy report-only."""

from __future__ import annotations

import pytest

from core.ingest.aliases import AliasMatcher, normalize


def make_matcher() -> AliasMatcher:
    # team_id -> name: 1 Arsenal, 2 Manchester United, 3 Athletic Bilbao,
    # 4 Real Racing Club de Santander
    aliases = {
        ("toa", "Arsenal"): 1,
        ("toa", "Manchester United"): 2,
        ("understat", "Arsenal"): 1,
        ("understat", "Manchester United"): 2,
        ("understat", "Athletic Club"): 3,
        ("understat", "Racing Santander"): 4,
    }
    names = {1: "Arsenal", 2: "Manchester United", 3: "Athletic Bilbao",
             4: "Real Racing Club de Santander"}
    return AliasMatcher(aliases, names)


def test_exact() -> None:
    m = make_matcher().match("toa", "Arsenal")
    assert m.matched == "Arsenal" and m.method == "exact"


def test_normalized_football_data_abbreviations() -> None:
    m = make_matcher().match("football_data", "Man United")
    assert m.matched == "Manchester United" and m.method == "normalized"


def test_phase0_fuzzy_mistake_is_not_repeated() -> None:
    # "Athletic Club" is a DETERMINISTIC spelling of Athletic Bilbao
    # (understat uses it) — normalized match is correct and desired
    ok = make_matcher().match("football_data", "Athletic Club")
    assert ok.matched == "Athletic Bilbao" and ok.method == "normalized"

    # the phase-0 mistake: "Real Racing Club de Santander" was paired with
    # Athletic Bilbao on the token "Club". Deterministically it must NOT
    # auto-match anything (its normalized form matches no alias)...
    bad = make_matcher().match("football_data",
                               "Real Racing Club de Santander")
    assert bad.matched is None
    # ...and fuzzy suggestions stay report-only (Racing Santander may be
    # suggested — that is correct — but never auto-applied)
    assert bad.method == "fuzzy:none"
    assert bad.matched is None


def test_normalize_stop_tokens_and_accents() -> None:
    assert normalize("FC Köln") == "cologne"  # abbreviation maps the fd spelling
    assert normalize("The Arsenal FC") == "arsenal"
    assert normalize("Nott'm Forest") == "nottingham forest"


# --- B7: the seven reviewer pairs ------------------------------------------

def _matcher_with(*teams: str) -> AliasMatcher:
    aliases: dict[tuple[str, str], int] = {}
    names: dict[int, str] = {}
    for i, name in enumerate(teams, start=1):
        names[i] = name
        aliases[("understat", name)] = i
    return AliasMatcher(aliases, names)


def test_b7_pairs_never_cross_match() -> None:
    m = _matcher_with("Paris FC", "Paris Saint Germain")
    assert m.match("football_data", "Paris FC").matched == "Paris FC"
    assert m.match("football_data", "Paris SG").matched == \
        "Paris Saint Germain"  # abbreviation, not fuzzy

    m = _matcher_with("Racing Santander", "Athletic Club")
    assert m.match("football_data", "Racing").matched is None  # ambiguous
    assert m.match("football_data", "Real Racing Club de Santander") \
        .matched is None  # the phase-0 mistake must stay unapplied

    m = _matcher_with("Manchester United", "Manchester City")
    assert m.match("football_data", "Man Utd").matched is None  # unknown
    # abbreviation is exact: not a fuzzy guess
    assert m.match("football_data", "Man United").matched == \
        "Manchester United"
    assert m.match("football_data", "Man City").matched == "Manchester City"

    m = _matcher_with("Real Madrid", "Real Sociedad")
    assert m.match("football_data", "Real Madrid").matched == "Real Madrid"
    assert m.match("football_data", "Real Sociedad").matched == \
        "Real Sociedad"

    # containment ambiguity: Inter could be Inter Milan or Internacional
    m = _matcher_with("Inter Milan", "Internacional")
    assert m.match("football_data", "Inter",
                   roster={1, 2}).matched is None

    m = _matcher_with("Athletic Club", "Atletico Madrid")
    assert m.match("football_data", "Athletic").matched is None
    assert m.match("football_data", "Athletic Club").matched == \
        "Athletic Club"
    assert m.match("football_data", "Atletico Madrid").matched == \
        "Atletico Madrid"

    m = _matcher_with("Sheffield United", "Sheffield Wednesday")
    assert m.match("football_data", "Sheffield United").matched == \
        "Sheffield United"
    assert m.match("football_data", "Sheffield Wed").matched == \
        "Sheffield Wednesday"


def test_b7_containment_needs_roster_and_unique_candidate() -> None:
    m = _matcher_with("Brighton and Hove Albion", "Arsenal")
    # no roster -> no containment at all
    assert m.match("football_data", "Brighton").matched is None
    # roster with a single containment candidate -> deterministic match
    hit = m.match("football_data", "Brighton", roster={1})
    assert hit.matched == "Brighton and Hove Albion"
    assert hit.method == "contained"
    # once the team HAS an fd alias, containment no longer applies to it
    # (it matches exactly next time)


def test_b7_normalized_collision_raises() -> None:
    with pytest.raises(Exception, match="normalized collision"):
        # two different teams whose names normalize identically
        _matcher_with("FC Koln", "FC Köln")
