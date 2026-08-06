# fantasy-draft-agent

A tiered fantasy football draft board built from open NFL data and tuned to one
specific league's settings. An orchestrator plus five sub-agents turn nflverse,
Sleeper, and FantasyPros data into a board that says *why* it disagrees with the
market rather than claiming to beat it.

The premise: ADP is a large crowd pricing public information, and it is usually
close to right. The edge available here is not better prediction — it is a board
computed against the correct baselines. Every public ranking assumes 12 teams.
This league has 10, which moves replacement level at every position.

## Quickstart

```bash
uv sync                                          # Python 3.11+, deps from pyproject
uv run python scripts/validate_league.py --summary
uv run python scripts/ingest.py                  # builds data/ff.duckdb (~30s, ~28 MB)
uv run python scripts/ingest.py --freshness      # what loaded, how old
uv run pytest                                    # 128 tests
```

No API key is required. FantasyPros is optional; everything else is free and
unauthenticated.

## The league

10-team redraft, **full PPR**, FAAB waivers, 6-team playoffs from week 15, trade
deadline week 12. Lineup: QB, RB, RB, WR, WR, TE, FLEX, FLEX, DEF — **9
starters, 5 bench, 2 IR, no kicker**, 14 roster spots.

The config at `config/leagues/main.yml` is the single source of truth, validated
against `config/leagues/_schema.yml`. **Nothing in the codebase hardcodes a
league setting.** Validation fails loudly: a null slot count is an error, never a
silent zero, because a setting that quietly defaults produces a board that is
wrong in a way nobody notices.

Six consequences the metrics actually implement:

| Constraint | What it changes |
|---|---|
| 10 teams x 14 spots = 140 rostered | Baselines computed from `teams x slots`; far higher than 12-team defaults |
| 5 bench spots | Contingent roles and handcuffs cost 20% of the bench each; waiver pool is rich, so insurance picks are near-worthless |
| 2 IR slots | A known absence stashes without consuming bench — those players are *more* draftable here |
| 2 FLEX on 2RB/2WR | 20 leaguewide flex slots; allocation is modelled from projected points, not assumed (lands WR-heavy in full PPR) |
| 1 QB, 1 TE, no K | Replacement sits at QB11 / TE12 — wait on both absent a tier break; kickers excluded from the universe entirely |
| INT -2, stacked DST bands | Turnover-prone passers lose value; DST yards-allowed stacks on points-allowed (+5 to -7), making streaming valuable but not draft capital |

Kicker exclusion is *derived*, not hardcoded: any position with zero dedicated
slots that no flex slot accepts is dropped from the player universe. Set `K: 1`
and kickers return — and the schema then demands a `kicking` scoring block.

## Architecture

```
CLAUDE.md            orchestrator — decomposes, dispatches, assembles. No analysis.
.claude/agents/      five sub-agents
.claude/commands/    /board, /refresh, /compare
config/              league config + schema, pinned seasons and source settings
src/ffdraft/         warehouse, league, scoring, ingest/, metrics/
sql/                 named queries agents call by filename
scripts/             ingest.py, validate_league.py, query.py
data/                gitignored; rebuilds from ingest
```

| Agent | Model | Tools | Owns |
|---|---|---|---|
| `data-ingest` | sonnet | Bash, Read, Write, Edit, Glob, Grep | Refreshing the warehouse |
| `usage-analyst` | sonnet | Bash, Read, Glob, Grep | Opportunity + efficiency |
| `market-analyst` | sonnet | Bash, Read, Glob, Grep | ADP, ECR, VOR, tiers |
| `news-scout` | sonnet | WebSearch, WebFetch, Read | Injuries, depth charts |
| `ranking-synthesizer` | opus | Bash, Read, Write, Glob, Grep | Merging into the board |

**Tool grants are deliberately narrow.** The analysts have no write access and no
web access, so an agent cannot resolve a disagreement by mutating the warehouse.
`news-scout` has web but no database, so scraped text cannot reach the tables.
`ranking-synthesizer` is the only agent that assigns a rank; the others return
measurements.

The orchestrator dispatches `usage-analyst`, `market-analyst`, and `news-scout`
**concurrently in one turn**, then hands their findings to
`ranking-synthesizer`. Only the synthesizer waits. Every dispatch carries the
full league config, not just that agent's slice — scoring format and roster slots
change every agent's answer.

## Commands

- `/board [league] [--slot N] [--top N]` — full fan-out, synthesis, timestamped
  export to `data/exports/`
- `/refresh [--seasons ...]` — `data-ingest` only, prints the freshness table
- `/compare <player-a> <player-b>` — head-to-head on usage, market, and news

Board output carries an assumptions block (scoring, league size, roster slots,
market source and date), then
`rank, player, pos, team, tier, proj_pts, vor, adp, adp_delta, confidence, why`,
then a "biggest divergences from market" section. Confidence is a required
column. Tiers lead, because presenting #14 as better than #16 when the gap is a
third of a point is false precision.

## Named queries

`sql/` holds metric definitions as files, called by filename:

```bash
uv run python scripts/query.py --list
uv run python scripts/query.py usage_profile --season 2025 --limit 20
uv run python scripts/query.py points_over_expected --season 2025 --min-games 8
uv run python scripts/query.py adp_deltas --limit 30
uv run python scripts/query.py roster_context --season 2025 --csv
uv run python scripts/query.py usage_profile --show      # print the SQL
```

| Query | Answers |
|---|---|
| `usage_profile` | What is his role? Season rates, last-six window, and the trend between them |
| `points_over_expected` | Did he earn it? Actual minus expected, with component splits |
| `adp_deltas` | What does he cost? Market rank, round in *this* league size, consensus spread |
| `roster_context` | Is the role good, or is the player good? Team share, depth chart, IR eligibility |

If agents compose SQL fresh each run, two invocations will compute target share
slightly differently and nobody will notice. Metric definitions must be diffable.
`--teams` and `--exclude-positions` default from the league config, so no query
hardcodes a league assumption.

## Data sources

**nflverse** is the backbone — free, no key, via `nflreadpy` (`nfl_data_py` is
deprecated; do not substitute it). `ff_opportunity` is the highest-value table:
actual-minus-expected is the cleanest free regression signal available.

**Sleeper** public API supplies the player master list with cross-platform ids
and trending adds/drops. **FantasyPros** requires `FANTASYPROS_API_KEY` and skips
cleanly when unset.

Three derived tables are built during ingest because no free source provides
them directly: `pbp_redzone` (red-zone and inside-10 touches, aggregated from
play-by-play), `dst_stats` (team defense scoring inputs, including points and
yards allowed which live on the opponent's row), and `depth_chart_current`.

**Never join on player name.** Joins go through `gsis_id`, falling back to
`ff_playerids`. Suffix and collision mismatches corrupt a board silently.

### Attribution

Player, usage, and expected-points data from [nflverse](https://github.com/nflverse),
licensed **CC-BY 4.0**. Expert consensus rankings via ffverse `ff_rankings`.

This project does not currently load FTN charting data. If `load_ftn_charting` is
ever added, FTN Data must be credited via nflverse under **CC-BY-SA 4.0**.

## Verification

Scoring is checked two ways. Unit tests assert hand-computed values for real 2024
stat lines — Barkley wk7 (26.7), Chase wk8 (20.4), Lamar wk12 (22.58), plus four
real team-defense lines through the stacked points-allowed and yards-allowed
bands. Separately, scoring the full 2024 season reproduces nflverse's own PPR
column to the decimal.

A test also asserts the scorer is *config-driven*: halving `receiving.reception`
must halve the reception contribution. Matching another library's PPR number
would not prove that — the two agree only by coincidence of settings.

```bash
uv run pytest                     # 128 tests
uv run pytest -m "not warehouse"  # 99 unit tests, no ingest required
uv run pytest -m warehouse        # 29 integration tests against data/ff.duckdb
```

Integration tests skip cleanly when the warehouse is absent. They check the
properties that would let a board be quietly wrong: kickers leaking into the
universe, rookies vanishing, the crosswalk silently dropping players, league
averages drifting out of credible range.

## Gotchas

- **Sub-agents load at session start.** Editing a file in `.claude/agents/`
  requires restarting Claude Code. Edits made through `/agents` apply
  immediately.
- **DuckDB is single-writer.** If ingest runs while a session holds the warehouse
  open you will hit lock contention. Use `--staged`, which builds a temp file and
  atomically renames it over the live warehouse.
- **Seasons are pinned** in `config/sources.yml` (history `2023–2025`, draft
  season `2026`) so a board is reproducible. `--seasons` overrides them and says
  loudly that the result is not.
- **Exports are timestamped** in `data/exports/` so this week's board can be
  diffed against last week's.
- **`data/` is fully gitignored** and rebuilds from ingest.
- **Preseason gaps are normal.** The draft season has no stats, snap counts, or
  injury report yet; ingest keeps the completed seasons and reports which it
  dropped rather than failing.
- **`depth_charts` is a snapshot table** carrying every scrape since March (138
  as of this build). Use `depth_chart_current` and report its `as of` timestamp.

## Known limitations

These are stated rather than papered over, because a hidden gap is worse than a
declared one.

- **The board is priced against ECR, not ADP, by default.** `ff_rankings` is
  FantasyPros expert consensus — where experts say a player should go, not where
  he actually goes. True ADP needs `FANTASYPROS_API_KEY`.
- **The Sleeper ADP proxy needs seed draft ids.** Sleeper exposes endpoints to
  read a draft by id but none that enumerate public drafts, so the proxy cannot
  discover them. Add ids under `sources.sleeper.adp_proxy.draft_ids` to enable
  it; until then it reports unavailable rather than publishing an ADP with
  nothing behind it.
- **ECR links to `gsis_id` for ~81% of the full list.** The top of the board
  links near-perfectly; misses are mostly rookies the crosswalk has not picked up.
  Unlinked players are returned flagged, never dropped.
- **Special-teams forced fumbles and fumble recoveries are not scored
  separately.** nflverse team stats do not split forced fumbles by phase. Both
  settings score zero and are listed in `scoring.KNOWN_GAPS`.
- **The agents have not been run end to end.** They are validated as correct —
  frontmatter matches spec, every embedded command executes — but sub-agents load
  at session start, so `/board` needs a restart before its first run.
