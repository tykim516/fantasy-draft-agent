"""Integration tests for the named queries, against a populated warehouse.

These skip cleanly when `data/ff.duckdb` does not exist, so a fresh clone can run
the suite before ingesting. Run `python scripts/ingest.py` to enable them.

What they check is not "does it return rows" but the properties that would let a
board be quietly wrong: kickers leaking into the universe, rookies vanishing, a
crosswalk silently dropping players, a rate computed over the wrong denominator.
"""

from __future__ import annotations

import polars as pl
import pytest

from ffdraft.config import load_sources
from ffdraft.warehouse import run_named

pytestmark = pytest.mark.warehouse


@pytest.fixture(scope="module")
def seasons():
    sources = load_sources()
    return sources["seasons"]["history"][-1], sources["seasons"]["draft_season"]


@pytest.fixture(scope="module")
def usage(warehouse, seasons):
    return run_named(warehouse, "usage_profile", {"season": seasons[0]})


@pytest.fixture(scope="module")
def market(warehouse, league):
    return run_named(
        warehouse,
        "adp_deltas",
        {
            "page_type": "redraft-overall",
            "teams": league.teams,
            "exclude_positions": list(league.excluded_positions),
        },
    )


@pytest.fixture(scope="module")
def roster(warehouse, seasons):
    return run_named(
        warehouse, "roster_context", {"season": seasons[0], "draft_season": seasons[1]}
    )


# --- usage_profile ---------------------------------------------------------


def test_usage_profile_returns_rows(usage):
    assert usage.height > 200


def test_usage_profile_excludes_kickers(usage):
    assert set(usage["position"].unique().to_list()) <= {"QB", "RB", "WR", "TE"}


def test_usage_profile_rates_are_in_range(usage):
    for column in ("target_share", "snap_pct"):
        values = usage[column].drop_nulls()
        assert values.min() >= 0.0
        assert values.max() <= 1.0, f"{column} exceeded 1.0 — wrong denominator?"


def test_air_yards_share_may_be_slightly_negative(usage):
    """Not a bug: targets behind the line of scrimmage carry negative air yards,
    so a player's share of a small team total can go below zero. Large negatives
    would mean a broken denominator."""
    values = usage["air_yards_share"].drop_nulls()
    assert values.max() <= 1.0
    assert values.min() > -0.5


def test_usage_profile_flags_small_samples(usage):
    small = usage.filter(pl.col("games") < 4)
    if small.height:
        assert set(small["data_status"].unique().to_list()) == {"insufficient_data"}


def test_usage_profile_last6_never_exceeds_season_games(usage):
    both = usage.filter(pl.col("games_last6").is_not_null())
    assert (both["games_last6"] <= both["games"]).all()
    assert both["games_last6"].max() <= 6


def test_usage_profile_snap_counts_actually_joined(usage):
    """A silently failed pfr_id crosswalk would leave this column all null."""
    assert usage["snap_pct"].null_count() < usage.height * 0.25


def test_usage_profile_expected_points_joined(usage):
    assert usage["poe"].null_count() < usage.height * 0.5


# --- points_over_expected --------------------------------------------------


def test_poe_is_actual_minus_expected(warehouse, seasons):
    df = run_named(warehouse, "points_over_expected", {"season": seasons[0], "min_games": 8})
    assert df.height > 100
    computed = (df["actual_points"] - df["expected_points"]).round(1)
    assert (computed - df["poe"]).abs().max() < 0.15


def test_poe_respects_min_games(warehouse, seasons):
    df = run_named(warehouse, "points_over_expected", {"season": seasons[0], "min_games": 10})
    assert df["games"].min() >= 10


def test_poe_read_column_matches_the_number(warehouse, seasons):
    df = run_named(warehouse, "points_over_expected", {"season": seasons[0], "min_games": 8})
    hot = df.filter(pl.col("read").str.starts_with("ran hot"))
    if hot.height:
        assert hot["poe_per_game"].min() > 3


# --- adp_deltas ------------------------------------------------------------


def test_market_excludes_league_excluded_positions(market, league):
    positions = set(market["position"].unique().to_list())
    for excluded in league.excluded_positions:
        assert excluded not in positions, f"{excluded} must never reach the board"


def test_market_rank_is_dense_and_ordered(market):
    assert market["market_rank"].to_list() == list(range(1, market.height + 1))
    assert market["ecr"].to_list() == sorted(market["ecr"].to_list())


def test_market_round_uses_this_league_size(market, league):
    """Round boundaries must come from the config, not a 12-team default."""
    first = market.head(league.teams)
    assert set(first["market_round"].to_list()) == {1}
    second = market.slice(league.teams, league.teams)
    assert set(second["market_round"].to_list()) == {2}


