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

## 2026-10-24 — Phase 2 review S1: 450 is an absolute ceiling for ALL tiers

**Decision**: the hard stop (450 of 500 monthly TOA credits) applies to
every tier including T-3. "T-3 is never disabled" means it is the LAST
degradation step and may not cause the counter to exceed 450 — the atomic
reservation refuses any call that would. Pacing is informational
(`ok | degrade | stop`) with an injectable clock; only the hard stop
refuses calls. Month length uses the real calendar month.

## 2026-10-24 — Phase 2 review S8: API-Football history loader deferred

**Decision** (project owner): the API-Football loader for UCL history
2022–2024 moves to phase 11 together with the UCL model work; phase 2
keeps no API-Football code (current season is blocked on the free plan,
top-5 history comes from football-data + Understat).

## 2026-10-24 — Phase 2 (final round, NIT): UCL /scores harvesting window

**Decision** (phase-7 requirement): UCL results must be fetched via
`/scores?daysFrom=3` no later than 72 hours after each match's kickoff.
The endpoint keeps a short backward window (`daysFrom=3` was verified to
return the previous matchday; older results are not visible), so a missed
window loses the result until another source covers it. The phase-7
scheduler should poll after each UCL matchday and at least twice per week.

## 2026-10-24 — Phase 2 (final round): postponed_suspected is cleared in phase 7

**Decision**: once a kickoff shift is confirmed (>15 min on a
TOA-authoritative kickoff), the flag is never cleared by the events poller —
the original kickoff and the shift moment are persisted
(`matches.original_kickoff_utc`, `rescheduled_at_utc`). Only phase 7 lifts
the status: a result arrives via `/scores`, or the match is played per
Understat. This replaced the interim auto-clear rule (dead code removed).

## 2026-10-24 — Phase 3: training-data selection rules

**Decision** (phase-3 model, dataset side):

- only `status='finished'` matches with a known score enter training —
  this single filter already excludes postponed*, void and scheduled;
- B5 quarantine (fd row refused: date mismatch > 1 day + score
  disagreement) is not persisted in the DB, so the loader takes
  `excluded_match_ids` and the caller parses `match#<id>` entries from
  the ingest report's `football_data[].quarantined` lists
  (`dataset.quarantined_match_ids`); `inverted_pairs` are NOT excluded
  (they name a different, valid db match);
- kickoff is `commence_time_utc` with the strict SQL filter
  `commence_time_utc < as_of`; a finished match with NULL commence time
  is skipped with a warning — we cannot prove it kicked off before
  as_of, and a fallback guess could leak the future in the phase-4
  walk-forward;
- goals come from the score: understat stats first, football-data stats
  as fallback; xG from understat only; matches without xG stay in
  training on goals alone (w=1 for that match);
- the team universe for promoted-team priors is every team in ANY match
  row of the league (any status, any season): a promoted team is
  discovered through its (future) scheduled matches; long-relegated
  teams also receive an inert prior — harmless, and keeps the rule
  deterministic.

**Reason**: phase-3 task additions (phase-2 outcomes) + leak-safety;
verified by tests incl. the bit-for-bit "future match added → parameters
unchanged" test.

## 2026-10-24 — Phase 3: Dixon-Coles likelihood details

**Decision** (math conventions in `core/model/dixon_coles.py`):

- rates: `lam_home = exp(attack_home + defence_away + gamma)`,
  `lam_away = exp(attack_away + defence_home)` — gamma enters the HOME
  rate only (Dixon & Coles 1997);
- identifiability *(corrected 2026-10-25, review S2 — the shifts as
  first written belonged to the draft rate structure with gamma on both
  sides)*: with the correct rates (gamma home-only, plus the mu0
  intercept added later) the likelihood is invariant under
  `(attack+c, defence−c)` and `(defence+c, mu0−c)` shifts; BOTH
  `sum(attack) = 0` and `sum(defence) = 0` are enforced via n-1 free
  parameters each, and mu0 carries the scoring level. The flat-direction
  symptom was real: with gamma on both rates, two fits with gamma −0.55
  and +0.17 had identical likelihood; and without mu0 the double
  constraint pinned the away-side level to 1 (see the intercept entry);
