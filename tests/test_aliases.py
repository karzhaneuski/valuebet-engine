"""Alias matching policy: exact/normalized auto, fuzzy report-only."""

from __future__ import annotations

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
    assert normalize("FC Köln") == "koln"
    assert normalize("The Arsenal FC") == "arsenal"
    assert normalize("Nott'm Forest") == "nottingham forest"
