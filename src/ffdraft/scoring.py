"""Apply a league's scoring rules to raw stat lines.

Points come from the league config and nowhere else. There is no default
scoring table in this module — if a setting is missing the config is invalid and
`league.py` has already raised.

Two shapes are supported:

* `score_offense_row` / `score_dst_row` take a mapping (one stat line) and are
  what the unit tests hand-check against.
* `score_offense` / `score_dst` take a Polars frame and add a points column.

Both run off the same `ScoringPlan`, so the frame path and the row path cannot
drift apart.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import polars as pl

from .league import Band, League, build_bands

Kind = Literal["multiply", "divide", "band"]


@dataclass(frozen=True)
class Term:
    """One scoring rule bound to one warehouse column.

    `divide` is for `yards_per_point`-style settings: 25 means 1 point per 25
    yards, so the term is yards / 25, not yards * 25.
    """

    group: str
    key: str
    column: str
    kind: Kind
    required: bool = True

    @property
    def dotted(self) -> str:
        return f"{self.group}.{self.key}"


# Column names below are nflverse `player_stats` columns, verified against the
# real 2024 table rather than assumed. Note `passing_interceptions` (not
# `interceptions`) and `fumbles_lost_total`, which already aggregates sack,
# rushing, and receiving lost fumbles.
OFFENSE_TERMS: tuple[Term, ...] = (
    Term("passing", "yards_per_point", "passing_yards", "divide"),
    Term("passing", "td", "passing_tds", "multiply"),
    Term("passing", "two_pt", "passing_2pt_conversions", "multiply"),
    Term("passing", "interception", "passing_interceptions", "multiply"),
    Term("rushing", "yards_per_point", "rushing_yards", "divide"),
    Term("rushing", "td", "rushing_tds", "multiply"),
    Term("rushing", "two_pt", "rushing_2pt_conversions", "multiply"),
    Term("receiving", "reception", "receptions", "multiply"),
    Term("receiving", "yards_per_point", "receiving_yards", "divide"),
    Term("receiving", "td", "receiving_tds", "multiply"),
    Term("receiving", "two_pt", "receiving_2pt_conversions", "multiply"),
    Term("misc", "fumble_lost", "fumbles_lost_total", "multiply"),
    Term("misc", "fumble_recovery_td", "fumble_recovery_tds", "multiply", required=False),
    # A skill player's own return/special-teams scoring.
    Term("special_teams_player", "td", "special_teams_tds", "multiply", required=False),
    Term("special_teams_player", "forced_fumble", "def_fumbles_forced", "multiply", required=False),
    Term(
        "special_teams_player", "fumble_recovery", "fumble_recovery_opp", "multiply", required=False
    ),
)

# These run against the derived `dst_stats` table, which ingest builds with
# clean names so this module never has to know nflverse's team-stat quirks.
DST_TERMS: tuple[Term, ...] = (
    Term("dst", "td", "def_tds", "multiply"),
    Term("dst", "sack", "sacks", "multiply"),
    Term("dst", "interception", "interceptions", "multiply"),
    Term("dst", "fumble_recovery", "fumble_recoveries", "multiply"),
    Term("dst", "forced_fumble", "forced_fumbles", "multiply", required=False),
    Term("dst", "safety", "safeties", "multiply"),
    Term("dst", "blocked_kick", "blocked_kicks", "multiply", required=False),
    Term("dst", "points_allowed", "points_allowed", "band"),
    Term("dst", "yards_allowed", "yards_allowed", "band", required=False),
    Term("special_teams", "td", "st_tds", "multiply", required=False),
)

# Settings this league scores that the free data cannot separate. Scored as
# zero, listed here, and reported by the board — a stated gap, not a guess.
KNOWN_GAPS: tuple[str, ...] = (
    "special_teams.forced_fumble — nflverse team stats do not split forced fumbles "
    "by phase, so special-teams forced fumbles are not scored separately from defense.",
    "special_teams.fumble_recovery — same limitation as special_teams.forced_fumble.",
)


@dataclass
class ScoringPlan:
    """The terms that will actually be applied, and what is missing."""

    league_name: str
    terms: list[tuple[Term, Any]] = field(default_factory=list)  # (term, points or bands)
    missing_required: list[str] = field(default_factory=list)
    missing_optional: list[str] = field(default_factory=list)
    inert_settings: list[str] = field(default_factory=list)

    def raise_if_incomplete(self) -> None:
        if self.missing_required:
            raise ValueError(
                f"cannot score {self.league_name}: input is missing required column(s) "
                + ", ".join(sorted(self.missing_required))
            )

    def describe(self) -> dict[str, Any]:
        return {
            "applied_terms": [t.dotted for t, _ in self.terms],
            "missing_required_columns": sorted(self.missing_required),
            "missing_optional_columns": sorted(self.missing_optional),
            "settings_present_but_unscored": sorted(self.inert_settings),
            "known_gaps": list(KNOWN_GAPS),
        }


def build_plan(league: League, terms: Iterable[Term], available: Iterable[str]) -> ScoringPlan:
    available = set(available)
    plan = ScoringPlan(league_name=league.name)

    for term in terms:
        block = league.scoring.get(term.group)
        if not isinstance(block, dict) or term.key not in block:
            # e.g. main.yml has no `kicking` block at all; nothing to score.
            continue
        if term.column not in available:
            (plan.missing_required if term.required else plan.missing_optional).append(term.column)
            continue
        value = block[term.key]
        payload = build_bands(value) if term.kind == "band" else float(value)
        plan.terms.append((term, payload))

    # Settings inside the groups this term set covers that no term implements.
    # Groups belonging to the *other* term set (DST settings during an offense
    # plan, say) are somebody else's job and are not reported here.
    known = {t.dotted for t in terms}
    covered_groups = {t.group for t in terms}
    for group in covered_groups:
        block = league.scoring.get(group)
        if not isinstance(block, dict):
            continue
        for key in block:
            if f"{group}.{key}" not in known:
                plan.inert_settings.append(f"{group}.{key}")

    return plan


def offense_plan(league: League, available: Iterable[str]) -> ScoringPlan:
    return build_plan(league, OFFENSE_TERMS, available)


def dst_plan(league: League, available: Iterable[str]) -> ScoringPlan:
    return build_plan(league, DST_TERMS, available)


# --- row scoring -----------------------------------------------------------


def _apply_row(plan: ScoringPlan, row: Mapping[str, Any]) -> float:
    total = 0.0
    for term, payload in plan.terms:
        if term.column not in row:
            # A banded term must not fire on an absent column: "no points_allowed
            # recorded" is not "allowed zero points", and scoring it as the shutout
            # band would invent 5 points out of nothing.
            continue
        raw = row[term.column]
        value = 0.0 if raw is None else float(raw)
        if term.kind == "divide":
            total += value / payload
        elif term.kind == "multiply":
            total += value * payload
        else:  # band
            total += _band_points(payload, value)
    return total


def _band_points(bands: list[Band], value: float) -> float:
    for band in bands:
        if band.contains(value):
            return band.points
    raise ValueError(f"value {value} matched no band in {[b.key for b in bands]}")


def _row_plan(league: League, terms: Iterable[Term]) -> ScoringPlan:
    """Plan over every column the term set knows about.

    Row scoring treats the stat line as sparse — a stat the player never
    recorded is simply absent — so availability is not checked here. `_apply_row`
    skips terms whose column the row does not carry.
    """
    terms = tuple(terms)
    return build_plan(league, terms, [t.column for t in terms])


def score_offense_row(league: League, row: Mapping[str, Any]) -> float:
    """Score one offensive stat line. Stats the row does not carry contribute zero."""
    return _apply_row(_row_plan(league, OFFENSE_TERMS), row)


def score_dst_row(league: League, row: Mapping[str, Any]) -> float:
    """Score one team-defense stat line.

    Stats the row does not carry contribute zero, including the banded
    `points_allowed` / `yards_allowed` terms — which is why ingest builds both
    columns for every team-week rather than leaving them nullable.
    """
    return _apply_row(_row_plan(league, DST_TERMS), row)


# --- frame scoring ---------------------------------------------------------


def _term_expr(term: Term, payload: Any) -> pl.Expr:
    col = pl.col(term.column).cast(pl.Float64).fill_null(0.0)
    if term.kind == "divide":
        return col / payload
    if term.kind == "multiply":
        return col * payload
    expr = pl.lit(None, dtype=pl.Float64)
    for band in reversed(payload):
        condition = col >= band.lo if band.hi is None else (col >= band.lo) & (col <= band.hi)
        expr = pl.when(condition).then(pl.lit(float(band.points))).otherwise(expr)
    return expr.fill_null(0.0)


def _score_frame(
    league: League,
    df: pl.DataFrame,
    terms: Iterable[Term],
    out: str,
    strict: bool,
) -> tuple[pl.DataFrame, ScoringPlan]:
    plan = build_plan(league, terms, df.columns)
    if strict:
        plan.raise_if_incomplete()
    if not plan.terms:
        return df.with_columns(pl.lit(0.0).alias(out)), plan

    total = _term_expr(*plan.terms[0])
    for term, payload in plan.terms[1:]:
        total = total + _term_expr(term, payload)
    return df.with_columns(total.alias(out)), plan


def score_offense(
    league: League,
    df: pl.DataFrame,
    out: str = "fantasy_points_league",
    strict: bool = True,
) -> tuple[pl.DataFrame, ScoringPlan]:
    """Add league-scored points to an nflverse `player_stats`-shaped frame."""
    return _score_frame(league, df, OFFENSE_TERMS, out, strict)


def score_dst(
    league: League,
    df: pl.DataFrame,
    out: str = "fantasy_points_league",
    strict: bool = True,
) -> tuple[pl.DataFrame, ScoringPlan]:
    """Add league-scored points to a `dst_stats`-shaped frame."""
    return _score_frame(league, df, DST_TERMS, out, strict)