- tau applies to the OBSERVED integer score (0-0/1-0/0-1/1-1); the
  quasi-Poisson part acts on the mixed target `y = w*goals + (1-w)*xG`
  with `lgamma(y+1)` replacing the factorial (non-integer targets are
  the whole point of w<1);
- time decay uses fractional days `(as_of - kickoff)/24h` inside
  `exp(-xi * days)`;
- rho is bounded to [-0.15, 0.15] (empirical league estimates are
  ~|0.05|; the bound keeps tau positive for realistic rate products;
  points with tau <= 0 are rejected with a large objective value);
- `home_advantage_prior` (gamma start) and `promoted_team_prior`
  (weakest-third rule: mean attack/defence of the weakest third by
  attack − defence) are STARTING values/rules from config, both marked
  as phase-4 tunables;
- the analytic gradient (incl. tau chains) is checked against finite
  differences in tests; fitting uses L-BFGS-B with a closed-form start
  (log scored/conceded averages) and tightened tolerances.

**Reason**: review checklist of phase 3 (tau correctness, non-integer
quasi-likelihood, no-leakage, promoted teams); the flat-direction
pitfall was caught by the synthetic-recovery test.

## 2026-10-24 — Phase 3 (step-1 review): intercept mu0 added to the DC rates

**Decision**: the model rates are `log lam_home = mu0 + gamma +
attack_home + defence_away` and `log lam_away = mu0 + attack_away +
defence_home`, with `sum(attack) = 0` AND `sum(defence) = 0` both kept.
`FittedModel` carries and serialises the new `intercept` (mu0) field.

**Reason** (review catch): without mu0 the second constraint pins the
away-side geometric mean rate to exactly exp(0) = 1, so the model
structurally cannot fit a league scoring more than ~2 goals per game —
demonstrated on a synthetic high-mean league (away level 1.5): the old
fit gave mean E[away goals] = 1.09 and inflated gamma to 0.61 to carry
the home level. With mu0 the same data recovers intercept ≈ 0.40,
gamma ≈ 0.24 and the mean scoring level (regression test
`test_high_mean_league_recovers_scoring_level`). mu0 is a fitted
parameter, not a config prior.

## 2026-10-25 — Phase 3 review step A (B1+S1+S7): prior, result lag, season baseline

**Decision**:

1. **B1 — Gaussian prior on team parameters** (in the objective, review
   B1): `objective = -ll + sum_t[(a_t-m_a,t)^2 + (d_t-m_d,t)^2]/(2*sigma^2)`
   with sigma = `ModelConfig.prior_sigma` = 0.5 (starting value: ~1.5x
   the empirical between-team sd of fitted attacks, ~0.33 in the smoke
   fit — strong enough to bound 1-5-match teams, weak enough that >=15
   matches dominate; phase-4 tunable, now in `config_hash`). The prior
   mean m is 0 for teams that played in the last completed league season
   and the weakest-third prior for everyone else (thin or no history),
   so its weight fades on its own as data accumulates. Zero-match teams
   are not fitted parameters at all: they receive the prior value after
   the fit (sum-to-zero constraints never distort them — first attempt
   fitted the whole universe and the packing constraint pushed the last
   team 0.055 off its prior, i.e. ~5% off its lambda; reverted).
   Circuit breaker after each fit: |attack|/|defence| > 3 or any current
   season pair rate outside [0.05, 6] -> warning +
   `FittedModel.suspicious_params` (serialized). Evidence
   (`reports/model/walkforward_stepA_2026-10-25.md`): bin 1-5 matches,
   w=1: -9.839 -> -3.067; w=0.5: -3.240 -> -3.039; all other bins >= as
   good; market-on-model slope 0.744 -> 0.872.
2. **S1 — result lag**: a match is trainable only when
   `kickoff + result_lag < as_of` (`ModelConfig.result_lag_minutes`,
   default 150 = 90 min play + stoppages/reporting buffer; tunable). The
   filter runs inside the loader SQL and in `fit_dixon_coles`'s input
   guard; `LoadStats.n_waiting_result` counts excluded finished rows.
