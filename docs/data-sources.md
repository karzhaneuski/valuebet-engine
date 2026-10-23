# Data sources — phase 0 recon

Status date: 2026-10-23 (updated same day after review). Branch: `phase-0-recon`.
All findings come from real requests; evidence lives in `reports/recon/*.json`,
scripts in `scripts/recon/` regenerate every number quoted below. Numbers not
measured by us are marked "declared by provider". No API keys in reports.

Legend: ✅ verified · ⚠️ verified with caveats/risks.

## 1. football-data.co.uk — ✅ verified (no key needed)

Method: downloaded all 30 CSVs (5 leagues × 6 seasons) on 2026-10-23
(`check_football_data.py` → `football_data.json`).

| League | Code | 2021-22 … 2025-26 | 2026-27 (current) |
|---|---|---|---|
| Premier League | E0 | 380 matches/season | 70 played |
| La Liga | SP1 | 380 | 89 |
| Serie A | I1 | 380 | 70 |
| Bundesliga | D1 | 306 (18 teams) | 54 |
| Ligue 1 | F1 | 380 in 21/22–22/23, 306 from 23/24 (18 teams) | 63 |

- Result columns (`Date`, `HomeTeam`, `AwayTeam`, `FTHG`, `FTAG`, `FTR`) present
  in every file.
- **Closing O/U 2.5 — use AvgC/MaxC, not Pinnacle**: `AvgC>2.5` is filled 1.0
  in all 30 files; `PC>2.5` (Pinnacle closing) is well filled only through
  2024-25 (0.96–1.0), drops to **0.46–0.55 in 2025-26 and is absent in
  2026-27** (football_data.json, `pc_over_fill`). Backtests and CLV must be
  computed on `AvgC` (and `MaxC` as the upper bound).
- Totals are limited to the 2.5 line; **no exact-score odds** here.
- 1X2 bookmakers actually present (base column codes → names per the official
  `notes.txt`, `fd_notes.json`): Pinnacle (P/PS), Bet365 (B365), William Hill
  (WH), Skybet (SK*), Betfred (BFD), Betfair (BF) + Betfair Exchange (BFE),
  BetMGM (BMGM), BetVictor (BV; VC = VC Bet → BetVictor), Bet&Win/bwin (BW),
  Coral (CL), Interwetten (IW), Ladbrokes (LB), Paddy Power (PP), 1XBet (1XB),
  plus Avg/Max aggregates; most with closing (C) and Asian-handicap (AH)
  variants.
- Dates are UK local time strings; phase 2 must store them as UTC-aware.
- Free, no key, no visible rate limiting (we throttled to 1 req/s).

**Recommendation: use as-is** for results history and closing O/U 2.5 (backtest
and CLV base), on `AvgC`/`MaxC`.

## 2. Understat xG via soccerdata — ✅ verified (no key needed)

Method: `soccerdata` **2026.43.0 (pinned in pyproject)**, `read_schedule()`,
5 leagues × 5 seasons + a direct JSON-API probe for the 6th season
(`check_understat.py` → `understat.json`); lineups probe
(`check_understat_lineups.py` → `understat_lineups.json`); schedule/timezone
alignment against TOA events (`odds_budget_sim.py` → `odds_budget.json`).

Coverage and API facts:

- League codes: `ENG-Premier League`, `ESP-La Liga`, `ITA-Serie A`,
  `GER-Bundesliga`, `FRA-Ligue 1`; seasons as ints (`2022` = 2022/23).
  **No UCL coverage.**
