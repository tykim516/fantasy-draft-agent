# fantasy-draft-agent — orchestrator instructions

A league-aware fantasy football draft board. An orchestrator plus five
sub-agents turn open NFL data into a tiered board for one specific league's
settings.

## Your job as orchestrator

**You do no analysis.** You decompose the ask, check the data is fit to use,
dispatch sub-agents, and assemble what they return. You do not compute a metric,
form an opinion about a player, or assign a rank yourself. If you find yourself
reasoning about whether a running back is undervalued, stop — that belongs to a
sub-agent.

The sequence for any board-shaped request:

1. **Validate the league config.** `uv run python scripts/validate_league.py`.
   It fails loudly on a missing or null setting. Do not proceed past a failure.
2. **Check warehouse freshness.** `uv run python scripts/ingest.py --freshness`
   reads `meta_ingest`. If tables are stale or missing, dispatch `data-ingest`
   first and wait for it.
3. **Dispatch `usage-analyst`, `market-analyst`, and `news-scout` concurrently
   in a single turn.** They have no dependency on each other. Three sequential
   round trips where one would do is the most common way to make this slow.
4. **Then dispatch `ranking-synthesizer`** with everything the three returned.
   Only the synthesizer waits.

**Every sub-agent prompt must carry the full league config**, not just the slice
that agent seems to need. Scoring format and roster slots change every agent's
answer — a usage question is different in full PPR than in standard, and a news
question is different when there are five bench spots. Include the output of
`uv run python scripts/validate_league.py --summary` in each dispatch.

## The league

10-team redraft, **full PPR**, FAAB waivers, 6-team playoffs from week 15, trade
deadline week 12.

Starting lineup: QB, RB, RB, WR, WR, TE, FLEX, FLEX, DEF — **9 starters, 5
bench, 2 IR, no kicker**. 14 roster spots; IR does not count against them.

The canonical config is `config/leagues/main.yml`, validated against
`config/leagues/_schema.yml`. Nothing in this project may hardcode a league
setting — read it from the config.

Six consequences that make a generic board actively wrong here:

- **10 teams x 14 spots = 140 players rostered leaguewide.** Every public
  ranking assumes 12 teams, so replacement level here sits far higher. Compute
  baselines from `teams x slots`.
- **Only 5 bench spots.** The dominant roster-construction constraint. Each is
  20% of your flexibility, so handcuffs and upside stashes cost far more than
  usual — and because the waiver pool is rich, insurance picks are close to
  worthless. Weight toward immediate contributors and say so for any player
  whose case rests on a contingent role.
- **2 IR slots partially offset that.** A known early-season absence can be
  stashed without consuming bench, making those players *more* draftable here.
  IR-eligibility gets its own column.
- **2 FLEX on a 2RB/2WR base** = 20 leaguewide flex slots beyond the base
  requirements. In full PPR these skew to WR and pass-catching RB. Model the
  allocation; state the assumption rather than assuming a split.
- **1 QB, 1 TE, no K, 10 teams** puts QB and TE replacement near QB11 and TE12 —
  freely available. Wait on both absent a clear tier break, and show the drop-off
  curve rather than asserting it. Kickers are excluded from the player universe
  entirely: never rank, project, or tier them.
- **INT at -2, and rich DST scoring** where yards-allowed stacks on
  points-allowed across a +5 to -7 spread. Turnover-prone volume passers lose
  real value. Streaming DST is genuinely valuable, but five bench spots mean you
  cannot carry two, so it runs through FAAB and the Wednesday 3 AM ET waiver
  clear — do not let DST ceiling inflate its draft-day rank.

## The sub-agents

| Agent | Model | Owns |
|---|---|---|
| `data-ingest` | sonnet | Refreshing the warehouse |
| `usage-analyst` | sonnet | Opportunity + efficiency |
| `market-analyst` | sonnet | ADP, ECR, VOR, tiers |
| `news-scout` | sonnet | Injuries, depth charts, situation changes |
| `ranking-synthesizer` | opus | Merging into the board |

Tool grants are deliberately narrow. The analysts have no write access and no
web access; `news-scout` has web but no database. This stops an agent from
resolving a disagreement by mutating the warehouse, and keeps scraped web text
out of the tables. **Do not widen these grants.**

`ranking-synthesizer` is the only agent that assigns ranks. The others return
measurements.

## Data and commands

```bash
uv run python scripts/validate_league.py --summary        # league shape
uv run python scripts/ingest.py --freshness               # meta_ingest
uv run python scripts/ingest.py --staged                  # refresh, atomic swap
uv run python scripts/query.py --list                     # named queries
uv run python scripts/query.py usage_profile --season 2025 --limit 40
```

Named queries live in `sql/` and are called by filename:
`usage_profile`, `points_over_expected`, `adp_deltas`, `roster_context`.

**`sql/` holds named queries, not inline SQL.** If agents compose SQL fresh each
run, two invocations will compute target share slightly differently and nobody
will notice. Metric definitions must be diffable. If a named query does not cover
something, the ad-hoc SQL must be shown and then promoted into a file.

**Never join on player name.** Join on `gsis_id`, falling back to
`ff_playerids`. Suffix and collision mismatches corrupt the board silently.

**Seasons are pinned** in `config/sources.yml` so a board is reproducible.
Overriding them makes the result non-reproducible and must be said out loud.

**`data/` is gitignored** and rebuilds from ingest.

### Market data, stated honestly

The free market anchor is `ff_rankings` — FantasyPros consensus redistributed by
ffverse. That is **ECR** (where experts say a player should go), not **ADP**
(where he actually goes). Label which one the board used. True ADP needs
`FANTASYPROS_API_KEY`, or seeded Sleeper draft ids in `config/sources.yml` —
Sleeper exposes no endpoint that enumerates public drafts, so the proxy cannot
discover them on its own. When neither is available, say the board is priced
against ECR.

## Board output

An assumptions block first: scoring format, league size, roster slots, market
source and date, and which seasons the data covers. Then:

| rank | player | pos | team | tier | proj_pts | vor | adp | adp_delta | confidence | why |

Followed by a **"biggest divergences from market"** section and a stated-gaps
section. Exports go to `data/exports/` with a timestamp so boards are diffable
week to week.

## Honesty rules

- **Tiers over ordinals.** Players within a tier are functionally
  interchangeable. Presenting #14 as better than #16 when the gap is a third of a
  point is false precision.
- **Confidence is a required column, not a footnote.**
- **Where usage and market disagree sharply, surface the gap as its own
  section** rather than averaging it into a bland consensus. The disagreement is
  the most useful thing on the page.
- **Never fabricate a stat.** Missing input means lower confidence and a stated
  gap, not a guessed number. `insufficient_data` is a valid and often correct
  answer, especially for rookies — college production is not a substitute for
  NFL usage.
- **Be honest about the edge.** ADP is a large crowd pricing public information.
  The edge here is a board tuned to these exact settings and transparent about
  *why* it disagrees — not a claim of beating consensus outright.

## Gotchas

- **DuckDB is single-writer.** If ingest runs while a session holds the
  warehouse open, you hit lock contention. Use `--staged`, which builds a temp
  file and atomically swaps it in.
- **Sub-agents load at session start.** Editing a file in `.claude/agents/`
  requires restarting Claude Code; edits made through `/agents` apply
  immediately.
- **`depth_charts` is a snapshot table** carrying every scrape since March. Use
  the derived `depth_chart_current`, and report its `as of` timestamp.
- **Preseason gaps are normal.** The draft season has no stats, snap counts, or
  injury report yet. Ingest keeps the completed seasons and reports what it
  dropped.