3. **S7 — season-scoped baseline**: the weakest-third prior is computed
   from the teams/matches of the LAST COMPLETED season only (a season is
   complete when its latest training match is >30 days old; fallback:
   all training matches when no season is quiet). `TrainingMatch` now
   carries `season`; the baseline reads that season's target values with
   correct index alignment (a misalignment bug here was caught by the
   unit test).

**Reason**: review B1/S1/S7. The thin-history failure was real and is
regression-tested (`test_one_match_does_not_run_away_from_the_prior`,
`test_three_matches_stay_within_one_sigma_of_the_prior`,
`test_promoted_team_error_falls_with_matches_played`,
`test_result_lag_excludes_matches_still_in_play_window`).

## 2026-10-25 — Phase 3 review S2: why the quasi-likelihood is sound

**Decision** (rationale recorded per review S2):

- The estimating equations `sum_k w_k (y_k - lam_k) x_k = 0` are unbiased
  whenever `E[y] = lam`, so the quasi-MLE is CONSISTENT for the rate that
  matches the mean of the target — for integer y it coincides with the
  ordinary Poisson MLE, and `lgamma(y+1)` is exactly the Poisson pmf
  normaliser extended off the integers. The model therefore estimates
  `lam = w*E[goals] + (1-w)*E[xG]`, not "goals" or "xG" alone.
- Verified on the DB copy (2024-2026, `scripts/model_smoke`-style SQL):
  Understat `sum(xG)/sum(goals)` = **1.078 / 1.101 / 1.092** per season,
  so mixing xG in lifts the fitted scoring level by ~
  `(1-w)*(ratio-1)` = +3.9..5.0% at w=0.5 — the main reason the smoke
  "model vs actual goals" deltas were positive (xG converts slightly
  above goals), compounded by recency weighting.
- rho is practically insensitive to w: refitting all five leagues at
  w=0.5 vs w=1.0 moves rho by at most 0.014
  (`scripts/model_rho_check.py`).
- What the quasi-likelihood does NOT model: dispersion (the variance
  stays the Poisson one, no phi parameter). Calibration of the resulting
  probabilities is a phase-4 deliverable (reliability/Brier).
- Phase-4 options recorded: (a) rescale xG by `sum(goals)/sum(xG)`
  computed strictly up to as_of before mixing; (b) fit the level
  parameters (mu0, gamma) on goals only while keeping team attack/
  defence on the mixture.

**Reason**: review S2; all numbers from real runs on a DB copy.

## 2026-10-25 — Phase 3 review S3: rho/gamma are weakly identified per league

**Decision** (recorded measurement + phase-4 plan):

- Observed-information SEs at the optimum (2x2 (gamma, rho) block of the
  inverted numeric Hessian of the penalized objective, xi=0.005,
  `scripts/model_rho_check.py`): se(rho) = **0.090..0.104**, se(gamma) =
  **0.056..0.064** across the five leagues. So per-league rho estimates
  (−0.087..+0.023) are within ~1 SE of each other and of a common value.
- Serie A's positive rho (+0.023) is noise: at xi=0 the same league fits
  rho = −0.039 (and Bundesliga moves −0.029 → −0.110) — well inside
  2*SE. No league shows a rho reliably away from the pack.
- Phase-4 plan: try (a) a COMMON rho across leagues (one parameter, much
  tighter SE) and/or (b) a separate, slower time decay for the
  league-level parameters (rho, gamma) than for team strengths; pick by
  walk-forward log loss.

**Reason**: review S3; measured, not assumed.

## 2026-10-25 — Phase 2 follow-up (0.1/0.2/0.3): understat roster glitch, fd report fields

**Decision**:

1. Understat served match 30804 (db match 4215, Villarreal - Deportivo,
   2026-09-05) with every roster row duplicated (61 rows, 32 unique
   players; copies differ only in surrogate row ids / roster_in refs).
   `lineup_rows` now deduplicates to one row per player_id (max minutes);
   `failed_matches` entries carry `ExceptionClass: message` instead of
   the class alone.
2. football-data reports always serialize `quarantined` and
   `inverted_pairs` (empty lists when empty). A far-date fd row is
   journalled into `date_mismatches` only AFTER the inverted/quarantine
   resolution - a row already classified there no longer also dangles in
   date_mismatches. The 10559 case ("fd Rennes v Paris SG 2026-08-23")
   is an INVERTED-PAIR record of the played PSG-Rennes (match 10363,
   same date), not a postponed Rennes home fixture: with the fixed
   ordering it lands in inverted_pairs (verified on a DB copy).
