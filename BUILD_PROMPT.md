# Build prompt — paste into Claude Code

> Run from an empty `~/Projects/fantasy-draft-agent/`. Start Claude Code there,
> then paste everything below the line.

---

Build a fantasy football draft-ranking system in this directory. It's an
orchestrator plus five sub-agents that turn open NFL data into a tiered draft
board for my specific league. Use `uv` for dependency management and Python
3.11+.

## Build order

Work in this sequence and stop after each phase so I can review:

1. Scaffold + `pyproject.toml` + `.gitignore` + league config
2. `src/ffdraft/` package and `scripts/ingest.py`, then run a real ingest
3. `sql/` named queries, validated against the populated warehouse
4. `.claude/agents/` and `.claude/commands/`
5. `tests/` and README

## Directory structure

```
fantasy-draft-agent/
├── CLAUDE.md                    # orchestrator instructions
├── README.md
├── pyproject.toml
├── .env.example                 # FANTASYPROS_API_KEY=
├── .gitignore                   # data/, .env, __pycache__, .venv
│
├── .claude/
│   ├── agents/                  # five sub-agent definitions
│   ├── commands/                # board.md, refresh.md, compare.md
│   └── settings.json            # allow Bash(python scripts/*), Bash(uv *)
│
├── config/
│   ├── leagues/
│   │   ├── main.yml             # my league (contents given below)
│   │   └── _schema.yml          # required keys; validation fails loudly
│   └── sources.yml              # pinned seasons, cache TTLs, enabled sources
│
├── src/ffdraft/
│   ├── __init__.py
│   ├── warehouse.py             # DuckDB connection, freshness checks
│   ├── league.py                # parse + validate league YAML
│   ├── scoring.py               # apply a league's scoring to raw stat lines
│   ├── ingest/
│   │   ├── nflverse.py
│   │   ├── sleeper.py
│   │   └── fantasypros.py
│   └── metrics/
│       ├── usage.py             # snap share, target share, WOPR, RZ touches
│       ├── vor.py               # value over replacement, league-aware
│       └── tiers.py             # gap-based clustering
│
├── scripts/
│   ├── ingest.py                # thin CLI over src/ffdraft/ingest
│   └── validate_league.py
│
├── sql/                         # named queries agents call by filename
│   ├── usage_profile.sql
│   ├── points_over_expected.sql
│   ├── adp_deltas.sql
│   └── roster_context.sql
│
├── data/                        # gitignored; rebuilds from ingest
│   ├── raw/                     # cached API responses
│   ├── ff.duckdb
│   └── exports/                 # timestamped generated boards
│
└── tests/
```

## Data sources

**nflverse is the backbone.** Free, no API key, CC-BY 4.0. Use the
`nflreadpy` package — `nfl_data_py` is deprecated and unmaintained; do not
use it or any tutorial based on it. `nflreadpy` returns Polars DataFrames.

Load: `load_player_stats`, `load_rosters`, `load_depth_charts`,
`load_snap_counts`, `load_injuries`, `load_schedules`, `load_nextgen_stats`
(receiving + rushing), `load_ff_playerids`, `load_ff_opportunity`.

`load_ff_opportunity` (expected fantasy points) is the highest-value table
here — actual-minus-expected is the cleanest free regression signal and is
what separates "scored a lot" from "will score a lot again."

**Sleeper public API** (`api.sleeper.app/v1`) — no auth. Player master list
with cross-platform IDs, trending adds/drops, and public draft picks that
aggregate into a free ADP proxy. The player dump is ~5MB; fetch once daily
at most and cache to `data/raw/`.

**DynastyProcess `ff_playerids`** — the crosswalk. Join on `gsis_id`, falling
back to this table. **Never join on player name** — suffix and collision
mismatches will silently corrupt the board.

**FantasyPros** — best consensus ECR/ADP but requires a key and the free tier
is prototype-only. Read `FANTASYPROS_API_KEY` from env; skip cleanly and fall
back to the Sleeper ADP proxy when it's unset. Never block ingest on it.

Ingest must be idempotent — `CREATE OR REPLACE TABLE` for full reloads,
upsert on `(player_id, season, week)` for incrementals. Write one row per
table to `meta_ingest (table_name, row_count, source, loaded_at)` so agents
can check freshness.

## My league — write this to `config/leagues/main.yml`

10-team redraft, full PPR, FAAB waivers, 6-team playoffs starting week 15,
trade deadline week 12. Starting lineup: QB, RB, RB, WR, WR, TE, FLEX,
FLEX, DEF — 9 starters, 5 bench, 2 IR, **no kicker**.

