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

> **Partially superseded** by "odds polling — default (d), T-3 per kickoff
> slot" (2026-10-23, final round): the default T-3 granularity is per kickoff
> slot; the day-slot variant is only a degradation step. Budget keys below
> refer to the pre-final numbers; current numbers live in
> `reports/recon/odds_budget.json` (`policies.*`).

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

**Reason**: the user bets exclusively at Fortuna; edges against bookmakers
the user cannot bet are meaningless as final numbers.

## 2026-10-23 — Phase 0 (review S11): totals — half lines only in v1

**Decision**: only half lines (x.5) are supported for totals markets. The
sport-level `totals` market returns one main line per bookmaker and both
whole (x.0) and quarter (x.25/x.75) lines occur; whole and quarter lines are
filtered out and logged (not silently dropped). Integer-line push handling
and quarter-line splitting are deferred.

**Reason**: whole lines need push semantics and quarter lines need half-stake
splitting — out of scope for v1; filtering keeps the model side clean.

## 2026-10-23 — Phase 0 (review S4): UCL current-season data gap

> **Superseded** by "UCL current-season data — /scores verified" below
> (2026-10-23, final round): the matchday date is 03–04.11 (not 27–29.10)
> and the `/scores` open item is closed.

**Decision**: UCL current-season schedule comes solely from TOA `/events`
(free); results — TOA `/scores` (measured 2 credits per 1–7-day call,
forward window only). **Open item**: whether a finished matchday remains
visible in `/scores` after it ends is not verified; probe once after the
2026-10-27/29 matchday. If unavailable, UCL results need another source
(none identified free so far) — UCL is experimental (phase 11) and may stay
totals-only with delayed settlement.

**Reason**: Understat has no UCL; API-Football current season is blocked;
no other free source was verified in phase 0.

## 2026-10-23 — Phase 0 (final round S1): odds polling — default (d), T-3 per kickoff slot

**Decision**: the default odds policy is **(d) with T-3 per kickoff slot**
(`odds_budget.json` → `policies.d_snapshots.no_break_28d.per_30d` = 239
credits/30d; forward window 162). The day-slot variant
(`policies.d_snapshots_day_slots.no_break_28d.per_30d` = 214) is **only a
degradation step**, not the default.

**Why not day-slots by default**: with day-granular T-3 the last snapshot
fires at T-1 of the day's FIRST kickoff, so evening matches get a stale
coefficient exactly when the T-2h news pass may have changed things —
measured from `toa_events.json` (2026-10-25): La Liga first kickoff 13:00,
Barcelona–Real Madrid 20:00 → snapshot at 12:00, **8 h stale**; Serie A first
kickoff 11:30, Udinese–Milan 19:45 → snapshot at 10:30, **9 h 15 m stale**.
This also corrupts pseudo-CLV (last snapshot far from the close).

**Factual firing semantics** (from the simulation, lazy firing at the last
minute of each tolerance window): snapshots are effectively taken at
**T-40 / T-18 / T-1** (lower bounds of the [40;56]/[18;30]/[1;4] h windows).
**Phase-5 requirement**: fire no later than the window's lower bound + 15
minutes, with exactly one retry on error, and record the actual firing
offset per snapshot.

**Option (off by default)**: a T-1 snapshot of the market we are actually
betting on, for candidates only — 1 credit per event
(`budget_scenarios.per_event.t1_bet_market_credits`).

## 2026-10-23 — Phase 0 (final round S2): prefilter before buying event-market odds

**Decision** (phase-5 requirement): candidates for per-event markets
(correct_score / btts / alternate_totals) are selected BEFORE their odds are
purchased:

1. take the sport-level T-24 totals snapshot (already paid for by policy (d));
2. de-vig it (Pinnacle + consensus of allowed bookmakers);
3. derive the market's total λ from the de-vigged over/under probabilities;
4. build a proxy score distribution from λ, splitting it into home/away
   parts with the model's home/away share;
5. a match becomes a candidate if **|P_model(BTTS) − P_proxy(BTTS)| ≥ δ**
   or the model edge on a half-line total is **≥ ½ of that market's
   threshold**;
6. rank candidates, cap at **15 per week** (config), check remaining TOA
   credits, and log every prefilter decision (accepted/rejected + reason).

`δ` and the weekly cap go to config.yaml.

**Reason**: per-event markets cost 1 credit each per market×region; without a
prefilter the candidateExtras budget (`budget_scenarios`) is unbounded.

## 2026-10-23 — Phase 0 (final round S3): quota degradation ladder and pacing

**Decision**: hard stop accounting starts at **450 of 500 monthly credits**.
Degradation order when the stop (or the pacing rule below) triggers:

1. per-candidate additional markets: cap at top-5 per week;
2. disable the T-48 tier;
3. disable the T-24 tier;
4. T-3: per-kickoff-slot → per-day;
5. **T-3 is never disabled** (last snapshot feeds pseudo-CLV and the final
   recommendation odds).

**Pacing rule**: before each paid call, check `remaining / days_left_in_month`;
if the projected burn exceeds the remaining quota (treating `/scores` calls —
2 credits each, `budget_scenarios.scores` — as part of the burn), enter the
ladder early instead of waiting for 450. Reference point: at 15
candidates/week with the optional T-1 enabled the combined burn is
`budget_scenarios.candidates_15_per_week.combined_t24_t1_30d` = **440/30d** —
already at the stop, so T-1 stays off by default and on only when pacing
allows.