3. The lineups backfill is offline-replayable: one Understat reader per
   league-season, its schedule frame pinned after a force_cache read
   (soccerdata bypasses the disk cache for the current season and
   re-reads the schedule inside every read_player_match_stats call);
   soccerdata loggers quieted to WARNING.

**Reason**: night-run part 0 (0.1/0.3 intertwined: the dedup regression
test needs injectable readers); report-field and classification fixes
from 0.2.


## 2026-10-25 — Phase 4: backtest, tuning, honest test result

**Decision**: walk-forward infrastructure (weekly refits, on-disk model
cache keyed by config hash, warmup 2021-22 / validation 2023-24 / test
2025-26 split, test period file-locked until FROZEN_CONFIG.json).
Tuning on validation only: xi*w grid (16), prior_sigma (9), model
options from the phase-3 review (xG goals-scaling, goals-only level,
common rho, slower league decay - all implemented as ModelConfig flags,
off by default). Every variant was statistically tied with the simplest
config (paired week-clustered bootstrap of the log-loss difference,
CI95 spanning 0), so the frozen config is xi=0.002, w=0.5,
prior_sigma=0.5, no options (validation LL 2.9112 vs 2.9148 baseline).
Test (ONE run, after the freeze): frozen LL 2.9157 vs baseline 2.9221 -
tuning transferred out of sample; bins 0/1-5/6-15/>15 =
2.722/3.129/2.858/2.908. Calibration on test: top-1 exact score
p=0.118 vs observed 0.126, top-3 set 0.311/0.332, {1:0,2:0,2:1}
0.239/0.248, 2-4 goals 0.608/0.614.
ROI on O/U 2.5 vs football-data closings (de-vigged AvgC market,
thresholds 2/4/6/8%): AvgC execution -9..-11% (CI excludes 0), MaxC
-4..-6% (CI spans 0) - NO edge over the closing market; expected and
accepted (the phase question was asked honestly). config.yaml updated
with the frozen values.