```yaml
name: main
teams: 10
format: redraft
playoff_teams: 6
playoff_start_week: 15
trade_deadline_week: 12
ir_slots: 2
waivers: faab

roster:
  QB: 1
  RB: 2
  WR: 2
  TE: 1
  FLEX: 2         # W/R/T
  SUPERFLEX: 0
  K: 0            # no kicker slot in this league
  DST: 1
  BENCH: 5
  # 9 starters, 14 roster spots, +2 IR (IR does not count against the 14)
  # validate_league.py must FAIL on a null slot count rather than defaulting.

scoring:
  passing:
    yards_per_point: 25        # +0.04/yd
    td: 4
    two_pt: 2
    interception: -2           # non-standard, harsher than typical
  rushing:
    yards_per_point: 10        # +0.1/yd
    td: 6
    two_pt: 2
  receiving:
    reception: 1               # full PPR
    yards_per_point: 10        # +0.1/yd
    td: 6
    two_pt: 2
  misc:
    fumble_lost: -2
    fumble_recovery_td: 6
  # No kicking block: this league has no K roster slot, so kicker scoring
  # is inert. Kickers must be excluded from the player universe entirely —
  # never rank, project, or tier them.
  dst:
    td: 6
    sack: 1
    interception: 2
    fumble_recovery: 2
    forced_fumble: 1
    safety: 2
    blocked_kick: 2
    points_allowed:
      "0": 5
      "1-6": 4
      "7-13": 3
      "14-20": 1
      "21-27": 0
      "28-34": -1
      "35+": -4
    yards_allowed:             # non-standard: stacks with points_allowed
      "0-99": 5
      "100-199": 3
      "200-299": 2
      "300-349": 0
      "350-399": -1
      "400-449": -3
      "450-499": -5
      "500-549": -6
      "550+": -7
  special_teams:
    td: 6
    forced_fumble: 1
    fumble_recovery: 1
  special_teams_player:
    td: 6
    forced_fumble: 1
    fumble_recovery: 1
```

`src/ffdraft/scoring.py` must compute projected points from this config
rather than from any hardcoded default. Write unit tests that score a few
known 2024 stat lines by hand and assert the function matches.

Six things about this league the metrics code must reflect. These are the
places where a generic board would be actively wrong for me:

- **The league is extremely shallow.** 10 teams × 14 spots = 140 rostered
  players, ~10 of which are DST. Baselines sit far higher than the 12-team
  defaults every public ranking assumes. Compute replacement level from
  `teams × slots` for this config; never hardcode.
- **Only 5 bench spots.** This is the dominant roster-construction
  constraint. Each bench slot is 20% of my flexibility, so lottery tickets,
  handcuffs, and upside stashes cost far more than usual — and because the
  waiver pool is so rich, insurance picks are close to worthless. Weight the
  board toward immediate contributors and say so explicitly for any player
  whose case rests on a contingent role.
- **2 IR slots partially offset that.** A player with a known early-season
  absence (suspension, PUP, rehab) can be stashed without consuming bench,
  so those players are *more* draftable here than the 5-man bench implies.
  Flag IR-eligibility as its own column where relevant.
- **2 FLEX on a 2RB/2WR base.** 20 flex-eligible starting slots across the
  league on top of the base requirements. In full PPR these skew to WR and
  pass-catching RB, so the RB and WR baselines must be derived from actual
  flex allocation, not assumed. Model the allocation and state the
  assumption.
- **1 QB, 1 TE, no K, 10 teams.** QB and TE replacement levels are very
  high — roughly QB10-14 and TE10-12 are freely available. Both positions
  should be waited on absent a clear tier break, and the board should show
  the drop-off curve rather than just asserting it. Kickers are excluded
  entirely.
- **INT at -2 and DST scoring is unusually rich.** Turnover-prone volume
  passers lose real value, so factor interception rate into QB projections.
  On DST, yards-allowed bonuses stack on points-allowed with a +5 to -7
  spread, which makes weekly matchup streaming genuinely valuable — but with
  only 5 bench spots I can't carry two, so streaming runs through FAAB and
  the Wednesday 3 AM ET waiver clear. Don't let DST ceiling inflate its
  draft-day rank.

## The five sub-agents

Each is a Markdown file in `.claude/agents/` with YAML frontmatter (`name`,
`description`, `tools`, `model`) and the system prompt as the body.