## 2026-10-23 — Phase 0 (final round S4): UCL current-season data — /scores verified

**Decision**: UCL schedule — TOA `/events` (free); **results — TOA `/scores`
with `daysFrom=3`** (2 credits per call): verified on 2026-10-23 that the
call returns recently **completed** UCL matches with final scores (9 finished
matches of the 2026-10-21 matchday, e.g. Como 4–1 Manchester United —
`odds_api.json` → `followup.ucl_scores`). Polling rule: one `daysFrom=3`
call after each matchday plus one mid-week (≈4 calls/28d = 8 credits,
`budget_scenarios.scores`). The next matchday is **03–04.11** (per
`toa_events.json`). The earlier open item is closed.

**Reason**: direct probe; closes the "UCL results source" gap without
API-Football (current season blocked there).

## 2026-10-23 — Phase 0 (final round S5): reasoning model is a flash with thinking

**Decision** (deviation from SPEC §3 "модели задаются в конфиге" — the
reasoning slot is filled by a flash-class model): 
`reasoning_model = gemini-3.8-flash + thinkingBudget 4096`, fallback
`gemini-3.7-flash`; `gemini-2.5-pro` is listed by /models but returns 404
("no longer available to new users"). Stability is determined by the model
NAME (stable = `gemini-<version>-flash|pro` without -latest/-preview/
-experimental; metadata has no stability flag) and availability by a probe
call; note the metadata `version` of gemini-3.8-flash is "3.0"
(`gemini.json` → `stable_candidates`). **Every shadow_llm record must store
model, version and the thinking config** used for the call. The real
shadow-adjustment schema test and the `thinkingLevel` parameter are phase-6
requirements (not validated in phase 0).

**Reason**: no stable pro-class model is callable with this key; previews
are excluded from the main loop by agreed policy; the fallback keeps the
reasoning slot upgradeable when a stable pro returns.


## 2026-10-23 — Phase 1 (review S4): football-data row matching rule

**Decision** (requirement for phase 2): football-data.co.uk rows are matched
to `matches` by **the team pair within a league season** (home/away aliases);
`Date`+`Time` columns are parsed as **Europe/London local time and converted
to UTC**; the date is used only as a ±1-day validation check of the pairing,
never as the join key. Phase-0 evidence: Understat placeholder days and TOA
commence times differ from CSV dates by up to a day; a date-joined match
would double-count or miss postponed games. The same rule is duplicated in
the phase-2 task text in PROMPTS.md.

**Reason**: review S4; there is no stable fixture id in the CSVs, and dates
alone are unreliable (postponements, placeholder kickoffs).

## 2026-10-23 — Phase 1 (review S9): local package renamed mcp/ → mcp_server/

**Decision**: the MCP service package is `mcp_server/` (module
`mcp_server.main`), not `mcp/`. This deviates from the SPEC §9 layout block
(`mcp/ MCP-сервер`) so that the official MCP Python SDK (package name `mcp`,
phase 8) can be imported without being shadowed. Compose, CI, Dockerfile and
tests use the new path.

**Reason**: a local `mcp/` package shadows the SDK of the same name; the
rename is the smallest change that keeps both importable.

## 2026-10-24 — Phase 2: Understat archive adapter (one private call isolated)

**Decision**: pre-window seasons (2021/22) are downloaded once through
Understat's public JSON endpoint `getLeagueData/{slug}/{season}` and stored
forever under `data/raw/understat/`. The endpoint only answers with the
library's `X-Requested-With` header, which the public `reader.get()` does
not send, so the adapter (`core/ingest/understat_archive.py`) makes a single
call to `reader._request_api` — the ONLY private-API usage, isolated in that
one module as agreed. `_read_league_season` is NOT used; payload parsing
lives in the adapter.

**Reason**: soccerdata's discovery window covers only the last 5 seasons;
the archive keeps 6 seasons of history without re-hitting the informal
source.

## 2026-10-24 — Phase 2: cross-source alias policy is deterministic-only

**Decision**: automatic team matching applies exactly three deterministic
rules — exact alias, normalization (fixed abbreviation table, stop tokens
without "club"/"de"), and single-candidate contiguous containment ("Brighton"
⊂ "Brighton and Hove Albion"). Anything similarity-based is NEVER applied
automatically; it lands in the ingest report for manual confirmation. The
phase-0 fuzzy mistake (Racing ↔ Athletic Club) is covered by a regression
test. Also: the first TOA correction of an Understat placeholder kickoff is
NOT a postponement — the shift rule only applies when the stored kickoff was
already TOA-authoritative.

**Reason**: review requirement (no auto-fuzzy); observed false
"postponed_suspected" flags during the first real events run.

## 2026-10-24 — Phase 2: /scores creates missing UCL matches

**Decision**: finished UCL matchdays drop out of `/events` before `/scores`
exposes them, so `apply_scores` creates the match (league `ucl`, TOA names
canonical, stage `league_phase`) when it is missing, then writes the result.

**Reason**: 9 completed matches of the 2026-10-21 matchday were visible in
`scores daysFrom=3` but had no match rows (verified in the phase-2 run).
