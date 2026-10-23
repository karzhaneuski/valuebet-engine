# Project decisions (deviations from SPEC)

Every entry: date, decision, reason. Append new entries at the bottom.

## 2026-10-23 — Phase 0: live loop without API-Football current season

**Decision**: API-Football is used only for historical data (free plan covers
seasons 2022–2024). The live pipeline (fixtures, results, lineups, key players)
runs on Understat via soccerdata + football-data.co.uk; odds on The Odds API.
Player availability (injuries/suspensions) is detected primarily by LLM
extraction from news (Tavily + Gemini), not from API-Football.

**Reason**: verified with real requests — the API-Football free plan returns
"Free plans do not have access to this season" for 2025/2026, so SPEC §3's
assumption (free tier covers current-season fixtures/lineups/injuries) is
factually wrong. Understat lineups (starters vs `position == 'Sub'`) were
verified as a workable substitute for the "key player" rule. Risk accepted:
news-based injury detection is noisier than a structured API — flagged ⚠️
candidates get an inflated edge threshold anyway (SPEC §6), and contradictory
sources already produce a warning flag.

## 2026-10-23 — Phase 0: odds polling is events-gated to fit the free quota

**Decision**: poll The Odds API every 6 h but only for sport keys that have
events commencing within 72 h (via the free `/events` endpoint). Flat fallback:
every 12 h for all 6 keys.

**Reason**: measured credit costs (markets × regions; totals+eu = 1 credit) show
flat 6-hourly polling of 6 sports needs ≈730 credits/month vs the verified
500/month free quota. Events-gated 6 h polling needs ≈310–390/month and keeps
the SPEC cadence. Arithmetic in docs/data-sources.md §7.

## 2026-10-23 — Phase 0: totals lines become per-league "configured ∩ available"

**Decision**: the SPEC's fixed totals set {0.5, 1.5, 2.5, 3.5, 4.5} is treated
as a wish-list: for each league, offer lines that are both configured and
actually returned by The Odds API (verified line sets differ per league, e.g.
no 0.5/1.5 for EPL).

**Reason**: factual market check (odds_api.json): 2.5/3.5 are available
everywhere; 0.5/1.5/4.5 depend on league and bookmaker.

## 2026-10-23 — Phase 0: BTTS odds unavailable free

**Decision**: SPEC §1's BTTS ("free side market") ships as model probability
only unless the user manually enters BTTS odds; no free BTTS odds source could
be verified (The Odds API: `btts` = INVALID_MARKET; API-Football: season
lockout). Revisit in phase 5.

**Reason**: phase-0 checklist requires stating explicitly whether exact score /
BTTS exist in any free odds API — they do not.
