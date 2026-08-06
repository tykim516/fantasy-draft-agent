"""League config validation.

The governing requirement: validation FAILS LOUDLY. A league setting that
silently defaults produces a board that is wrong in a way nobody notices, which
is worse than a board that refuses to build. Most of these tests assert that
something is rejected.
"""

from __future__ import annotations

import dataclasses

import pytest

from ffdraft.league import (
    League,
    LeagueValidationError,
    build_bands,
    load_league,
    parse_band_key,
    score_band,
    validate,
)

# --- the requirement called out in main.yml itself -------------------------


def test_null_slot_count_fails_rather_than_defaulting(raw_league, schema):
    """`K:` with no value must fail, not quietly become 0.

    This is the case main.yml names explicitly. A null slot is a config bug; a
    zero is a deliberate statement that the league does not use the slot.
    """
    raw_league["roster"]["K"] = None
    errors = validate(raw_league, schema)
    assert any("roster.K is null" in e for e in errors)
    assert any("config bug" in e for e in errors)


def test_zero_slot_is_accepted(raw_league, schema):
    """Zero is how you say "this league has no kicker" — it must pass."""
    assert raw_league["roster"]["K"] == 0
    assert validate(raw_league, schema) == []


def test_missing_slot_fails(raw_league, schema):
    del raw_league["roster"]["TE"]
    errors = validate(raw_league, schema)
    assert any("missing slot 'TE'" in e for e in errors)


# --- structural validation -------------------------------------------------


def test_valid_config_produces_no_errors(raw_league, schema):
    assert validate(raw_league, schema) == []


def test_missing_required_key_fails(raw_league, schema):
    del raw_league["teams"]
    assert any("missing required key 'teams'" in e for e in validate(raw_league, schema))


def test_unknown_top_level_key_fails(raw_league, schema):
    raw_league["playoff_team"] = 6  # typo for playoff_teams
    errors = validate(raw_league, schema)
    assert any("unknown top-level key 'playoff_team'" in e for e in errors)


def test_unknown_roster_slot_fails(raw_league, schema):
    raw_league["roster"]["WRT"] = 1
    assert any("unknown roster slot 'WRT'" in e for e in validate(raw_league, schema))


def test_bool_is_not_an_int(raw_league, schema):
    """`teams: true` is not a team count, even though bool subclasses int."""
    raw_league["teams"] = True
    assert any("'teams' must be int" in e for e in validate(raw_league, schema))


def test_enum_is_enforced(raw_league, schema):
    raw_league["waivers"] = "blind_bidding"
    assert any("must be one of" in e for e in validate(raw_league, schema))


def test_out_of_range_fails(raw_league, schema):
    raw_league["teams"] = 1
    assert any("must be >= 2" in e for e in validate(raw_league, schema))


def test_all_errors_are_reported_at_once(raw_league, schema):
    """Fixing a config one error per run is miserable."""
    del raw_league["teams"]
    raw_league["roster"]["K"] = None
    raw_league["waivers"] = "nonsense"
    assert len(validate(raw_league, schema)) >= 3


# --- cross-field rules -----------------------------------------------------


def test_playoff_teams_cannot_exceed_teams(raw_league, schema):
    raw_league["playoff_teams"] = 12
    assert any("exceeds teams" in e for e in validate(raw_league, schema))


def test_trade_deadline_must_precede_playoffs(raw_league, schema):
    raw_league["trade_deadline_week"] = 16
    assert any("must be before" in e for e in validate(raw_league, schema))


# --- scoring validation ----------------------------------------------------


def test_missing_scoring_group_fails(raw_league, schema):
    del raw_league["scoring"]["receiving"]
    assert any("missing required group 'receiving'" in e for e in validate(raw_league, schema))


def test_missing_scoring_key_fails(raw_league, schema):
    del raw_league["scoring"]["passing"]["interception"]
    assert any("missing required key 'interception'" in e for e in validate(raw_league, schema))


def test_zero_divisor_fails(raw_league, schema):
    """yards_per_point is a divisor; zero would be a ZeroDivisionError mid-board."""
    raw_league["scoring"]["rushing"]["yards_per_point"] = 0
    assert any("must be > 0" in e for e in validate(raw_league, schema))


def test_kicking_block_required_only_when_kicker_is_rostered(raw_league, schema):
    """main.yml omits kicking scoring, and that is correct — K is 0."""
    assert validate(raw_league, schema) == []
    raw_league["roster"]["K"] = 1
    errors = validate(raw_league, schema)
    assert any("scoring.kicking is required" in e for e in errors)


