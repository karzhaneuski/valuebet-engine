# Data sources — phase 0 recon

Status date: 2026-10-23. Branch: `phase-0-recon`. All findings below come from
real requests executed on 2026-10-23 (evidence: `reports/recon/*.json`,
scripts: `scripts/recon/`). No API keys are stored in reports.

Legend: ✅ verified · ⚠️ verified with caveats/risks · ❌ not available.

## 1. football-data.co.uk — ✅ verified (no key needed)

Method: downloaded all 30 CSVs (5 leagues × 6 seasons) on 2026-10-23
(`check_football_data.py`, evidence: `football_data.json`).

| League | Code | 2021-22 … 2025-26 | 2026-27 (current) |
|---|---|---|---|
| Premier League | E0 | 380 matches/season | 70 played |
| La Liga | SP1 | 380 | 89 |
| Serie A | I1 | 380 | 70 |
| Bundesliga | D1 | 306 (18 teams) | 54 |
| Ligue 1 | F1 | 380 in 21/22–22/23, 306 from 23/24 (18 teams) | 63 |

- All result columns (`Date`, `HomeTeam`, `AwayTeam`, `FTHG`, `FTAG`, `FTR`) present
  in every file; closing totals fully filled.
- **Closing O/U 2.5**: past seasons have `PC>/<` (Pinnacle), `MaxC>/<`, `AvgC>/<`;
  fill rate 1.0 everywhere. The in-progress 2026-27 files currently have only
  `MaxC>/<` and `AvgC>/<` — current-season CLV must use `AvgC`/`MaxC`.
- ~30 bookmaker column groups for 1X2 (B365, Pinnacle, Betfair exchange, etc.)
  with closing variants; totals limited to the 2.5 line; **no exact-score odds**.
- Dates are UK local time strings; phase 2 must store them as UTC-aware.
- Free, no key, no visible rate limiting (we throttled to 1 req/s).

**Recommendation: use as-is** for results history and closing O/U 2.5 (backtest base).

## 2. Understat xG via soccerdata — ✅ verified (no key needed)

Method: `soccerdata` 2026.43.0, `Understat.read_schedule()`, 5 leagues × 5 seasons
+ direct JSON-API probe for the 6th season (`check_understat.py`, `understat.json`).

API findings (soccerdata 2026.43.0 differs from older docs):

- League codes: `ENG-Premier League`, `ESP-La Liga`, `ITA-Serie A`,
  `GER-Bundesliga`, `FRA-Ligue 1`; seasons as ints (`2022` = 2022/23).
  **Understat has no UCL coverage** — only these five leagues (plus others
  irrelevant to us).
- `read_schedule()` returns per-match `home_xg` / `away_xg`, goals, teams, dates,
  `is_result` — and **includes future (unplayed) matches**, so it doubles as the
  fixtures feed for the current season.
- **Limitation**: soccerdata season discovery lists only the **last 5 seasons**
  (2022/23 … 2026/27). Older seasons are still served by Understat's JSON API
  (`getLeagueData/{slug}/{season}`); the 2021/22 probe worked for all 5 leagues
  (~0.5 s per league-season). Phase 2 must fetch pre-window seasons that way.
- **Lineups fallback**: `read_player_match_stats(match_id)` works for current-season
  matches (verified on a played 2026/27 EPL match): 16 players per team
  (11 starters + 5 subs), substitutes marked `position == 'Sub'`, with minutes,
  xG, cards per player (`understat_lineups.json`). This makes "≥70% starts in
  last 8" computable without API-Football.