def test_unlinked_players_are_returned_not_dropped(market):
    """A player silently missing is worse than one flagged as unjoinable."""
    assert set(market["crosswalk_status"].unique().to_list()) <= {"linked", "unlinked"}
    linked = market.filter(pl.col("crosswalk_status") == "linked")
    assert linked.height / market.height > 0.6


def test_top_of_the_board_links_almost_perfectly(market):
    top = market.head(100)
    assert top.filter(pl.col("crosswalk_status") == "unlinked").height <= 10


def test_market_carries_its_source_and_date(market):
    assert market["market_as_of"].null_count() == 0
    assert market["market_source"].unique().to_list() == [
        "ECR (FantasyPros consensus via ffverse ff_rankings)"
    ]


# --- roster_context --------------------------------------------------------


def test_roster_context_includes_rookies(roster):
    """Starting from prior usage alone would drop every rookie off the board."""
    rookies = roster.filter(pl.col("rookie"))
    assert rookies.height > 20
    assert set(rookies["data_status"].unique().to_list()) == {"insufficient_data"}


def test_rookies_have_no_fabricated_usage(roster):
    rookies = roster.filter(pl.col("rookie"))
    assert rookies["games"].max() == 0
    assert rookies["targets"].drop_nulls().sum() == 0


def test_ir_eligible_is_the_reserve_list_only(roster):
    """RET and CUT are also 'not active' but are not stashes."""
    flagged = roster.filter(pl.col("ir_eligible"))
    assert set(flagged["roster_status_raw"].unique().to_list()) == {"RES"}
    assert flagged.height < roster.height * 0.1


def test_role_status_is_three_valued(roster):
    assert set(roster["role_status"].unique().to_list()) <= {
        "lead",
        "rotational",
        "contingent",
        "unknown",
    }


def test_unknown_depth_is_not_reported_as_contingent(roster):
    """Absence of evidence must not masquerade as a finding."""
    unknown = roster.filter(pl.col("role_status") == "unknown")
    assert not unknown["contingent_role"].any()


def test_team_shares_are_fractions(roster):
    for column in ("team_target_share", "team_carry_share"):
        values = roster[column].drop_nulls()
        assert values.min() >= 0.0
        assert values.max() <= 1.0


def test_lead_backs_hold_most_of_their_backfield(roster):
    """Sanity check on role_status: a lead back who played a real season should
    own a large share of his backfield. Restricted to 8+ games, because a player
    who is his team's RB1 across two appearances is a labelling artefact."""
    leads = roster.filter(
        (pl.col("position") == "RB")
        & (pl.col("role_status") == "lead")
        & (pl.col("games") >= 8)
    )
    assert leads.height > 10
    assert leads["team_carry_share"].drop_nulls().median() > 0.35


# --- scoring against the real warehouse ------------------------------------


def test_league_scoring_runs_on_the_real_player_stats(warehouse, league, seasons):
    from ffdraft.scoring import score_offense

    df = warehouse.execute(
        "SELECT * FROM player_stats WHERE season = ? AND season_type = 'REG'", [seasons[0]]
    ).pl()
    scored, plan = score_offense(league, df)
    assert not plan.missing_required, plan.missing_required
    assert scored["fantasy_points_league"].null_count() == 0


def test_dst_scoring_runs_on_the_real_dst_stats(warehouse, league, seasons):
    from ffdraft.scoring import score_dst

    df = warehouse.execute("SELECT * FROM dst_stats WHERE season = ?", [seasons[0]]).pl()
    scored, plan = score_dst(league, df)
    assert not plan.missing_required
    assert scored["fantasy_points_league"].null_count() == 0
    # Every team-week must land in exactly one band for both stacked DST tables.
    assert scored.height == df.height


def test_dst_stats_yards_allowed_is_plausible(warehouse, seasons):
    """Guards the sack-yardage sign bug: the real league average is ~337/game."""
    mean = warehouse.execute(
        "SELECT avg(yards_allowed) FROM dst_stats WHERE season = ?", [seasons[0]]
    ).fetchone()[0]
    assert 300 < mean < 370, f"league-average yards allowed of {mean:.1f} is not credible"


def test_dst_stats_points_allowed_is_plausible(warehouse, seasons):
    mean = warehouse.execute(
        "SELECT avg(points_allowed) FROM dst_stats WHERE season = ?", [seasons[0]]
    ).fetchone()[0]
    assert 18 < mean < 27, f"league-average points allowed of {mean:.1f} is not credible"


def test_fumble_recoveries_are_recoveries_not_defensive_fumbles(warehouse, seasons):
    """def_fumbles averages ~0.11/game; real recoveries are ~0.5."""
    mean = warehouse.execute(
        "SELECT avg(fumble_recoveries) FROM dst_stats WHERE season = ?", [seasons[0]]
    ).fetchone()[0]
    assert 0.3 < mean < 0.8, f"{mean:.2f} recoveries per team-game is not credible"
