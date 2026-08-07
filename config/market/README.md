# Sleeper ADP — a hand-maintained input

`sleeper_adp.csv` is the only file in this project a human is expected to edit by
hand. Everything else rebuilds from ingest.

## Why it is here and not in `data/`

`data/` is fully gitignored and `data/raw/` is the ingest cache, cleared without
warning. A file you maintain by hand must be version-controlled, or a cache clear
silently reverts the board to ECR-only pricing and nothing says so.

## Why it exists at all

Sleeper's draft board sorts by Sleeper's own ADP, so that number is what the
other managers in the league are actually looking at while they pick. It is the
best available model of who will be gone by a given pick.

Sleeper publishes no ADP endpoint — `/players/nfl/adp` and `/adp/nfl/{season}`
both 404, `nflreadpy` has no ADP loader, and `ff_rankings` carries only ECR. So
the number is copied in by hand.

ADP and ECR are not interchangeable and the board keeps both:

- **ECR** (`ff_rankings`) — where experts say a player *should* go. A value
  anchor. This is what the board prices against.
- **ADP** (this file) — where he *actually* goes on Sleeper. An availability
  anchor. This is what slot-survival math runs on.

The gap between them is the point. See `ecr_vs_adp` in `sql/adp_deltas.sql`.

## Refreshing

1. Copy the current ADP out of Sleeper into `sleeper_adp.csv`.
2. Drop a dated copy in `history/` so week-over-week drift stays diffable.
3. Update `sources.sleeper_adp_file.as_of` in `config/sources.yml` to the date
   the ADP was pulled. This is not optional — it is the date the board reports,
   and file mtime is wrong after any `git clone`.
4. `uv run python scripts/ingest.py --sources sleeper`
5. Confirm the printed link summary lists no new `pending` players. If it does,
   resolve them in `adp_aliases.yml` (below).

Ingest warns when `as_of` is older than `max_age_days`. In August ADP moves fast
enough that a two-week-old file will misprice rookies badly.

## Accepted columns

Header matching is case- and whitespace-insensitive.

| Column | Required | Notes |
|---|---|---|
| `Name` / `Player` | yes | Full name. Suffixes and punctuation are normalized away. |
| `ADP` | yes | A dense rank (1, 2, 3 …), not an average pick number. |
| `Position ADP` / `Position` / `Pos` | no | `RB1`, `DEF17`; the trailing digits are read but not trusted (see below). |
| `Team` / `Tm` | no | Improves disambiguation when present. |
| `sleeper_id` / `gsis_id` | no | If present, used directly and no name matching happens. |

### Known quirk in the current export

`Position ADP` does **not** agree with the overall ordering — Ja'Marr Chase is
ADP 3 / WR2 while Puka Nacua is ADP 4 / WR1. The two columns come from different
computations upstream. Only the position *label* is used; positional rank is
re-derived from the overall column. Do not try to reconcile them.

## `adp_aliases.yml`

Names that cannot be linked to a `gsis_id` automatically land here as `pending`,
with candidate ids listed. Confirm each once and it is remembered forever.

This project forbids joining on player name (`config/sources.yml`, `CLAUDE.md`),
because suffix and collision mismatches corrupt a board silently. The alias map
is how a name-keyed file is admitted without breaking that rule: every link is
either unambiguous or a recorded, reviewed, diffable human decision.

Most rows need no attention — the resolver links ~99% on its own. The residue is
usually rookies the crosswalk has not absorbed yet, or a formal name where the
export disagrees with nflverse ("Nathaniel Dell" vs "Tank Dell").