- Coverage: all 6 seasons × 5 leagues verified, xG fill ~1.0 (Serie A 2021/22:
  1 of 380 matches lacks xG). Played-match counts match football-data almost
  exactly (one La Liga discrepancy, 88 vs 89 — log mismatches, don't drop).
- Speed/cost: ~3 s per league (5 season pages) on first fetch, ~0.5 s per cached
  page; current-season pages refetch per call by design. Informal source behind
  TLS fingerprinting → use soccerdata (pinned) with a file cache, throttle, and
  refresh a few times per day at most.
- Team spelling differs from football-data (and soccerdata applies its own
  replacements) → `team_aliases` (phase 1) must map both.

**Recommendation: use soccerdata (pinned) with file cache** for xG, fixtures and
lineups; older seasons via the getLeagueData endpoint.

## 3. API-Football — ⚠️ verified: free tier has NO current-season access

Method: `/status`, fixtures/lineups/injuries/odds probes (`check_api_football.py`,
`check_api_football_seasons.py`, evidence: `api_football.json`). Plan: **Free**,
daily limit **100 requests/day** (confirmed via `/status`; the `x-ratelimit-*`
headers are the separate 10 req/min limit).

**Season lockout (blocking finding)**: every request for season 2026 (and 2025)
returns `Free plans do not have access to this season, try from 2022 to 2024.`
Verified explicitly for fixtures and injuries. The free window is **2022–2024**
(2022/23 … 2024/25). Consequences:

- No current-season schedule, results, lineups, injuries or odds from this source
  on the free plan → the live loop cannot use API-Football. Alternatives chosen:
  Understat (fixtures, results, lineups — see §2), football-data (results),
  The Odds API (odds, events).
- On an allowed season the endpoints themselves work fine: lineups returned both
  teams with formations and startXI; fixture injuries returned 8 entries; odds
  for an old fixture returned 0 (odds are simply not kept long-term — expected).
- Remaining value on the free plan: historical data for 2022–2024, **including
  UCL results for the phase-11 league-strength model** (Understat has no UCL).

**Recommendation: keep the key for historical/UCL data only**; revisit a paid
plan only if news-based injury detection proves insufficient (see decisions.md).

## 4. The Odds API — ✅ verified (region eu)

Method: `/sports`, per-market odds probes on `soccer_epl`, totals for the other
sports, `/events`, combined-markets cost check (`check_odds_api.py`, evidence:
`odds_api.json`).

- Free quota: **500 credits/month** (header `x-requests-remaining` = 500 at start).
- Sport keys: `soccer_epl`, `soccer_spain_la_liga`, `soccer_italy_serie_a`,
  `soccer_germany_bundesliga`, **`soccer_france_ligue_one`** (not `ligue_1`!),
  `soccer_uefa_champs_league`.
- **Measured credit costs**: `/sports` and `/events` are **free**; each odds call
  costs `markets × regions` (totals+eu = 1 credit; h2h+totals+eu = 2 credits).
- **Available markets on this key**: `h2h`, `totals`, `spreads`. **`btts`,
  `alternate_totals`, `correct_score` return `INVALID_MARKET`** — not available
  on the free tier (and no alternate endpoint exists). So: **no exact-score and
  no BTTS odds in any free odds API we could verify** → SPEC's exact
  score/multiscore markets stay on manual input; BTTS odds unavailable (see
  decisions.md).
- `totals` returns multiple lines per bookmaker (union across ~13–18 eu
  bookmakers): EPL {2.25–3.5}, La Liga {1.5–4.5}, Serie A {0.5–3.75},
  Bundesliga {2.5–4.5}, Ligue 1 {2.25–3.5}, UCL {2.5–4.5}. Half-lines 2.5/3.5
  everywhere; 0.5/1.5/4.5 league-dependent — phase 5 must configure
  lines per league as "requested ∩ available".
- eu-region bookmakers include **Pinnacle**, William Hill, Marathon Bet, Betsson,
  Tipico, 1xBet, Betfair, Matchbook, Unibet/Winamax variants (~13–24 per market).
- ~18–21 upcoming events per sport at query time; `/events` (free) lists them
  with commence times — use it to gate odds polling.

**Recommendation: use as the totals-odds source** with events-gated polling
(budget in §7).

## 5. Search APIs for team news — ✅ verified

Method: `<home team> injury news team news` on 3 real upcoming EPL fixtures
(taken from The Odds API events), 2026-10-23 (`check_search.py`, evidence:
`search.json`).

- **Tavily**: works on all 3 queries, 5 results each; results are genuinely
  relevant (premierinjuries.com, NewsNow injury feeds, Transfermarkt suspension
  pages) with usable content snippets. Quota: no usage header returned — the
  1000/month basic-plan figure is **declared by the provider, not API-verifiable**.
- **Brave Search** (second candidate): also works on all 3 queries (descriptions
  shorter than Tavily content). Kept as fallback.

**Recommendation: Tavily primary, Brave fallback.**

## 6. Gemini — ✅ verified

Method: model list + one structured-output call on a real news snippet saved by
the search probe (`check_gemini.py`, evidence: `gemini.json`).

- 46 models with `generateContent` reachable by the key, incl.
  `gemini-2.5-flash` (extraction) and `gemini-2.5-pro` (reasoning) — matching
  the SPEC config roles of a fast and a strong model.
- **Structured output works on the exact SPEC §6 schema** (`responseSchema`):
  the model returned a valid JSON array with `player`, `team`,
  `status ∈ {out, doubtful, suspended}`, `source_url`, `quote_summary`
  (4 items extracted from the sample, schema-validated in code).
  Token usage measured: ~255 prompt / ~274 completion for a short snippet.

**Recommendation: use Gemini via the provider abstraction** (phase 6) with
`responseSchema` enforcement + pydantic validation behind it.

## 7. Selection summary & quota budget

### Final source selection

| Data | Source | Status |
|---|---|---|
| Results history + closing O/U 2.5 | football-data.co.uk | ✅ |
| xG per match | Understat via soccerdata (pinned) | ✅ |
| Fixtures/schedule current season | Understat `read_schedule` (+ free TOA `/events`) | ✅ |
| Lineups → key players | Understat `read_player_match_stats` | ✅ |
| Injuries/suspensions | LLM extraction from Tavily news (primary); API-Football 2022–24 only | ⚠️ see decisions.md |
| Totals odds | The Odds API, `totals`, region eu | ✅ |
| Exact score / multiscore odds | manual input (no free source — verified) | manual |
| BTTS odds | not available free → model probability only or manual | ⚠️ see decisions.md |
| News search | Tavily (Brave fallback) | ✅ |
| LLM | Gemini 2.5 flash + pro | ✅ |
| UCL history (phase 11) | API-Football free window 2022–2024 | ✅ |

### Quota budget for the SPEC scenario (top-5 + UCL, 72 h ahead, every 6 h)

**The Odds API (500 credits/month):**

- Flat polling: 6 sports × 1 credit (totals, eu) × 4 polls/day = 24/day ≈
  **730 credits/month — DOES NOT FIT** (146% of the free quota). Even 8h cadence
  (18/day ≈ 547) does not fit.
- **Events-gated 6h polling (recommended)**: `/events` is free — poll odds only
  for sports with matches commencing within 72 h. Typical week: top-5 matches
  Fri–Sun (≈ 3 days × 4 polls × 5 sports = 60) + UCL Tue–Thu (≈ 3 × 4 × 1 = 12)
  ≈ 72–90 credits/week ≈ **310–390/month — fits** with headroom for manual
  checks.
- Flat 12h fallback: 6 × 2 = 12/day ≈ 365/month — also fits, simpler but
  coarser.

**API-Football (100/day):** live loop does not use it (season lockout);
occasional historical pulls (UCL results backfills) are a few dozen requests per
run — fits easily.

**Understat (no quota, informal):** season pages ~6 requests per refresh (5
current + ad hoc), a few refreshes/day; player-match pages ~1 per new match
(~50–70/week) — throttle + permanent cache.

**Tavily (declared 1000/month):** 5–15 value candidates/week × 2 windows
(T-24h, T-2h) = 10–30 searches/week ≈ **40–130/month — fits**.

**Gemini (pay-per-token):** per candidate ≈ ≤6 extraction calls (flash,
~800 in / 300 out each) + 1 justification + 1 shadow JSON (pro). At the top of
the candidate range (15/week ≈ 65/month) ≈ ~420k input + ~140k output tokens
per month ≈ **on the order of $1/month** — negligible.

### Honest bottom line

The free-tier plan is viable with two documented deviations: API-Football is
demoted to historical-only use (injuries come from news extraction instead),
and odds polling must be events-gated (or 12-hourly) to fit the 500-credit
quota. Exact-score odds exist nowhere free — that market lives or dies with
manual input, as SPEC already assumed.
