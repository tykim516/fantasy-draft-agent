"""Baselines, tiers, and usage aggregation.

The theme: nothing may assume 12 teams. Every baseline in these tests is checked
against a number derived from the league config.
"""

from __future__ import annotations

import polars as pl
import pytest

from ffdraft.metrics import INSUFFICIENT_DATA
from ffdraft.metrics.tiers import add_tiers, assign_tiers, find_breaks, tier_summary
from ffdraft.metrics.usage import flag_rookies, points_over_expected, usage_profile
from ffdraft.metrics.vor import add_vor, compute_baselines, positional_dropoff


def make_pool(counts: dict[str, int] = None) -> pl.DataFrame:
    """A synthetic projection pool: descending points, plenty at each position."""
    counts = counts or {"QB": 40, "RB": 70, "WR": 90, "TE": 40, "DST": 32, "K": 32}
    rows = []
    for position, n in counts.items():
        for i in range(n):
            rows.append(
                {
                    "player": f"{position}{i + 1}",
                    "position": position,
                    "proj_pts": 400.0 - i * 5.0,
                }
            )
    return pl.DataFrame(rows)


# --- replacement level -----------------------------------------------------


def test_baselines_come_from_teams_times_slots(league):
    base = compute_baselines(league, make_pool())
    assert base.base_starters["QB"] == 10   # 10 teams x 1 QB
    assert base.base_starters["RB"] == 20   # 10 teams x 2 RB
    assert base.base_starters["WR"] == 20
    assert base.base_starters["TE"] == 10
    assert base.base_starters["DST"] == 10


def test_qb_replacement_sits_high_in_a_ten_team_league(league):
    """The whole reason a generic board is wrong here.

    QB takes no flex slots in this league (SUPERFLEX is 0), so replacement is
    exactly the 11th quarterback — against QB13 in a 12-team league. TE is flex
    eligible, so its replacement depends on the modelled allocation and is
    checked in test_replacement_rank_is_base_plus_flex_plus_one.
    """
    base = compute_baselines(league, make_pool())
    assert base.replacement_rank["QB"] == 11
    assert base.replacement_rank["DST"] == 11
    # TE is flex eligible, so it can only sit at or above its dedicated slots.
    assert base.replacement_rank["TE"] >= 11


def test_flex_allocation_is_modelled_not_assumed(league):
    base = compute_baselines(league, make_pool())
    allocated = sum(base.flex_allocation[p] for p in ("RB", "WR", "TE"))
    assert allocated == league.flex_slots_total() == 20
    # Positions that no flex slot accepts must receive none.
    assert base.flex_allocation["QB"] == 0
    assert base.flex_allocation["DST"] == 0


def test_flex_allocation_follows_projected_points(league):
    """If WRs project higher, they should win more of the flex slots."""
    rows = []
    for i in range(60):
        rows.append({"player": f"WR{i+1}", "position": "WR", "proj_pts": 300.0 - i})
    for i in range(60):
        rows.append({"player": f"RB{i+1}", "position": "RB", "proj_pts": 250.0 - i})
    for i in range(30):
        rows.append({"player": f"TE{i+1}", "position": "TE", "proj_pts": 150.0 - i})
    for i in range(20):
        rows.append({"player": f"QB{i+1}", "position": "QB", "proj_pts": 350.0 - i})
    for i in range(20):
        rows.append({"player": f"D{i+1}", "position": "DST", "proj_pts": 120.0 - i})
    base = compute_baselines(league, pl.DataFrame(rows))
    assert base.flex_allocation["WR"] > base.flex_allocation["RB"]
    assert base.flex_allocation["TE"] == 0


def test_replacement_rank_is_base_plus_flex_plus_one(league):
    base = compute_baselines(league, make_pool())
    for position in ("RB", "WR", "TE"):
        expected = base.base_starters[position] + base.flex_allocation[position] + 1
        assert base.replacement_rank[position] == expected


def test_baselines_report_their_assumptions(league):
    described = compute_baselines(league, make_pool()).describe()
    assert described["rostered_players_leaguewide"] == 140
    assert described["flex_allocation_modelled"]
    joined = " ".join(described["notes"])
    assert "flex slots modelled" in joined
    assert "12-team defaults" in joined


def test_thin_position_pool_is_flagged_not_silently_optimistic(league):
    """Fewer players than slots must produce a note, not a quiet best guess."""
    base = compute_baselines(league, make_pool({"QB": 3, "RB": 40, "WR": 40, "TE": 40, "DST": 32}))
    assert any("QB" in note and "optimistic" in note for note in base.notes)


# --- VOR -------------------------------------------------------------------


def test_add_vor_excludes_positions_outside_the_universe(league):
    scored, _ = add_vor(league, make_pool())
    assert "K" not in scored["position"].unique().to_list()


def test_vor_is_points_minus_replacement(league):
    scored, base = add_vor(league, make_pool())
    row = scored.filter(pl.col("position") == "RB").head(1).to_dicts()[0]
    assert row["vor"] == pytest.approx(row["proj_pts"] - base.replacement_points["RB"])


def test_replacement_level_player_has_zero_vor(league):
    scored, base = add_vor(league, make_pool())
    rbs = scored.filter(pl.col("position") == "RB").sort("proj_pts", descending=True)
    replacement_index = base.replacement_rank["RB"] - 1
    assert rbs["vor"].to_list()[replacement_index] == pytest.approx(0.0)