# --- banded scoring --------------------------------------------------------


@pytest.mark.parametrize(
    "key,expected",
    [("0", (0, 0)), ("1-6", (1, 6)), ("35+", (35, None)), ("500-549", (500, 549))],
)
def test_band_key_parsing(key, expected):
    assert parse_band_key(key) == expected


def test_band_gap_fails(raw_league, schema):
    bands = raw_league["scoring"]["dst"]["points_allowed"]
    del bands["14-20"]
    assert any("leaves a gap after" in e for e in validate(raw_league, schema))


def test_band_overlap_fails(raw_league, schema):
    raw_league["scoring"]["dst"]["points_allowed"]["5-15"] = 2
    assert any("overlaps" in e for e in validate(raw_league, schema))


def test_band_must_be_open_topped(raw_league, schema):
    bands = raw_league["scoring"]["dst"]["points_allowed"]
    bands["35-40"] = bands.pop("35+")
    errors = validate(raw_league, schema)
    assert any("open-topped" in e for e in errors)


def test_band_must_start_at_zero(raw_league, schema):
    bands = raw_league["scoring"]["dst"]["points_allowed"]
    bands["1"] = bands.pop("0")
    assert any("must start at 0" in e for e in validate(raw_league, schema))


def test_every_value_maps_to_exactly_one_band(league):
    """A validated band table must cover every real game with no holes."""
    bands = league.bands("dst", "points_allowed")
    for value in range(0, 100):
        matches = [b for b in bands if b.contains(value)]
        assert len(matches) == 1, f"{value} matched {[b.key for b in matches]}"


def test_score_band_raises_outside_every_band():
    bands = build_bands({"0-9": 1, "10-19": 2})
    assert score_band(bands, 5) == 1
    with pytest.raises(ValueError, match="fell outside every band"):
        score_band(bands, 50)


# --- derived league shape --------------------------------------------------


def test_derived_roster_shape(league):
    assert league.teams == 10
    assert league.starters_per_team == 9
    assert league.roster_spots == 14           # 9 starters + 5 bench; IR does not count
    assert league.rostered_players == 140      # the real depth constraint
    assert league.bench_spots == 5
    assert league.ir_slots == 2


def test_kickers_are_excluded_from_the_universe(league):
    """Derived from K: 0, not hardcoded anywhere."""
    assert "K" not in league.player_universe
    assert league.excluded_positions == ("K",)
    assert set(league.player_universe) == {"QB", "RB", "WR", "TE", "DST"}


def test_flex_slots_are_computed_from_the_config(league):
    assert league.flex_slots == {"FLEX": ["RB", "WR", "TE"]}
    assert league.flex_slots_total() == 20     # 10 teams x 2 FLEX
    assert league.flex_eligible_positions() == {"RB", "WR", "TE"}
    assert league.base_starters("RB") == 20    # 10 teams x 2 RB
    assert league.base_starters("QB") == 10


def test_superflex_absent_means_no_superflex_slot(league):
    """SUPERFLEX is 0 here, so it must not appear as a usable flex slot."""
    assert "SUPERFLEX" not in league.flex_slots


def test_assumptions_block_carries_what_agents_need(league):
    a = league.assumptions()
    assert a["scoring"] == "full PPR"
    assert a["rostered_players_leaguewide"] == 140
    assert a["excluded_positions"] == ["K"]
    assert a["bench_spots"] == 5


# --- loader ---------------------------------------------------------------


def test_load_league_raises_with_every_error(tmp_path):
    import yaml

    from ffdraft.config import load_yaml
    from ffdraft.league import league_path

    broken = load_yaml(league_path("main"))
    broken["roster"]["RB"] = None
    del broken["teams"]
    path = tmp_path / "broken.yml"
    path.write_text(yaml.safe_dump(broken))

    with pytest.raises(LeagueValidationError) as excinfo:
        load_league(path)
    assert len(excinfo.value.errors) >= 2
    assert "broken.yml" in str(excinfo.value)


def test_league_is_immutable(league):
    """Frozen, so an agent cannot mutate the league mid-run and change baselines."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        league.teams = 12


def test_league_type_is_constructible_for_variants(league):
    """Tests build modified leagues by copying; that must keep working."""
    variant = League(**{**league.__dict__, "name": "variant"})
    assert variant.name == "variant"
    assert variant.rostered_players == league.rostered_players
