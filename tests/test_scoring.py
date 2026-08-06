"""Scoring tests against real 2024 stat lines, hand-computed.

Every expected value below is arithmetic done by hand from
`config/leagues/main.yml`, written out in the comment above it. The stat lines
are real 2024 regular-season games pulled from the warehouse, so these assert
that the scorer matches a human reading the league rules — not merely that it
agrees with some other library's idea of PPR.
"""

from __future__ import annotations

import copy

import polars as pl
import pytest

from ffdraft.league import League
from ffdraft.scoring import (
    dst_plan,
    offense_plan,
    score_dst,
    score_dst_row,
    score_offense,
    score_offense_row,
)

# --- real 2024 regular-season stat lines ----------------------------------

BARKLEY_W7 = dict(
    rushing_yards=176, rushing_tds=1, receptions=2, receiving_yards=11, fumbles_lost_total=0
)
# 176/10 = 17.6  +  1 rush TD x6 = 6  +  2 rec x1 = 2  +  11/10 = 1.1
BARKLEY_W7_POINTS = 26.7

CHASE_W8 = dict(receptions=9, receiving_yards=54, receiving_tds=1)
# 9 rec x1 = 9  +  54/10 = 5.4  +  1 rec TD x6 = 6
CHASE_W8_POINTS = 20.4

LAMAR_W12 = dict(
    passing_yards=177,
    passing_tds=2,
    passing_interceptions=0,
    rushing_yards=15,
    rushing_tds=1,
)
# 177/25 = 7.08  +  2 pass TD x4 = 8  +  15/10 = 1.5  +  1 rush TD x6 = 6
LAMAR_W12_POINTS = 22.58

SHAHEED_W6 = dict(receptions=1, receiving_yards=11, rushing_yards=2, special_teams_tds=1)
# 1 rec = 1  +  11/10 = 1.1  +  2/10 = 0.2  +  1 return TD x6 = 6
SHAHEED_W6_POINTS = 8.3

AUSTIN_W8 = dict(receptions=3, receiving_yards=54, receiving_tds=1, special_teams_tds=1)
# 3 rec = 3  +  54/10 = 5.4  +  1 rec TD x6 = 6  +  1 return TD x6 = 6
AUSTIN_W8_POINTS = 20.4

OFFENSE_CASES = [
    ("Saquon Barkley 2024 wk7", BARKLEY_W7, BARKLEY_W7_POINTS),
    ("Ja'Marr Chase 2024 wk8", CHASE_W8, CHASE_W8_POINTS),
    ("Lamar Jackson 2024 wk12", LAMAR_W12, LAMAR_W12_POINTS),
    ("Rashid Shaheed 2024 wk6", SHAHEED_W6, SHAHEED_W6_POINTS),
    ("Calvin Austin III 2024 wk8", AUSTIN_W8, AUSTIN_W8_POINTS),
]


@pytest.mark.parametrize("label,line,expected", OFFENSE_CASES, ids=[c[0] for c in OFFENSE_CASES])
def test_offense_matches_hand_calculation(league, label, line, expected):
    assert score_offense_row(league, line) == pytest.approx(expected)


def test_negative_settings_apply():
    """Interceptions and lost fumbles must subtract, at this league's rates."""
    from ffdraft.league import load_league

    league = load_league("main")
    line = dict(
        passing_yards=300,
        passing_tds=2,
        passing_interceptions=3,
        fumbles_lost_total=1,
    )
    # 300/25 = 12  +  2 x4 = 8  +  3 INT x -2 = -6  +  1 fumble x -2 = -2
    assert score_offense_row(league, line) == pytest.approx(12.0)


# --- real 2024 team defense lines -----------------------------------------

DST_CASES = [
    (
        "DEN 2024 wk5",
        dict(
            sacks=3, interceptions=3, fumble_recoveries=0, forced_fumbles=0, safeties=0,
            def_tds=1, st_tds=0, blocked_kicks=0, points_allowed=18, yards_allowed=330,
        ),
        # 3 sacks x1 = 3  +  3 INT x2 = 6  +  1 def TD x6 = 6
        # +  18 allowed -> band "14-20" = 1  +  330 yds -> band "300-349" = 0
        16.0,
    ),
    (
        "PIT 2024 wk5",
        dict(
            sacks=2, interceptions=2, fumble_recoveries=1, forced_fumbles=2, safeties=0,
            def_tds=0, st_tds=0, blocked_kicks=1, points_allowed=20, yards_allowed=445,
        ),
        # 2 + 4 + (1 FR x2 = 2) + (2 FF x1 = 2) + (1 blocked x2 = 2)
        # +  20 allowed -> "14-20" = 1  +  445 yds -> "400-449" = -3
        10.0,
    ),
    (
        "PIT 2024 wk1",
        dict(
            sacks=2, interceptions=2, fumble_recoveries=1, forced_fumbles=0, safeties=0,
            def_tds=0, st_tds=0, blocked_kicks=0, points_allowed=10, yards_allowed=226,
        ),
        # 2 + 4 + 2  +  10 allowed -> "7-13" = 3  +  226 yds -> "200-299" = 2
        13.0,
    ),
    (
        "DEN 2024 wk1",
        dict(
            sacks=2, interceptions=1, fumble_recoveries=1, forced_fumbles=0, safeties=2,
            def_tds=0, st_tds=0, blocked_kicks=0, points_allowed=26, yards_allowed=304,
        ),
        # 2 + 2 + 2 + (2 safeties x2 = 4)
        # +  26 allowed -> "21-27" = 0  +  304 yds -> "300-349" = 0
        10.0,
    ),
]