**Reason**: phase-4 task; all numbers from real runs on DB copies
(reports/backtest/*, records under .cache/bt_records).


## 2026-10-26 — Phase 4 review S2: the test period was touched by phase-3 work

**Decision/acknowledgement**: before the phase-4 freeze, the window
2025-08..2026-10 was scored by scripts/model_walkforward.py (phase-3
review step A) and by the phase-3 reviewer (prior_sigma=0.5 chosen,
w=1.0 rejected on that window's evidence). The frozen config itself was
selected on validation only, but the test period is NOT pristine - treat
phase-4 test numbers as mildly optimistic. Mitigation now in code: a
DATE lock in core.backtest (ensure_window_allowed - any walk-forward
window ending on/after 2025-07-01 refuses to run without
FROZEN_CONFIG.json; runner.predictions and scripts/model_walkforward.py
both gate through it; tested). FROZEN_CONFIG.json now records the real
freeze-commit timestamp as frozen_at (2026-10-25T00:39:51Z, commit
fc789a8). Part B adds a "second look" label to its single test run.

**Reason**: review S2; leakage hygiene must be structural, not
behavioural.


## 2026-10-26 — Phase 4 review S4/S5: thresholds, MaxC limit, margins

**Decision**:
1. No O/U 2.5 threshold (2/4/6/8%) produces a positive ROI on the test
   period at ANY executable price; the raw model therefore emits NO
   O/U signals in phase 5 - totals probabilities are reference output
   only (the phase-4b market mixture revisits this formally).
2. MaxC is documented as an unreachable UPPER bound: on validation
   1104/3504 matches (31.5%) show a riskless fork 1/MaxO+1/MaxU < 1;
   never quote MaxC ROI as a scenario (reports/backtest/part3_margin.json).
3. AvgC margin differs by period (4.79% validation vs 6.53% test);
   cross-period ROI comparisons are invalid - always normalise by the
   fair ROI -m/(1+m).

**Reason**: review S4/S5; numbers from the DB copy runs above.

## 2026-10-26 — Phase 4b B1: pre-match totals stored in their own table

**Decision**: football-data pre-match O/U 2.5 columns (Avg>2.5/Avg<2.5,
P>2.5/P<2.5, Max>2.5/Max<2.5 - the no-C families) are loaded into a NEW
`prematch_odds` table (migration a86d9da4539e) instead of a kind flag on
closing_odds. Reasons: the bookmaker sets differ (Avg/P/Max vs
AvgC/MaxC/PC), closing_odds carries a CHECK constraint bound to the
closing families, and CLV code that filters by bookmaker alone would
silently mix prematch rows into closing references. The fd loader now
upserts prematch rows idempotently next to the closing ones
(prematch_written/prematch_updated in FdReport). Load verified on a DB
copy: 21,017 rows written (Avg 7,427 / Max 7,427 / P 6,163), rerun
updates 0. The main DB is NOT touched - owner command in SUMMARY_4b.

**Reason**: phase-4b task B1 (hybrid needs a pre-match lambda source
that matches the bot's 1-3-days-ahead betting scenario).

## 2026-10-26 — Phase 4b B2/B3: hybrid matrix beats the pure model; draw fix

**Decision** (all numbers on VALIDATION only, prematch Avg as the live
scenario):
- B2 hybrid = proxy_score_matrix(lambda_total from the de-vigged
  PRE-MATCH Avg over/under 2.5, home share from the model's expected
  goals, rho from the model). Exact-score log loss 2.9067 vs model
  2.9112: delta -0.0045, paired calendar-week bootstrap CI95
  [-0.0088, -0.0001] (reviewer's numbers reproduced: -0.0045 and
  -0.0103 with closing AvgC, CI [-0.0153, -0.0052]). The market total
  level carries real information the model lacks.
- B3 draw correction: the model and the hybrid underprice 2:2 (model
  cell z +3.2, hybrid +3.4; p 0.049 vs observed 0.062) and slightly
  overprice 0:0 (hybrid z -1.9). Honest caveat (review S3): κ_high is
  statistically indistinguishable by log loss (-0.0013 [-0.0030, +0.0002])
  across 15 variants × 16 cells searched, so the |z|<=2 criterion is an
  in-sample fit. Cross-fitting κ by season (2023 -> 2024: k_opt = 0.337,
  eval diff -0.0003; 2024 -> 2023: k_opt = 0.179, eval diff -0.0023;
  same for the market matrix: k_opt ≈ 0.18-0.34, test delta consistently
  negative) confirms that κ_high in [0.18, 0.34] transfers stably across
  seasons. Choosing κ_high=0.20 departs from "simpler is better" for a
  principled a priori reason: bivariate Poisson Dixon-Coles is well-known
  to underestimate high draws due to independent Poisson tails, so
  inflating high draws corrects a known structural deficit rather than
  fitting noise.

**Reason**: phase-4b B2/B3; artefacts reports/backtest/4b_hybrid_*.json.

## 2026-10-26 — Phase 4b B4: O/U signals are reference-only (mixture + CLV)

**Decision**:
- On validation, the in-sample fitted logit mixture on O/U 2.5 puts
  w_model = 0.18 vs w_market = 1.01 (prematch Avg); in-sample binary
  log loss 0.6647 is tied with the pure market (diff CI95 [-0.0021,
  +0.0006]). Out-of-sample cross-validation (train 2023 -> test 2024:
  mixture 0.6699 vs market 0.6688; train 2024 -> test 2023: mixture
  0.6613 vs market 0.6620; overall OOS mixture 0.6656 vs market 0.6654,
  CI95 [-0.0018, +0.0021]) confirms the mixture does NOT beat the pure
  market out of sample.
- CLV of the model's >=2%-edge bets against PREMATCH prices (selected by
  p * prematch_price - 1 >= 0.02, measuring shift prematch -> closing,
  ties separated; review B1 a): 1,533 bets, line moved toward us in
  726 (47.4% of all bets, 48.5% excluding ties), moved against in 772
  (50.4%), tied (shift 0) in 35 (2.3%); mean probability shift is -0.00 pp
  (-0.00005). The earlier claim of "systematic move away (21%)" was a bug
  from selecting bets on closing prices. CLV is neutral (no market edge).
- Conclusion for phase 5: O/U totals probabilities remain REFERENCE OUTPUT
  ONLY — the value engine emits no O/U recommendations because CLV is
  neutral and the mixture does not beat the market.

**Reason**: phase-4b B4; reports/backtest/4b_mixture.json.

## 2026-10-26 — Phase 4b B5/B6: hybrid transfers to the test; phase-5 consequences

**Decision** (single SECOND-LOOK test run after freezing
FROZEN_CONFIG_4b; the window was already used by phases 3/4):
- hybrid (prematch-Avg lambda + model shape + kappa_high 0.20) test
  exact-score LL 2.9043 vs pure model 2.9158: delta -0.0115,
  CI95 [-0.0163, -0.0068] - the validation gain TRANSFERRED (and is
  larger than validation's -0.0058). Every 0:0..3:3 cell within |z|<=2
  (diagonal: 0:0 -0.5, 1:1 +0.2, 2:2 -0.3); multiscore calibration:
  top-1 0.119/0.131 (z ≈ 1.7), top-3 0.315/0.329 (z ≈ 1.4),
  {1:0,2:0,2:1} 0.242/0.248, 2-4 goals 0.610/0.614.
  Underestimating top scores means tail scores are overpredicted; this
  can generate false value signals on long odds (review S4).

**Phase-5 consequences (B6 rewritten, review B1 d)**:
1. Base exact-score matrix in phase 5 is built ENTIRELY FROM MARKET
   ODDS: total lambda from O/U 2.5 devigged, home goal share from
   1X2 devigged, plus high-draw correction κ_high (0.20). The statistical
   model Dixon-Coles is NOT in the matrix: validation evidence shows
   market 1X2 share beats model share by -0.0157 [-0.0205, -0.0113],
   all-market beats model share by -0.0163 [-0.0213, -0.0118], and
   cross-fitting the model's weight α in the share yields α = 0.0000 in
   both directions. The model serves as: (a) standby reserve if market
   odds are missing, and (b) anomaly flag when |share_model - share_1x2|
   is large.
2. Value definition: value = discrepancy between Fortuna's exact-score and
   multiscore prices and the fair market matrix.
3. Benchmark for phase 5: fair matrix derived from Pinnacle event-level
   correct_score grid (TOA) devigged.
4. Prefilter (phase-0 decisions S2) redefined: the earlier
   |P_hybrid - P_proxy| is degenerate (proxy = hybrid). The new ranking
   rule selects candidate matches by divergence between model share and
   market share |share_model - share_1x2| or divergence on totals,
   capped at top 15 matches/week, without purchasing event-level odds.
5. Live odds source and credit budget: sport-level /odds polls
   `markets=totals,h2h` (2 markets × 1 region eu = 2 credits/snapshot).
   5 leagues × 3 snapshots (T-48, T-24, T-3) × 2 credits = 30 credits/round,
   ~120 credits/month. Adding candidate event-level correct_score (15/week ×
   4 × 1 = 60) and UCL /scores (8 credits) yields ~188 credits/month,
   well below the 450 hard-stop ceiling.
6. 1X2 odds role: 1X2 is used STRICTLY AS INPUT to construct the fair
   matrix. Bets on 1X2 are NEVER issued (SPEC §1).
7. Quality arbiter for phase 7: CLV measured against our own exact-score
   price snapshots before kickoff. Realised ROI is informational only
   (at average odds ~9, a ±5% CI requires ~12,000 bets).
8. Test period lock: after phase 4b, the test period (from 2025-07-01) is
   exhausted. backtest_hybrid and backtest_mixture enforce
   FROZEN_CONFIG_4b.json. Subsequent evaluation will occur exclusively
   forward (phase 7 forward tracking).
9. Data timing: football-data prematch odds can be captured later than
   the weekly walk-forward as_of (Friday 06:00 UTC); this reflects
   pre-kickoff market consensus, not match outcome leakage. PrematchOdds
   constraints and comments are general, not restricted to O/U.

**Reason**: phase-4b B5/B6; reports/backtest/4b_test_second_look.json,
reports/backtest/4b_hybrid_market_1x2.json, reports/backtest/4b_hybrid_all_market.json.
