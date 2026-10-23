# Project decisions (deviations from SPEC)

Every entry: date, decision, reason. Append new entries at the bottom.

## 2026-10-23 — Phase 0: live loop without API-Football current season

**Decision**: API-Football is used only for historical data (free plan covers
exactly seasons 2022–2024; verified — 2025 and 2026 are blocked, including
UCL current season). The live pipeline (fixtures, results, lineups, key
players) runs on Understat via soccerdata + football-data.co.uk; kickoff
times and postponements from The Odds API `/events`. Player availability
(injuries/suspensions) is detected primarily by LLM extraction from news
(Tavily + Gemini).

**Reason**: verified with real requests — the API-Football free plan returns
"Free plans do not have access to this season" for 2025/2026. Understat
lineups (starters vs `position == 'Sub'`) were verified as a workable
substitute for the "key player" rule. Risk accepted: news-based injury
detection is noisier than a structured API — flagged ⚠️ candidates get an
inflated edge threshold anyway (SPEC §6), and contradictory sources already
produce a warning flag.

## 2026-10-23 — Phase 0 (review B1): odds polling policy — snapshots (d), not flat 6h

**Decision**: replace SPEC's illustrative "every 6 h for 72 h ahead" flat
polling with the snapshot policy (d): per sport key, snapshots at T-48
(window [40;56] h), T-24 ([18;30] h) and T-3 ([1;4] h) before each start
slot, merged per sport (one `/odds` call covers all events of a sport). For
T-3 a start slot is a match day (variant d-day in the simulation); T-48/T-24
windows merge across the weekend round as implemented. Budget on real events
(`odds_budget.json`): 215/30d (no-break window) + per-candidate event-market
snapshots (correct_score+btts at T-24, 43–129/30d) = 258–344/month.

- Kickoff times and postponements come from `/events` (free), polled every
  6 h, plus a recheck at T-4 h before each candidate match.
- **Hard stop**: if 450 of the 500 monthly credits are used, degrade in the
  order T-48 → T-24; **T-3 is never skipped** (it feeds CLV-critical last
  snapshots).
- Monthly quota reset date: **не проверено** (not verifiable via API; needs a
  one-time check in the provider dashboard).
- Per-kickoff-slot T-3 variant costs 240/30d (also fits) — chosen default is
  the cheaper day-slot variant.

**Reason**: measured cost model (1 credit = markets × regions, independent
of event count/horizon) + minute-level simulation on real events: flat 6h
needs 720/30d (144% of quota), flat 12h 360, gated 6h/72h 448 (fits but
leaves no room for candidate markets), snapshots 215–240.

## 2026-10-23 — Phase 0 (review B1): CLV metric definition

**Decision**: for O/U 2.5 the primary closing-line metric is
`CLV_fair = recommended_odds × p_fair(AvgC) − 1`, where `p_fair` is the
de-vigged (margin-free) closing probability from football-data's `AvgC`
pair; secondary: `odds / MaxC − 1`. For other totals lines and UCL (no
historical closing odds from football-data), compute **pseudo-CLV** against
our own last odds snapshot before kickoff, and store `clv_source`
(e.g. `avgc`, `maxc`, `own_snapshot_t3`) on every CLV number.

**Reason**: Pinnacle closing (`PC>2.5`) is unreliable since 2025-26
(fill 0.46–0.55, absent in 2026-27 — measured), so AvgC/MaxC are the honest
reference; explicit provenance prevents mixing reference qualities.

## 2026-10-23 — Phase 0 (review B2): exact-score and BTTS odds ARE available per event

**Decision**: sport-level `/odds` keeps serving `totals` (all matches);
`btts`, `correct_score`, `alternate_totals` are fetched **per candidate
event** from `/sports/{sport}/events/{eventId}/odds` (1 credit per market ×
region), one snapshot at T-24 (optionally T-3 while quota allows). Manual
input remains supported but is no longer the only source for exact
score/multiscore odds. Supersedes the earlier "BTTS odds unavailable" entry.

**Reason**: review-driven probe on the correct endpoint returned all three
markets (incl. a full Pinnacle correct-score grid); the earlier INVALID_MARKET
result was the sport-level endpoint, not an entitlement failure.

## 2026-10-23 — Phase 0 (review S10): Fortuna as the only bettable bookmaker

**Decision** (user constraint: betting only at Fortuna, efortuna.pl, Polish
license; other bookmakers are market reference only):

- `market_reference`: **Pinnacle de-vigged + consensus of allowed bookmakers
  de-vigged** — used to find candidates and for de-vig context.
- `bookmaker_whitelist`: **[fortuna]** — the final edge is computed against
  the Fortuna odds: from the API if Fortuna ever appears there (checked
  2026-10-23: **not present in region eu under any name**), otherwise from
  the user's manual input.
- Offshore bookmakers (1xBet, BetOnline.ag, MyBookie.ag, GTbets) and
  commission exchanges (Betfair, Matchbook) participate in neither the
  reference nor the "best odds". Pinnacle is Curaçao-licensed but is kept as
  the sharp reference by explicit decision (reference-only, never bet).
- Phase-5 config keys: `market_reference_books`, `bookmaker_whitelist`,
  `excluded_books`.

**Reason**: the user bets exclusively at Fortuna; edges against books he
cannot bet are meaningless as final numbers.

## 2026-10-23 — Phase 0 (review S11): totals — half lines only in v1

**Decision**: only half lines (x.5) are supported for totals markets. The
sport-level `totals` market returns one main line per bookmaker and both
whole (x.0) and quarter (x.25/x.75) lines occur; whole and quarter lines are
filtered out and logged (not silently dropped). Integer-line push handling
and quarter-line splitting are deferred.

**Reason**: whole lines need push semantics and quarter lines need half-stake
splitting — out of scope for v1; filtering keeps the model side clean.

## 2026-10-23 — Phase 0 (review S4): UCL current-season data gap

**Decision**: UCL current-season schedule comes solely from TOA `/events`
(free); results — TOA `/scores` (measured 2 credits per 1–7-day call,
forward window only). **Open item**: whether a finished matchday remains
visible in `/scores` after it ends is not verified; probe once after the
2026-10-27/29 matchday. If unavailable, UCL results need another source
(none identified free so far) — UCL is experimental (phase 11) and may stay
totals-only with delayed settlement.

**Reason**: Understat has no UCL; API-Football current season is blocked;
no other free source was verified in phase 0.
