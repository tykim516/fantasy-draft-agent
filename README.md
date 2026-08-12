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
uv run pytest                                    # 252 tests
uv run python scripts/serve.py                   # local draft UI on :8000
```

To refresh ADP: replace `config/market/adp.csv`, update
`sources.adp_file.as_of` (and `provider`, if the source changed) in `config/sources.yml`, and re-run
`uv run python scripts/ingest.py --sources sleeper`. See `config/market/README.md`.

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
config/market/       the hand-maintained ADP file and its alias map
src/ffdraft/         warehouse, league, scoring, ingest/, metrics/, market/
src/ffdraft/web/     local draft UI (FastAPI + a static page, no build step)
sql/                 named queries agents call by filename
scripts/             ingest.py, validate_league.py, query.py, serve.py
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

## The draft UI

```bash
uv run python scripts/serve.py        # http://127.0.0.1:8000
```

A local web board for running a live draft. Click a player to mark him taken,
shift-click to mark him yours; state persists to `data/draft_state.json` and
survives a restart, because a draft is one shot.

It runs at two speeds, kept deliberately separate:

| | What it does | How long |
|---|---|---|
| **Board / Compare tabs** | Read the warehouse directly — ECR, ADP, tiers, usage, survival-to-your-next-pick | instant |
| **Commands tab** | Shells out to `claude -p "/board …"`, fans out to sub-agents | minutes, costs tokens |

Use the native board while the clock is running and the agent runs the night
before. `/refresh` is the exception: it is `data-ingest` and nothing else, so the
UI calls `scripts/ingest.py` directly rather than burning an agent turn to shell
out to a script.

**The native board is market-ordered, not projection-ordered, and says so.** It
ranks by ECR with ADP, tiers and usage beside it. It shows no `proj_pts` and no
VOR, because this project produces neither outside an agent run — inventing them
in the UI would mean inventing numbers.

Set your draft slot and every row gets a `next pick` read (`likely` / `toss-up` /
`gone`) computed from ADP against your actual snake picks — from slot 5 in a
10-team league those are 5, 16, 25, 36. The read is coarse on purpose: ADP is a
central tendency with real variance, and a precise probability would be false
confidence.

The server binds to `127.0.0.1` and refuses any other host. It has no auth and
can start subprocesses, so it must not be exposed on a network interface.

## Commands

- `/board [league] [--slot N] [--top N]` — full fan-out, synthesis, timestamped
  export to `data/exports/`
- `/refresh [--seasons ...]` — `data-ingest` only, prints the freshness table
- `/compare <player-a> <player-b>` — head-to-head on usage, market, and news

Board output carries an assumptions block (scoring, league size, roster slots,
both market sources with their own dates), then
`rank, player, pos, team, tier, proj_pts, vor, ecr, adp, ecr_vs_adp, confidence, why`,
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
| `adp_deltas` | What does he cost, and when does he leave the board? ECR and ADP side by side, rounds at *this* league size, and where the two disagree |
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

**ADP** arrives as a file a human maintains at `config/market/adp.csv`, because
no free API publishes it — Sleeper has no ADP endpoint and `nflreadpy` has no ADP
loader. It is the only hand-edited input in the project; see
`config/market/README.md`.

The board keeps ECR and ADP as separate columns and never averages them. ECR is a
*value* anchor (where experts say a player should go) and prices the board; ADP
is an *availability* anchor (where he actually goes) and drives slot-survival
math. `ecr_vs_adp` is the gap, and it is the most actionable column on the board
— a player experts rank higher than the market falls further than his ECR implies.

**How much the ADP is worth depends on who published it**, so `provider` in
`config/sources.yml` is a real setting rather than a label, and it rides onto
every row as `adp_source` alongside `adp_format_note`:

- ADP from **the platform this league drafts on** approximates what the other
  nine managers see on screen while picking. Availability claims are strong.
- ADP from **anywhere else** describes a different crowd, possibly at a different
  league size. Still a real price, and the ECR-vs-ADP gap is still informative,
  but not evidence about your specific opponents.

The current file is **CBS Sports** while the league drafts on **Sleeper**, so the
second case applies and the agents are instructed to hedge availability claims
accordingly. Its league size is not stated in the export.

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
uv run pytest                     # 252 tests
uv run pytest -m "not warehouse"  # 194 unit tests, no ingest required
uv run pytest -m warehouse        # 58 integration tests against data/ff.duckdb
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

- **ADP is a hand-maintained file and goes stale.** Sleeper publishes no ADP
  endpoint (`/players/nfl/adp` and `/adp/nfl/{season}` both 404) and `nflreadpy`
  has no ADP loader, so `config/market/adp.csv` is copied in by a human
  and dated by hand in `config/sources.yml`. Ingest warns past `max_age_days`,
  but nothing can refresh it automatically. ADP moves fast in August.
- **ADP covers ~217 players against ECR's ~490.** Past the end of the export
  there is no ADP and the board is priced on ECR alone. Those rows carry a null
  `adp`, never a substituted one. 217 is still comfortably deeper than the 140
  players this league rosters.
- **ADP rounds come from the adjusted rank, not the average pick.** The export
  publishes an average pick observed in *other people's* drafts, whose league
  size and excluded positions are not this league's. That number cannot be
  divided into rounds here without importing their format, so `adp_round` uses
  the rank recomputed over this league's universe. The raw average pick is still
  returned, and the survival read uses it against the observed `Hi/Lo` range.
- **ECR links to `gsis_id` for ~81% of the full list**, via
  `ff_playerids.fantasypros_id`. This is now the binding constraint on ADP
  coverage at the top of the board: six of the top 120 ECR players have no ADP,
  and five of them *are* in the ADP file — the miss is on the ECR side, because
  `ff_playerids` has no `fantasypros_id` for them. They are 2026 rookies
  (Jeremiyah Love, Carnell Tate, Jordyn Tyson, Makai Lemon, Jadarian Price).
  Unlinked players are returned flagged, never dropped.
- **The Sleeper draft-scraping proxy is dormant.** It remains in the code and
  needs seed ids under `sources.sleeper.adp_proxy.draft_ids`; Sleeper exposes no
  endpoint that enumerates public drafts. The file supersedes it.
- **Special-teams forced fumbles and fumble recoveries are not scored
  separately.** nflverse team stats do not split forced fumbles by phase. Both
  settings score zero and are listed in `scoring.KNOWN_GAPS`.
- **The agents have not been run end to end.** They are validated as correct —
  frontmatter matches spec, every embedded command executes — but sub-agents load
  at session start, so `/board` needs a restart before its first run.
