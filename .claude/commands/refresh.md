---
description: Refresh the warehouse from all sources and print the freshness table
argument-hint: "[--seasons 2023 2024 2025] [--staged] [--force]"
allowed-tools: Bash, Read, Glob, Grep, Task
---

Refresh the data warehouse. Arguments: `$ARGUMENTS`

Dispatch `data-ingest` only. Do not run any analysis and do not build a board.

Tell it to:

1. Print current freshness first (`uv run python scripts/ingest.py --freshness`)
   and say whether a refresh is actually warranted. If everything is within TTL
   and nothing is broken, stop there and say so.
2. Otherwise run the ingest, passing through any arguments given above.
   Prefer `--staged` when this session has the warehouse open — DuckDB is
   single-writer, and `--staged` builds a temp file and atomically swaps it in.
3. Print the freshness table afterwards: table, rows, source, loaded_at,
   age_hours.

If `--seasons` was passed, warn clearly that overriding the pinned seasons in
`config/sources.yml` makes the resulting board non-reproducible.

Report back: the freshness table, anything skipped or failed with its reason,
and one line on whether the warehouse is fit to build a board on.

Expected skips, which are **not** failures:

- `fantasypros_adp` when `FANTASYPROS_API_KEY` is unset
- the Sleeper draft proxy, which is skipped whenever the hand-maintained ADP file
  loaded — the file is preferred, so this skip means things are working

`sleeper_adp` **loading** is the normal case. Two things in its detail line need
reporting rather than passing over:

- `unlinked N` with named players — they need an entry in
  `config/market/adp_aliases.yml` before they carry ADP on the board
- a `STALE:` warning — the ADP file is maintained by hand and has aged past
  `max_age_days`. Say so plainly; ADP moves fast in August.
- Draft-season injuries, snap counts, and stats before the season starts
