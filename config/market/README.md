# ADP — a hand-maintained input

`adp.csv` is the only file in this project a human is expected to edit by hand.
Everything else rebuilds from ingest.

**Current file: CBS Sports, pulled 2026-08-12. League size unknown.**

## Why it is here and not in `data/`

`data/` is fully gitignored and `data/raw/` is the ingest cache, cleared without
warning. A file you maintain by hand must be version-controlled, or a cache clear
silently reverts the board to ECR-only pricing and nothing says so.

## Why it exists at all

No free API publishes ADP. Sleeper has no ADP endpoint — `/players/nfl/adp` and
`/adp/nfl/{season}` both 404 — `nflreadpy` has no ADP loader, and `ff_rankings`
carries only ECR. So the number is copied in by hand.

ADP and ECR are not interchangeable and the board keeps both:

- **ECR** (`ff_rankings`) — where experts say a player *should* go. A value
  anchor. This is what the board prices against.
- **ADP** (this file) — where he *actually* goes. An availability anchor, and
  what slot-survival math runs on.

The gap between them is the point. See `ecr_vs_adp` in `sql/adp_deltas.sql`.

## Provenance matters — set `provider` honestly

`sources.adp_file.provider` is not a label, it changes what the number means:

- **ADP from the platform the league drafts on** is close to a model of what the
  other managers see on their screen while picking. Availability claims are
  strong: "he will likely be there at 25."
- **ADP from anywhere else** describes a different crowd, possibly at a different
  league size and scoring format. Still a real price, and the ECR-vs-ADP gap is
  still informative — but it is not evidence about your specific opponents.
  Claims must hedge to "the market takes him around here."

This league drafts on **Sleeper** and the current file is **CBS**, so the second
case applies. `provider` and `format_note` are carried onto every row as
`adp_source` and `adp_format_note` so the caveat travels with the data instead of
living one file away from everything that consumes it.

If you ever swap in a file from a different source, **update `provider` in the
same commit.** A stale provider is worse than none: it makes the board state a
confident availability claim on the strength of the wrong crowd.

## Refreshing

1. Copy the current ADP into `adp.csv`.
2. Drop a dated copy in `history/`, named `adp-<provider>-<date>.csv`, so
   week-over-week drift stays diffable and the source stays visible.
3. Update `sources.adp_file.as_of` — and `provider` if the source changed — in
   `config/sources.yml`. This is not optional: `as_of` is the date the board
   reports, and file mtime is wrong after any `git clone`.
4. `uv run python scripts/ingest.py --sources sleeper`
5. Confirm the printed link summary lists no new `pending` players. If it does,
   resolve them in `adp_aliases.yml` (below).

Ingest warns when `as_of` is older than `max_age_days`. In August ADP moves fast
enough that a two-week-old file will misprice rookies badly.

## Accepted columns

Header matching is case- and whitespace-insensitive. **Two layouts parse**, and
both keep working, because `history/` holds files in each and a board rebuilt
from an older export should still build.

### Full layout (current — preferred)

```
Rank,Player,Trend,Avg Pos,Hi/Lo,Pct
1,Jahmyr Gibbs RB  DET,—,1.45,1/3,100
216,Bills DST  BUF,—,176.71,128/248,23
```

| Column | Required | Notes |
|---|---|---|
| `Player` | yes | Name, position and team packed into one cell. Split on the trailing `POS TEAM` pair. |
| `Avg Pos` | yes | A **real average pick**, not a rank. Ties are expected and fine. |
| `Rank` | no | The export's own dense ordering. Its presence is what flags the file as carrying average picks. |
| `Hi/Lo` | no | Earliest and latest pick observed. Drives the survival read. |
| `Pct` | no | Percent of drafts the player was taken in. |
| `Trend` | no | Movement since the last publish; stored, not yet used. |

This layout is better on three counts: an average pick can be compared directly
against a pick number (a rank cannot), `Hi/Lo` gives the real spread instead of a
guessed cushion, and the packed team abbreviation both disambiguates namesakes
and keys defenses without a name lookup.

Trailing non-player rows — `Legend`, `Injury`, `News` — are dropped: a row with
no usable ADP is not a player.

### Rank-only layout (older)

```
Name,ADP,Position ADP
Jahmyr Gibbs,1,RB1
```

| Column | Required | Notes |
|---|---|---|
| `Name` / `Player` | yes | Full name. Suffixes and punctuation are normalized away. |
| `ADP` | yes | A dense rank (1, 2, 3 …). |
| `Position ADP` / `Position` / `Pos` | no | `RB1`, `DEF17`; the trailing digits are read but not trusted. |
| `Team` / `Tm` | no | Improves disambiguation when present. |
| `sleeper_id` / `gsis_id` | no | If present, used directly and no name matching happens. |

In this layout `Position ADP` does **not** agree with the overall ordering —
Ja'Marr Chase was ADP 3 / WR2 while Puka Nacua was ADP 4 / WR1, because the two
columns come from different computations upstream. Only the position *label* is
used. Do not try to reconcile them.

`adp_is_average_pick` on the loaded table records which layout a given load came
from, because pick math is only valid against a real pick number.

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