def test_dropoff_curve_is_monotonic(league):
    curve = positional_dropoff(make_pool(), "QB", top_n=12)
    assert curve["pos_rank"].to_list() == list(range(1, 13))
    assert curve["drop_from_best"].to_list() == sorted(curve["drop_from_best"].to_list())


# --- tiers -----------------------------------------------------------------


def test_tiers_break_on_large_gaps():
    values = [100.0, 99.0, 98.0, 60.0, 59.0, 58.0]
    tiers = assign_tiers(values, max_tier_size=None)
    assert tiers[:3] == [1, 1, 1]
    assert tiers[3:] == [2, 2, 2]


def test_even_spacing_produces_one_tier():
    values = [100.0 - i for i in range(10)]
    assert set(assign_tiers(values, max_tier_size=None)) == {1}


def test_max_tier_size_splits_a_flat_run():
    values = [100.0 - i for i in range(20)]
    tiers = assign_tiers(values, max_tier_size=5)
    assert max(tiers) >= 4


def test_breaks_explain_themselves():
    breaks = find_breaks([100.0, 99.0, 98.0, 60.0, 59.0], max_tier_size=None)
    assert breaks
    assert breaks[0].after_rank == 3
    assert "sd" in breaks[0].reason


def test_short_series_is_one_tier():
    assert assign_tiers([10.0, 5.0]) == [1, 1]


def test_add_tiers_within_position():
    df = pl.DataFrame(
        {
            "player": [f"p{i}" for i in range(12)],
            "position": ["RB"] * 6 + ["WR"] * 6,
            "vor": [100.0, 99.0, 98.0, 40.0, 39.0, 38.0, 90.0, 89.0, 88.0, 30.0, 29.0, 28.0],
        }
    )
    tiered = add_tiers(df, "vor", over="position", max_tier_size=None)
    for position in ("RB", "WR"):
        part = tiered.filter(pl.col("position") == position).sort("vor", descending=True)
        assert part["tier"].to_list() == [1, 1, 1, 2, 2, 2]


def test_tier_summary_reports_spread():
    df = pl.DataFrame({"vor": [100.0, 99.0, 60.0, 59.0], "tier": [1, 1, 2, 2]})
    summary = tier_summary(df).sort("tier")
    assert summary["players"].to_list() == [2, 2]
    assert summary["spread"].to_list() == [1.0, 1.0]


def test_empty_frame_is_handled():
    empty = pl.DataFrame({"vor": []})
    assert "tier" in add_tiers(empty).columns


# --- usage -----------------------------------------------------------------


def weekly_frame() -> pl.DataFrame:
    rows = []
    for week in range(1, 13):
        rows.append(
            {
                "player_id": "A",
                "season": 2025,
                "week": week,
                "target_share": 0.20 if week <= 6 else 0.30,  # role grew
                "wopr": 0.5 if week <= 6 else 0.8,
                "targets": 5,
                "rz20_touches": 1,
            }
        )
    rows += [
        {
            "player_id": "B",
            "season": 2025,
            "week": week,
            "target_share": 0.10,
            "wopr": 0.2,
            "targets": 2,
            "rz20_touches": 0,
        }
        for week in range(1, 3)  # only two games
    ]
    return pl.DataFrame(rows)


def test_usage_profile_computes_trend():
    profile = usage_profile(weekly_frame(), season=2025)
    a = profile.filter(pl.col("player_id") == "A").to_dicts()[0]
    assert a["games"] == 12
    assert a["target_share"] == pytest.approx(0.25)
    assert a["target_share_last6"] == pytest.approx(0.30)
    assert a["target_share_trend"] == pytest.approx(0.05)  # role is growing


def test_small_sample_is_flagged():
    profile = usage_profile(weekly_frame(), season=2025)
    b = profile.filter(pl.col("player_id") == "B").to_dicts()[0]
    assert b["games"] == 2
    assert b["data_status"] == INSUFFICIENT_DATA


def test_sample_note_is_always_present():
    profile = usage_profile(weekly_frame(), season=2025)
    assert profile["sample_note"].null_count() == 0


def test_rookies_are_marked_insufficient_data():
    profile = usage_profile(weekly_frame(), season=2025)
    flagged = flag_rookies(profile, rookie_ids={"A"})
    a = flagged.filter(pl.col("player_id") == "A").to_dicts()[0]
    assert a["data_status"] == INSUFFICIENT_DATA
    assert a["rookie"] is True


def test_points_over_expected_is_actual_minus_expected():
    weekly = pl.DataFrame(
        {
            "player_id": ["A", "A", "B", "B"],
            "total_fantasy_points": [20.0, 30.0, 10.0, 10.0],
            "total_fantasy_points_exp": [15.0, 20.0, 14.0, 14.0],
        }
    )
    poe = points_over_expected(weekly).sort("player_id")
    assert poe["poe"].to_list() == pytest.approx([15.0, -8.0])
    assert poe["poe_per_game"].to_list() == pytest.approx([7.5, -4.0])


def test_points_over_expected_requires_its_columns():
    with pytest.raises(ValueError, match="needs column"):
        points_over_expected(pl.DataFrame({"player_id": ["A"]}))


def test_usage_profile_requires_week():
    with pytest.raises(ValueError, match="week"):
        usage_profile(pl.DataFrame({"player_id": ["A"], "season": [2025]}), season=2025)
