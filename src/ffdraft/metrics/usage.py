"""Opportunity and efficiency metrics.

The distinction this module exists to preserve: *role* and *quality* are
different things. Target share and red-zone touches describe a role, which is
stable and largely a coaching decision. Yards per target and touchdown rate
describe conversion, which is noisy and regresses. A player can be good with a
bad role or bad with a good role, and a board that averages the two into one
number cannot tell you which.

Inputs are per player-week frames produced by the named queries in `sql/`.
Nothing here queries the warehouse itself, so every metric has one definition
and one diff.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from . import INSUFFICIENT_DATA

# Below this many games a rate stat is describing noise, not a role.
MIN_GAMES_FOR_RATES = 4
# A "recent form" window. Six games is long enough to survive one bad script and
# short enough to catch a role change.
RECENT_WINDOW = 6

# Weekly columns a usage profile can average, when present.
RATE_COLUMNS = (
    "snap_pct",
    "target_share",
    "air_yards_share",
    "wopr",
    "racr",
    "pacr",
)
# Weekly columns a usage profile sums.
VOLUME_COLUMNS = (
    "targets",
    "carries",
    "receptions",
    "rz20_touches",
    "rz10_touches",
    "rz20_touchdowns",
    "points_over_expected",
)


@dataclass(frozen=True)
class UsageWindow:
    """What a usage number was computed over. Every metric must carry this."""

    season: int
    games: int
    window: str  # "season" or "last6"

    def label(self) -> str:
        return f"{self.window} {self.season} (n={self.games})"


def _present(df: pl.DataFrame, columns: tuple[str, ...]) -> list[str]:
    return [c for c in columns if c in df.columns]


def aggregate_window(
    weekly: pl.DataFrame,
    group_keys: tuple[str, ...] = ("player_id",),
    suffix: str = "",
) -> pl.DataFrame:
    """Collapse per player-week rows into per-player rates, volumes, and a game count.

    Rates are averaged over games played, not over the whole season — a player
    who missed six weeks should not have his target share diluted by weeks he
    was not on the field.
    """
    rates = _present(weekly, RATE_COLUMNS)
    volumes = _present(weekly, VOLUME_COLUMNS)

    aggs = [pl.len().alias(f"games{suffix}")]
    aggs += [pl.col(c).drop_nulls().mean().alias(f"{c}{suffix}") for c in rates]
    aggs += [pl.col(c).sum().alias(f"{c}{suffix}") for c in volumes]
    aggs += [
        (pl.col(c).sum() / pl.len()).alias(f"{c}_per_game{suffix}")
        for c in volumes
        if c != "points_over_expected"
    ]

    return weekly.group_by(list(group_keys)).agg(aggs)


def usage_profile(
    weekly: pl.DataFrame,
    season: int,
    group_keys: tuple[str, ...] = ("player_id",),
    recent_window: int = RECENT_WINDOW,
    min_games: int = MIN_GAMES_FOR_RATES,
) -> pl.DataFrame:
    """Full-season usage, the last-N-game window, and the delta between them.

    The delta is the point of the exercise. A player whose season target share
    is 18% but whose last six weeks are 25% is a different investment than one
    whose numbers run the other way, and a season average hides both.
    """
    if weekly.is_empty():
        return weekly

    if "week" not in weekly.columns:
        raise ValueError("usage_profile needs a 'week' column to build a recent-form window")

    has_season = "season" in weekly.columns
    season_rows = weekly.filter(pl.col("season") == season) if has_season else weekly
    full = aggregate_window(season_rows, group_keys)

    recent_rows = season_rows.filter(
        pl.col("week") > (pl.col("week").max().over(list(group_keys)) - recent_window)
    )
    recent = aggregate_window(recent_rows, group_keys, suffix="_last6")

    profile = full.join(recent, on=list(group_keys), how="left")

    # Trend columns only where both windows produced the metric.
    deltas = [
        (pl.col(f"{c}_last6") - pl.col(c)).alias(f"{c}_trend")
        for c in _present(weekly, RATE_COLUMNS)
        if f"{c}_last6" in profile.columns
    ]
    if deltas:
        profile = profile.with_columns(deltas)

    return profile.with_columns(
        data_status=pl.when(pl.col("games") >= min_games)
        .then(pl.lit("ok"))
        .otherwise(pl.lit(INSUFFICIENT_DATA)),
        sample_note=pl.lit(
            f"season {season}; rates averaged over games played; "
            f"last6 = final {recent_window} weeks the player appeared in"
        ),
    ).sort("games", descending=True)


def flag_rookies(
    profile: pl.DataFrame, rookie_ids: set[str], id_col: str = "player_id"
) -> pl.DataFrame:
    """Mark players with no NFL usage history as `insufficient_data`.

    College production is not a substitute. It is measured against different
    opposition under different usage rules, and treating it as equivalent is how
    a board ends up confidently ranking a rookie it knows nothing about. The
    honest output is a stated gap that widens confidence downstream.
    """
    if profile.is_empty():
        return profile
    return profile.with_columns(
        data_status=pl.when(pl.col(id_col).is_in(list(rookie_ids)))
        .then(pl.lit(INSUFFICIENT_DATA))
        .otherwise(pl.col("data_status")),
        rookie=pl.col(id_col).is_in(list(rookie_ids)),
    )


def points_over_expected(
    weekly: pl.DataFrame,
    actual_col: str = "total_fantasy_points",
    expected_col: str = "total_fantasy_points_exp",
    group_keys: tuple[str, ...] = ("player_id",),
) -> pl.DataFrame:
    """Actual minus expected fantasy points, per player.

    The cleanest free regression signal there is: it separates "scored a lot"
    from "will score a lot again". A large positive residual is a claim about
    touchdown luck, not about talent, and should pull a projection down rather
    than up.
    """
    missing = [c for c in (actual_col, expected_col) if c not in weekly.columns]
    if missing:
        raise ValueError(f"points_over_expected needs column(s) {missing}")

    return (
        weekly.group_by(list(group_keys))
        .agg(
            games=pl.len(),
            actual_points=pl.col(actual_col).sum(),
            expected_points=pl.col(expected_col).sum(),
        )
        .with_columns(
            poe=pl.col("actual_points") - pl.col("expected_points"),
            poe_per_game=(pl.col("actual_points") - pl.col("expected_points")) / pl.col("games"),
        )
        .sort("poe", descending=True)
    )
