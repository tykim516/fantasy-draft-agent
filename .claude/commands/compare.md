---
description: Head-to-head comparison of two players on usage, market, and news
argument-hint: "<player-a> <player-b>"
allowed-tools: Bash, Read, Glob, Grep, Task
---

Compare two players head to head. Arguments: `$ARGUMENTS`

Parse two player names. If only one is given, or the names are ambiguous, ask
before proceeding — comparing the wrong Josh Allen wastes the whole exercise.

## Sequence

Start with `uv run python scripts/validate_league.py --summary` and pass that
league config to every agent you dispatch.

Dispatch `usage-analyst`, `market-analyst`, and `news-scout` **concurrently in
one turn**, each scoped to just these two players:

- `usage-analyst` — snap share, target share, air-yards share, WOPR, red-zone
  and inside-10 touches, points over expected, and last-six-games trend for both.
  Sample sizes on every number. `insufficient_data` where it applies.
- `market-analyst` — projected points, VOR against this league's baselines, tier,
  market rank with source and date, and the positional context: does taking one
  over the other actually cost anything given where their positions' replacement
  levels sit?
- `news-scout` — current situation for both: injuries, depth chart, scheme or
  coordinator changes, camp reports. Dated and sourced.

Then dispatch `ranking-synthesizer` to produce the verdict.

## Output

- A side-by-side table of the key usage and market numbers
- **Are they in the same tier?** If so, say plainly that the choice is close to
  arbitrary and the tie should break on roster fit, bye week, or preference —
  false precision helps nobody
- Where usage and market disagree about either player, surfaced explicitly
- Roster-fit read for this league: with five bench spots, does either case rest
  on a contingent role? Is either IR-eligible and therefore stashable without
  spending a bench spot?
- A confidence statement, and any gap that lowered it

Do not manufacture a winner. "These two are interchangeable and here is what
would break the tie" is a legitimate and often correct answer.
