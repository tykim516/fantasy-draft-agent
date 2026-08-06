---
description: Build a full tiered draft board — fan out to the analysts, synthesize, and write a timestamped export
argument-hint: "[league] [--slot N] [--top N]"
allowed-tools: Bash, Read, Glob, Grep, Task
---

Build a draft board. Arguments: `$ARGUMENTS`

Parse them as: an optional league name (default `main`), `--slot N` for my draft
position, and `--top N` for how many players to rank (default 150).

Run this sequence. Do no analysis yourself.

## 1. Preflight

```bash
uv run python scripts/validate_league.py --summary
uv run python scripts/ingest.py --freshness
```

If validation fails, stop and report the errors — do not build a board on an
invalid config. If tables are missing or stale beyond their TTL, dispatch
`data-ingest` and wait for it before continuing.

## 2. Fan out — all three in ONE turn

Dispatch `usage-analyst`, `market-analyst`, and `news-scout` **concurrently in a
single turn**. They have no dependency on each other; running them sequentially
wastes two round trips.

Give each one the full league config from the `--summary` output above, not just
its own slice. Scoring format and roster slots change every agent's answer.

- `usage-analyst` — opportunity and efficiency for the player pool, with sample
  sizes and `insufficient_data` where it applies
- `market-analyst` — VOR against this league's baselines, tiers, positional
  drop-off curves, market ranks with source and date, and (if `--slot` was given)
  which tiers survive to each of my picks
- `news-scout` — injuries, depth chart moves, scheme and coordinator changes,
  holdouts, camp reports, with dates and sources

## 3. Synthesize

Dispatch `ranking-synthesizer` with everything the three returned plus the league
config. It is the only agent that assigns ranks.

Require from it:

- An assumptions block: scoring format, league size, roster slots, market source
  and date, seasons covered
- The board: `rank, player, pos, team, tier, proj_pts, vor, adp, adp_delta,
  confidence, why`
- Columns marking contingent-role players and IR-eligible stashes
- A "biggest divergences from market" section
- A stated-gaps section for anything missing that lowered confidence

## 4. Export

Have the synthesizer write the full board to
`data/exports/board_<YYYYMMDD>T<HHMMSS>.md` so it can be diffed against last
week's. Never overwrite an existing export. Report the path.

## 5. Report

Summarise for me in the chat: the top tiers, the sharpest divergences from
market, anything flagged `insufficient_data`, and — if `--slot` was given — what
is likely to be available at each of my picks. Lead with tiers, not ordinals.