- xG per match (`home_xg`/`away_xg`) for all 6 seasons (2021/22 … 2026/27),
  fill ~1.0 (Serie A 2021/22: 379/380). Match counts line up with
  football-data almost exactly (one La Liga current-season discrepancy,
  88 vs 89 — log, don't drop).
- soccerdata's season discovery covers only the **last 5 seasons**; 2021/22
  was fetched through Understat's JSON API `getLeagueData/{slug}/{season}`
  (~0.5 s per league-season, `understat.json` `elapsed_s`). **Phase 2 note
  (N6): the recon calls the reader's private `_read_league_season`; the
  production loader must call the `getLeagueData` endpoint itself (or another
  public surface) and not depend on private API of the pinned library.**
- Lineups: `read_player_match_stats(match_id)` works for current-season
  matches (played 2026/27 EPL match verified): 16 players/team, substitutes
  marked `position == 'Sub'`, with minutes/xG/cards — the "≥70% starts in
  last 8" key-player rule is computable from Understat alone.

Schedule role and time semantics (S5, evidence `odds_budget.json`
`understat_alignment`, `toa_events.json`):

- `read_schedule()` includes **future matches**, so it is the full-season
  fixture list. **There is no match status field**, and **far-future rounds
  carry placeholder kickoff times** (observed: whole rounds of 10 matches all
  at 15:00 while near rounds have real spread; see `toa_events.json` vs
  `understat` in `odds_budget.json`).
- **Timezone**: matched 94 TOA events to Understat entries (best-|Δ| within
  ±48 h, fuzzy team names): median Δ = 0 min, 86% exactly 0 — Understat naive
  datetimes are effectively UTC. Nonzero deltas cluster at **−24…−30 h**
  (10 cases): these are **placeholder days** (Understat places far rounds on
  a default day while the real round spans Fri–Mon), not postponements;
  small deltas (minutes…hours) are kickoff-time drift or short postponements.
- **Authoritative kickoff times: TOA `/events`** (free). Postponement rules
  for phase 2: event disappeared from `/events` before start, or
  `commence_time` shifted > 15 min, or no result by KO+3 h → mark
  `postponed?`; recommendations older than 48 h without a result are voided.
- Team names differ from both football-data and TOA spellings: 82
  Understat↔TOA name pairs recorded in `odds_budget.json`
  (`understat_alignment.team_name_pairs`, e.g. "FC Cologne" vs
  "1. FC Köln", "Roma" vs "AS Roma") — seed material for the phase-1
  `team_aliases` table, which must map football-data + Understat + TOA
  spellings.
- Informal source behind TLS impersonation (soccerdata ships a
  TLS-impersonating HTTP client — library fact, not our measurement). Use the
  pinned library with a file cache and throttling; refresh a few times/day.

**Recommendation**: xG, lineups and the season fixture list via soccerdata
(pinned) + `getLeagueData` for pre-window seasons; kickoff times and
postponements from TOA `/events`.

## 3. API-Football — ⚠️ verified: free tier has NO current-season access

Method: `/status`, fixtures/lineups/injuries/odds probes
(`check_api_football.py`, `check_api_football_seasons.py` →
`api_football.json`). Plan **Free**, daily limit **100 requests/day**
(`/status`); the `x-ratelimit-*` headers are a separate 10 req/min limit.

**Season lockout (blocking finding)**: every request for season 2025 or 2026
returns `Free plans do not have access to this season, try from 2022 to
2024.` Verified for fixtures and injuries. The free window is exactly
**2022–2024 = three seasons (2022/23, 2023/24, 2024/25), without 2025/26**.

- On allowed seasons the endpoints work: lineups (both teams, formation,
  startXI), fixture injuries (8 entries), and **UCL 2024/25 fixtures are
  accessible (44 matches in a Jan–Feb 2025 window, real pairings)** — this is
  the free source of UCL history for phase 11.
- Odds for an old fixture return 0 rows (odds are not kept long-term).
- Live loop (current-season fixtures/results/lineups/injuries/odds) cannot
  use API-Football on the free plan → Understat + football-data + TOA cover
  it; injuries come from news extraction (decisions.md).

**Recommendation: keep the key for historical 2022–2024 data and UCL
history only.**

## 4. The Odds API — ✅ verified (region eu)

Method: `/sports`, sport-level market probes, `/events`, cost measurements
(`check_odds_api.py`), event-level market probes + Ligue 1 + `/scores` +
Fortuna check (`check_odds_followup.py`) — all evidence in `odds_api.json`.

- Free quota: **500 credits/month** (`x-requests-remaining` = 500 at start).
- Sport keys: `soccer_epl`, `soccer_spain_la_liga`, `soccer_italy_serie_a`,
  `soccer_germany_bundesliga`, **`soccer_france_ligue_one`**, 
  `soccer_uefa_champs_league`.
- **Measured credit costs**: `/sports` and `/events` **0 credits** (delta
  verified); each `/odds` call costs `markets × regions` — totals+eu = 1,
  h2h+totals+eu = 2. **The cost does not depend on the number of events
  returned or the requested horizon.**
- **Sport-level markets** (`/sports/{sport}/odds`): `h2h`, `totals`,
  `spreads`. `btts`/`alternate_totals`/`correct_score` are INVALID_MARKET
  here.
- **Event-level markets** (`/sports/{sport}/events/{eventId}/odds`) — review
  B2 finding: **`btts`, `correct_score`, `alternate_totals` all work** at 1
  credit per market×region per event, incl. Pinnacle: BTTS yes/no prices, a
  full correct-score grid (~25–101 outcomes per bookmaker; Pinnacle grid up
  to 5:5), alternate totals with points {0.5, 1.0, 1.5, 1.75, 2.0, 2.25,
  2.5, 2.75, 3.0, 3.25, 3.5, 3.75, 4.0, 4.5, 5.0, 5.5, 6.5, 7.5} (1xBet,
  Codere, Coolbet, Pinnacle, William Hill, BetOnline.ag among 5–6 books).
  So exact-score/BTTS odds ARE available free — per event, for candidates
  (budget in §7); sport-wide polling of them does not fit the quota.
- `totals` (sport-level) returns one main line per bookmaker; the union
  across books per league (measured): EPL {2.25–3.5}, La Liga {1.5–4.5},
  Serie A {0.5–3.75}, Bundesliga {2.5–4.5}, Ligue 1 {2.25–3.5}, UCL
  {2.5–4.5}. Whole and quarter lines (2.0, 2.25, 2.75 …) occur — see the
  half-lines-only decision in decisions.md.
- `/scores` (UCL): **verified — completed matches ARE visible**. A
  `daysFrom=3` call (no `daysTo`, 2 credits) returned 27 events including
  **9 finished matches of the 2026-10-21 matchday with final scores**
  (e.g. Como 4–1 Manchester United), plus the upcoming 03–04.11 matchday
  (`followup.ucl_scores`). Polling rule (decisions.md): one call after each
  matchday + one mid-week ≈ 8 credits/28d.
- eu-region bookmakers observed: Pinnacle, William Hill, Betclic, Tipico,
  Betsson, 1xBet, Betfair (sportsbook), Matchbook, BetOnline.ag, MyBookie.ag,
  GTbets, Unibet/Winamax/PMU/Codere/LeoVegas/NordicBet variants, Coolbet,
  888sport, Marathon Bet. **Fortuna (efortuna.pl): NOT present under any
  name in region eu** (`fortuna_check` in `odds_api.json`). Offshore or
  commission exchanges, excluded from reference and best-odds (decisions.md):
  1xBet (Curaçao), BetOnline.ag, MyBookie.ag, GTbets (US offshore), Betfair,
  Matchbook (exchanges with commission); Pinnacle is Curaçao-licensed but is
  deliberately kept as the sharp reference (decisions.md).

**Recommendation**: totals via sport-level calls (snapshot policy, §7);
btts/correct_score/alternate_totals per candidate event.

## 5. Search APIs for team news — ✅ verified

Method: `<home team> injury news team news` on 3 real upcoming EPL fixtures,
2026-10-23 (`check_search.py` → `search.json`).

- **Tavily**: works on all 3 queries, 5 results each; sources are on-topic
  (premierinjuries.com, NewsNow injury feeds, Transfermarkt suspensions).
  Quota: no usage header — 1000/month basic plan is **declared by provider**.
- **Brave** (fallback): works on all 3 queries, shorter descriptions.
  Free tier 2000/month, 1 req/s — **declared by provider** (not verifiable
  via API response).
- Quality caveats (honest): results include stale template pages (the saved
  sample still listed a player "expected back 12 Oct" for a 23 Oct match) and
  aggregators that link out rather than state facts. The Gemini prompt now
  carries the match date and drops players whose expected-back date precedes
  it (§6); for phase 6 also: query with opponent and match date
  ("<team> vs <opponent> team news <date>"), prefer recency-filtered/news
  topics, and cross-check against a second source before flagging.
- Budget with 2 teams per candidate (home AND away), 2 windows per match:
  5–15 candidates/week × 2 searches × 2 windows = **20–60 searches/week ≈
  86–260/month** vs Tavily 1000 declared — fits.

## 6. Gemini — ✅ verified

Method: model list + per-candidate `GET /models/{name}` metadata + probe
calls + two structured-output tests (`check_gemini.py` → `gemini.json`).

- 46 models with `generateContent` reachable; stable candidates by NAME
  pattern `gemini-<version>-flash|pro` (no -latest/-preview/-experimental —
  stability is decided by the name; the metadata has no stability flag):
  `gemini-2.5-flash/pro`, `gemini-3.5/3.6/3.7/3.8-flash`. Per-candidate
  metadata recorded; note `version` of gemini-3.8-flash is **"3.0"**
  (`stable_candidates`).
- **Selection is explicit and availability-probed**: `gemini-2.5-pro` is
  listed by /models but **returns 404 "no longer available to new users"**
  on call — so reasoning_model falls back per the agreed chain to the same
  flash with an elevated thinking budget (4096).
  - extraction_model: **gemini-3.8-flash** (newest stable flash)
  - reasoning_model: **gemini-3.8-flash + thinkingBudget 4096** (no stable
    pro is callable with this key; 3.x-pro exist only as previews, excluded
    from the main loop; gemini-2.5-pro fallback is dead — 404).
- **Structured output works on the exact SPEC §6 schema** with both picks
  (`responseSchema`, all five fields, status enum) — schema-valid in both
  tests.
- **Match-date-aware extraction (S7)**: prompt includes the match date and
  the rule "expected back before match date ⇒ do not include". The first
  phase-0 run (no date) wrongly returned `suspended` for a player expected
  back 11 days before the match; with the date both tests exclude him
  (`stale_case_pass: true`), while players without a return date correctly
  stay `out`.
- Token usage incl. thinking (usageMetadata): extraction test 300 prompt /
  199 candidates / **433 thoughts**; reasoning-mode test 300/192/**518**.
  Cost estimates must use the totals incl. thoughts (~0.9–1.0k tokens/call).
- Deferred to phase 6 (decisions.md S5): the real shadow-adjustment schema
  test and the `thinkingLevel` parameter; every shadow record must store
  model, version and thinking config.

## 7. Selection summary & quota budget

### Final source selection

| Data | Source | Status |
|---|---|---|
| Results history + closing O/U 2.5 (AvgC/MaxC) | football-data.co.uk | ✅ |
| xG per match, lineups, season fixture list | Understat via soccerdata (pinned) | ✅ |
| Kickoff times, postponements, near-horizon fixtures | TOA `/events` (free) | ✅ |
| Injuries/suspensions | LLM extraction from Tavily news (primary) | ⚠️ decisions.md |
| Totals odds (all matches) | TOA sport-level `totals`, snapshot policy | ✅ |
| btts / correct_score / alternate_totals odds | TOA event-level, candidates only | ✅ (B2) |
| Fortuna odds (final edge) | manual input (Fortuna ∉ TOA eu) | decisions.md |
| News search | Tavily (Brave fallback) | ✅ |
| LLM | Gemini 3.8-flash (extraction) / 3.8-flash+thinking (reasoning) | ✅ |
| UCL history 2022–2024 | API-Football free window | ✅ |

### The Odds API credit budget (B1, `odds_budget.json`)

Cost model: 1 credit = 1 market × 1 region per `/odds` call; **independent of
event count and horizon** (measured). `/events` and `/sports` are free.

Two 28-day windows simulated minute-by-minute: **forward** (2026-10-23 →
11-20, contains a 12-day international break, `windows.forward_28d`) and
**no-break** (2026-11-20 → 12-18, `windows.no_break_28d`). Event times: real
where TOA lists them; Understat placeholder rounds re-timed from the
empirical (weekday, kickoff) distribution of real rounds ("retimed" —
primary); the "asis" variant keeps placeholders (lower bound). **Honesty
note: in the no-break window all kickoff times beyond TOA's horizon are
synthetic (re-timed placeholders)** — only the fixture DATES are real.
UCL matchdays beyond TOA's horizon are cadence-based estimates (flagged);
estimated matchdays falling into international-break windows are dropped
(`events_input.*.soccer_uefa_champs_league.estimated_matchdays_dropped_in_break`,
e.g. 2026-11-17/18 inside the Nov 9–20 break, `international_break_windows`).

Credits per 30 days (retimed / asis; keys `policies.<name>.<window>.per_30d`):

| Policy | no-break /30d | forward /30d | vs 500 quota |
|---|---|---|---|
| (a) flat 6h | 720 | 720 | ❌ 144% |
| (b) flat 12h | 360 | 360 | ✅ |
| (c) events-gated 6h/72h | **442** / 275 | 285 / 240 | ✅ tight |
| **(d) snapshots T-48/24/3, per-kickoff slot — DEFAULT** | **239** / 81 | 162 / 123 | ✅ |
| (d-day) same, T-3 per match day — degradation only | 214 / — | 144 | ✅ |
| (e) (d) + T-1h | 360 / — | 265 | ✅ tight |
| (f) (d) + T-72h | 302 / — | 201 | ✅ |

Corrections to the earlier draft: with a 72 h horizon the gate is open for
most of the week (weekend rounds span Fri–Mon), not "≈3 days × 4 polls" —
that arithmetic was wrong and is withdrawn.

Comparison with the reviewer's expectations (c ≈480–520, d ≈170–200,
no-break, /30d): (c) simulated 442 — below the range: the gate is quantized
to 6-hour ticks and the specific Nov 20–Dec 18 fixture list has slightly
fewer Friday/Monday matches than a "typical" week; per-sport totals are in
`policies.c_gated_6h_72h.no_break_28d.per_sport`. (d) simulated 239 — above
the range: the narrow T-3 window [1;4] h needs a fresh snapshot per kickoff
slot; treating the whole match day as one slot gives 214 — the difference is
purely slot granularity, both variants are reported.

**Default policy (decisions.md, final round S1): (d) with per-kickoff-slot
T-3** — day-slots would leave evening matches with a 6–9 h stale last
snapshot (measured from `toa_events.json`, 2026-10-25: Barcelona–Real Madrid
20:00 vs day-first-kickoff 13:00 → 8 h; Udinese–Milan 19:45 vs 11:30 →
9 h 15 m), corrupting pseudo-CLV and post-T-2h-news freshness. Factual
firing semantics of the simulation (lazy, last minute of each window):
snapshots effectively land at **T-40 / T-18 / T-1**; phase 5 must fire no
later than lower bound + 15 min with one retry.

Combined budget scenarios (`budget_scenarios`, policy (d) no-break base +
per-candidate T-24 `correct_score,btts` (2 credits/event) + `/scores` 8/28d):

| Scenario (per 30d) | key |
|---|---|
| 5 cand/week → **290** | `budget_scenarios.candidates_5_per_week.combined_t24_30d` |
| 10 cand/week → **333** | `budget_scenarios.candidates_10_per_week.combined_t24_30d` |
| 15 cand/week → **376** | `budget_scenarios.candidates_15_per_week.combined_t24_30d` |
| 15 cand/week + optional T-1 → **440** | `budget_scenarios.candidates_15_per_week.combined_t24_t1_30d` |

Degradation ladder and pacing rule at the 450 hard stop: decisions.md
(final round S3). Candidate prefilter BEFORE buying event odds: decisions.md
(final round S2).

TOA quota state after all recon probes: **20 of 500 used, 480 remaining**
(2026-10-23). Breakdown by run: 9 (`check_odds_api.py` probes) + 3 (inline
Ligue 1 key probing, incl. one request whose response failed to parse
client-side and was repeated) + 4 (`check_odds_followup.py` first run:
btts/correct_score/alternate_totals/Ligue 1) + 2 (`/scores` daysFrom=1..7
probe) + 2 (`/scores` daysFrom=3 probe, final round). Monthly reset date:
**not checked** (dashboard-only; recorded as "не проверено" — decisions.md).

### Other quotas

- **API-Football (100/day)**: historical pulls only (occasional dozens per
  backfill) — fits.
- **Understat**: no quota; ~6 season pages per refresh + ~1 request per new
  match for lineups, throttled and cached.
- **Tavily (1000/mo declared)**: 86–260/month — fits.
- **Gemini (pay-per-token)**: ~1.0k tokens incl. thoughts per extraction
  call (measured); per candidate ≤ 6 extraction + 1 justification + 1 shadow
  calls ⇒ at 15 candidates/week ≈ ~0.9M tokens/month — still on the order of
  a few dollars (provider price list applies; declared, not measured).

### Honest bottom line

Free-tier plan is viable: totals odds via the snapshot policy (d, per-slot
T-3), exact-score/BTTS/alternate-totals odds per candidate from the event
endpoint after a prefilter (a review-driven discovery that reopens the
SPEC's exact-score market beyond manual input), UCL results from `/scores`
(verified), final edge against Fortuna via manual input (Fortuna absent
from TOA eu). Open items: TOA quota reset date (dashboard-only).