| Agent | Model | Tools | Owns |
|---|---|---|---|
| `data-ingest` | sonnet | Bash, Read, Write, Edit, Glob, Grep | Refreshing the warehouse |
| `usage-analyst` | sonnet | Bash, Read, Glob, Grep | Opportunity + efficiency |
| `market-analyst` | sonnet | Bash, Read, Glob, Grep | ADP, ECR, VOR, tiers |
| `news-scout` | sonnet | WebSearch, WebFetch, Read | Injuries, depth charts |
| `ranking-synthesizer` | opus | Bash, Read, Write, Glob, Grep | Merging into the board |

Tool grants are deliberately narrow. The analysts have no write access and no
web access; `news-scout` has web but no database. This keeps an agent from
resolving a disagreement by mutating the warehouse, and keeps scraped web
text out of the tables.

**`usage-analyst`** measures opportunity — snap %, route participation,
target share, air-yards share, WOPR, red-zone and inside-the-10 touches,
actual-minus-expected points, and last-6-game trend versus full season. It
must always state sample size, separate "he is good" from "his role is good,"
and return `insufficient_data` for rookies rather than substituting college
production as equivalent.

**`market-analyst`** computes VOR against the position baseline for *this*
league's settings, ADP deltas, gap-based tiers, positional scarcity, and
which tiers survive to each of my picks given my draft slot. It treats ADP as
*price*, never as *value*, and labels the ADP source and date.

**`news-scout`** covers what historical data can't: injuries, depth chart
moves, coordinator and scheme changes, holdouts, camp reports. Every claim
needs a date and a source. Prefer beat reporters and official injury reports;
report quotes, not takes. Surface contradictions rather than resolving them —
that uncertainty should widen confidence downstream. Paraphrase; don't
reproduce article text.

**`ranking-synthesizer`** is the only agent that assigns ranks. Analysts
return measurements. It baselines from usage, overrides with news when the
situation itself changed, computes VOR for my settings, tiers by clustering,
and flags every case where its rank and ADP differ by more than a round. It
also owns roster-construction fit: with 5 bench spots, it must mark any
player whose value depends on a contingent role, and mark IR-eligible
stashes separately since those don't consume bench.

## Orchestration (`CLAUDE.md`)

The orchestrator does no analysis. It decomposes the ask, checks warehouse
freshness via `meta_ingest`, dispatches `usage-analyst`, `market-analyst`,
and `news-scout` **concurrently in one turn**, then hands their findings to
`ranking-synthesizer`. Only the synthesizer waits.

Every sub-agent prompt must carry the full league config, not just that
agent's slice — scoring format and roster slots change every agent's answer.

Board output schema: `rank, player, pos, team, tier, proj_pts, vor, adp,
adp_delta, confidence, why`, preceded by an assumptions block (scoring
format, league size, roster slots, ADP source and date) and followed by a
"biggest divergences from market" section.

Honesty rules to encode in `CLAUDE.md`:

- Tiers over ordinals. Players within a tier are functionally
  interchangeable; presenting #14 as better than #16 when the gap is a third
  of a point is false precision.
- Confidence is a required column, not a footnote.
- Where usage and market disagree sharply, surface the gap as its own
  section rather than averaging it into a bland consensus.
- Never fabricate a stat. Missing input means lower confidence and a stated
  gap, not a guessed number.
- ADP is a large crowd pricing public information. The edge here is a board
  tuned to my exact settings and transparent about *why* it disagrees — not
  a claim of beating consensus outright.

## Slash commands (`.claude/commands/`)

- `/board [league] [--slot N] [--top N]` — full fan-out, synthesis, write a
  timestamped export to `data/exports/`
- `/refresh [--seasons ...]` — `data-ingest` only, print the freshness table
- `/compare <player-a> <player-b>` — head-to-head on usage, market, and news

## Constraints and known gotchas

- **DuckDB is single-writer.** If ingest runs as a scheduled job while an
  interactive session is open, you'll hit lock contention. Have the job write
  to a temp file and atomically swap.
- **Sub-agents load at session start.** Editing a file in `.claude/agents/`
  requires a Claude Code restart; edits via `/agents` apply immediately. Note
  this in the README.
- **`sql/` holds named queries, not inline SQL.** If agents compose SQL fresh
  each run, two invocations will compute target share slightly differently and
  I'll never notice. Metric definitions must be diffable.
- **Pin seasons in `config/sources.yml`** rather than defaulting to "last 4,"
  so a board is reproducible.
- **Timestamp everything in `data/exports/`** so I can diff this week's board
  against last week's and see what moved.
- **`data/` is fully gitignored.** The warehouse rebuilds from ingest, and
  nflverse redistribution carries attribution obligations. Credit nflverse
  (CC-BY 4.0) in the README, and FTN Data via nflverse (CC-BY-SA 4.0) if any
  FTN charting data is used.

Start with phase 1.
