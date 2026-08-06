"""Value over replacement, derived from *this* league's settings.

Public rankings assume 12 teams. This league has 10, which moves every
replacement level up and is the single biggest reason a generic board is wrong
here. Nothing in this module is hardcoded: baselines come from `teams × slots`
and from where the flex slots actually get spent.

Flex allocation is modelled rather than assumed. A 2RB/2WR base with two flex
spots does not mean "one flex RB and one flex WR" — it means the best 20
remaining flex-eligible players in the league get started, and in full PPR that
skews toward pass-catching backs and volume receivers. The allocation this
module computes is reported alongside the baselines, because it is an
assumption a reader is entitled to disagree with.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from ..league import League


@dataclass
class Baselines:
    """Replacement level per position, plus the reasoning that produced it."""

    league_name: str
    teams: int
    base_starters: dict[str, int]
    flex_allocation: dict[str, int]
    replacement_rank: dict[str, int]
    replacement_points: dict[str, float]
    rostered_players: int
    notes: list[str] = field(default_factory=list)

    def describe(self) -> dict:
        return {
            "league": self.league_name,
            "teams": self.teams,
            "rostered_players_leaguewide": self.rostered_players,
            "base_starting_slots": self.base_starters,
            "flex_allocation_modelled": self.flex_allocation,
            "replacement_rank": self.replacement_rank,
            "replacement_points": {k: round(v, 2) for k, v in self.replacement_points.items()},
            "notes": self.notes,
        }


def compute_baselines(
    league: League,
    projections: pl.DataFrame,
    points_col: str = "proj_pts",
    position_col: str = "position",
) -> Baselines:
    """Derive replacement level per position from projected points.

    The procedure:

    1. Every position's dedicated starting slots are `teams × slots`.
    2. Those slots are filled by the top projected players at each position.
    3. Every remaining flex-eligible player competes for the leaguewide flex
       slots; the top `teams × flex_slots` of them get started. Counting those
       by position *is* the flex allocation — it is observed, not assumed.
    4. Replacement level at a position is the next player after all of its
       slots, dedicated and flex, are filled.
    """
    for column in (points_col, position_col):
        if column not in projections.columns:
            raise ValueError(f"compute_baselines needs a {column!r} column")

    universe = set(league.player_universe)
    pool = (
        projections.filter(pl.col(position_col).is_in(list(universe)))
        .drop_nulls(points_col)
        .sort(points_col, descending=True)
        .with_columns(
            pos_rank=pl.col(points_col).rank("ordinal", descending=True).over(position_col)
        )
    )

    base_starters = {pos: league.base_starters(pos) for pos in sorted(universe)}
    flex_positions = league.flex_eligible_positions() & universe
    flex_total = league.flex_slots_total()

    # Everyone not already locked into a dedicated starting slot.
    remaining = pool.filter(
        pl.col(position_col).is_in(sorted(flex_positions))
        & (pl.col("pos_rank") > pl.col(position_col).replace_strict(base_starters, default=0))
    )
    flex_winners = remaining.head(flex_total)
    counts = {
        row[position_col]: row["n"]
        for row in flex_winners.group_by(position_col).agg(pl.len().alias("n")).to_dicts()
    }
    flex_allocation = {pos: int(counts.get(pos, 0)) for pos in sorted(universe)}

    replacement_rank: dict[str, int] = {}
    replacement_points: dict[str, float] = {}
    notes: list[str] = []

    for pos in sorted(universe):
        rank = base_starters[pos] + flex_allocation[pos] + 1
        replacement_rank[pos] = rank
        at_pos = pool.filter(pl.col(position_col) == pos)[points_col].to_list()
        if not at_pos:
            replacement_points[pos] = 0.0
            notes.append(f"{pos}: no projected players in the pool; replacement set to 0")
        elif rank <= len(at_pos):
            replacement_points[pos] = float(at_pos[rank - 1])
        else:
            replacement_points[pos] = float(at_pos[-1])
            notes.append(
                f"{pos}: only {len(at_pos)} players projected but replacement rank is {rank}; "
                f"used the last available player, so {pos} VOR is optimistic"
            )

    if flex_total:
        share = ", ".join(f"{p} {flex_allocation[p]}" for p in sorted(flex_positions))
        notes.append(
            f"{flex_total} leaguewide flex slots modelled as: {share}. This is derived from "
            f"projected points, not assumed from position labels."
        )
    notes.append(
        f"{league.teams} teams x {league.roster_spots} roster spots = {league.rostered_players} "
        f"players rostered leaguewide. Baselines sit far higher than the 12-team defaults "
        f"public rankings assume."
    )
    for pos in ("QB", "TE"):
        if pos in replacement_rank:
            notes.append(
                f"{pos} replacement is {pos}{replacement_rank[pos]} — freely available, so "
                f"{pos} should be waited on absent a clear tier break."
            )

    return Baselines(
        league_name=league.name,
        teams=league.teams,
        base_starters=base_starters,
        flex_allocation=flex_allocation,
        replacement_rank=replacement_rank,
        replacement_points=replacement_points,
        rostered_players=league.rostered_players,
        notes=notes,
    )


def add_vor(
    league: League,
    projections: pl.DataFrame,
    points_col: str = "proj_pts",
    position_col: str = "position",
    out: str = "vor",
) -> tuple[pl.DataFrame, Baselines]:
    """Attach value over replacement, plus the baselines used to compute it."""
    baselines = compute_baselines(league, projections, points_col, position_col)
    scored = projections.filter(pl.col(position_col).is_in(list(league.player_universe)))
    return (
        scored.with_columns(
            replacement_points=pl.col(position_col).replace_strict(
                baselines.replacement_points, default=None
            )
        )
        .with_columns((pl.col(points_col) - pl.col("replacement_points")).alias(out))
        .sort(out, descending=True),
        baselines,
    )


def positional_dropoff(
    projections: pl.DataFrame,
    position: str,
    points_col: str = "proj_pts",
    position_col: str = "position",
    top_n: int = 24,
) -> pl.DataFrame:
    """The drop-off curve at a position, so the board can show it rather than assert it.

    "Wait on QB" is a claim. The gap between QB5 and QB12 is the evidence, and a
    reader who disagrees with the conclusion can still read the curve.
    """
    return (
        projections.filter(pl.col(position_col) == position)
        .drop_nulls(points_col)
        .sort(points_col, descending=True)
        .head(top_n)
        .with_columns(pos_rank=pl.int_range(1, pl.len() + 1))
        .with_columns(
            drop_from_prev=(pl.col(points_col).shift(1) - pl.col(points_col)).round(2),
            drop_from_best=(pl.col(points_col).first() - pl.col(points_col)).round(2),
        )
    )