@pytest.mark.parametrize("label,line,expected", DST_CASES, ids=[c[0] for c in DST_CASES])
def test_dst_matches_hand_calculation(league, label, line, expected):
    assert score_dst_row(league, line) == pytest.approx(expected)


def test_yards_allowed_stacks_on_points_allowed(league):
    """This league's non-standard rule: both bands score, they do not replace each other."""
    shutout = dict(
        sacks=0, interceptions=0, fumble_recoveries=0, forced_fumbles=0, safeties=0,
        def_tds=0, st_tds=0, blocked_kicks=0, points_allowed=0, yards_allowed=50,
    )
    # points_allowed "0" = 5, yards_allowed "0-99" = 5 -> 10, not 5
    assert score_dst_row(league, shutout) == pytest.approx(10.0)


def test_dst_band_edges(league):
    """Band boundaries are inclusive on both ends and the top band is open."""
    base = dict(
        sacks=0, interceptions=0, fumble_recoveries=0, forced_fumbles=0, safeties=0,
        def_tds=0, st_tds=0, blocked_kicks=0, yards_allowed=325,  # "300-349" = 0
    )
    assert score_dst_row(league, {**base, "points_allowed": 13}) == pytest.approx(3.0)   # "7-13"
    assert score_dst_row(league, {**base, "points_allowed": 14}) == pytest.approx(1.0)   # "14-20"
    assert score_dst_row(league, {**base, "points_allowed": 35}) == pytest.approx(-4.0)  # "35+"
    assert score_dst_row(league, {**base, "points_allowed": 99}) == pytest.approx(-4.0)  # open top


# --- the scorer is driven by config, not by a hardcoded table --------------


def test_scoring_follows_the_config_not_a_default(league):
    """Halving the reception value must halve the reception contribution.

    This is the test that proves the scorer reads main.yml. Matching some other
    library's PPR number would not — the two agree by coincidence of settings.
    """
    half_ppr_scoring = copy.deepcopy(league.scoring)
    half_ppr_scoring["receiving"]["reception"] = 0.5
    half_ppr = League(**{**league.__dict__, "scoring": half_ppr_scoring})

    # 9 rec x0.5 = 4.5  +  54/10 = 5.4  +  6
    assert score_offense_row(half_ppr, CHASE_W8) == pytest.approx(15.9)
    assert score_offense_row(league, CHASE_W8) == pytest.approx(20.4)


def test_six_point_passing_td_changes_qb_scoring(league):
    six_pt = copy.deepcopy(league.scoring)
    six_pt["passing"]["td"] = 6
    modified = League(**{**league.__dict__, "scoring": six_pt})
    # Lamar's two passing TDs go from 8 points to 12.
    assert score_offense_row(modified, LAMAR_W12) == pytest.approx(LAMAR_W12_POINTS + 4)


def test_absent_banded_column_scores_nothing(league):
    """A missing points_allowed is not a shutout.

    Scoring an absent column through the "0" band would invent 5 points from a
    stat that was never recorded.
    """
    row = dict(sacks=1, interceptions=0, fumble_recoveries=0, forced_fumbles=0,
               safeties=0, def_tds=0, st_tds=0, blocked_kicks=0)
    assert score_dst_row(league, row) == pytest.approx(1.0)


# --- frame scoring matches row scoring -------------------------------------


def test_frame_and_row_paths_agree(league):
    df = pl.DataFrame(
        [{**dict.fromkeys(_ALL_OFFENSE_COLUMNS, 0), **line} for _, line, _ in OFFENSE_CASES]
    )
    scored, plan = score_offense(league, df)
    assert not plan.missing_required
    for computed, (_, line, expected) in zip(
        scored["fantasy_points_league"].to_list(), OFFENSE_CASES, strict=True
    ):
        assert computed == pytest.approx(expected)
        assert computed == pytest.approx(score_offense_row(league, line))


_ALL_OFFENSE_COLUMNS = [
    "passing_yards", "passing_tds", "passing_2pt_conversions", "passing_interceptions",
    "rushing_yards", "rushing_tds", "rushing_2pt_conversions",
    "receptions", "receiving_yards", "receiving_tds", "receiving_2pt_conversions",
    "fumbles_lost_total", "fumble_recovery_tds", "special_teams_tds",
    "def_fumbles_forced", "fumble_recovery_opp",
]


def test_strict_mode_refuses_to_score_missing_columns(league):
    """Silently treating an absent required column as zero would understate points."""
    df = pl.DataFrame({"receptions": [5]})
    with pytest.raises(ValueError, match="missing required column"):
        score_offense(league, df)


def test_plan_reports_what_it_could_not_score(league):
    plan = offense_plan(league, ["receptions", "receiving_yards"])
    assert "passing_yards" in plan.missing_required
    described = plan.describe()
    assert described["known_gaps"], "known data gaps must be reported, not hidden"


def test_dst_plan_covers_every_dst_setting(league):
    """Every scored DST setting in main.yml should map to a column."""
    plan = dst_plan(
        league,
        ["sacks", "interceptions", "fumble_recoveries", "forced_fumbles", "safeties",
         "def_tds", "st_tds", "blocked_kicks", "points_allowed", "yards_allowed"],
    )
    assert not plan.missing_required
    assert not plan.missing_optional
    applied = {term for term in plan.describe()["applied_terms"]}
    assert "dst.points_allowed" in applied
    assert "dst.yards_allowed" in applied


def test_dst_frame_scoring(league):
    df = pl.DataFrame([line for _, line, _ in DST_CASES])
    scored, plan = score_dst(league, df)
    assert not plan.missing_required
    for computed, (_, _, expected) in zip(
        scored["fantasy_points_league"].to_list(), DST_CASES, strict=True
    ):
        assert computed == pytest.approx(expected)
