"""Identity resolution for the hand-maintained Sleeper ADP file.

The governing requirement mirrors the one in test_league.py: this must never
guess. The project forbids joining on player name because a wrong match corrupts
a board silently — a real player inheriting another player's usage is worse than
a missing row, because nothing looks wrong.

So most of these tests assert that an ambiguous case is *refused*, not resolved.
"""

from __future__ import annotations

import pytest
import yaml

from ffdraft.market.resolve import (
    Index,
    alias_key,
    load_aliases,
    normalize_name,
    normalize_position,
    parse_position_adp,
    resolve_row,
    write_pending,
)

# gsis_ids used below are the real ones, so a copy-paste into the alias file works.
CHASE = "00-0036900"
AJ_BROWN = "00-0035676"
AJ_BROWN_RETIRED = "00-0031234"
TANK_DELL = "00-0038977"


@pytest.fixture
def index() -> Index:
    return Index(
        by_name_position={
            ("jamarr chase", "WR"): {CHASE},
            ("aj brown", "WR"): {AJ_BROWN, AJ_BROWN_RETIRED},
            ("kenneth walker", "RB"): {"00-0037746"},
        },
        active={CHASE, AJ_BROWN, "00-0037746"},
        teams_by_name={"cleveland browns": "CLE", "los angeles rams": "LA", "buffalo bills": "BUF"},
        sleeper_to_gsis={"4034": CHASE},
        teams_for={CHASE: {"CIN"}, AJ_BROWN: {"PHI"}, AJ_BROWN_RETIRED: {"TEN"}},
        team_aliases={"SF": "SFO", "LAR": "LA"},
    )


# --- normalization ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Ja'Marr Chase", "jamarr chase"),
        ("A.J. Brown", "aj brown"),
        ("Kenneth Walker III", "kenneth walker"),
        ("Marvin Harrison Jr.", "marvin harrison"),
        ("Amon-Ra St. Brown", "amon ra st brown"),
        ("  Bijan   Robinson ", "bijan robinson"),
        ("", ""),
    ],
)
def test_name_normalization(raw, expected):
    assert normalize_name(raw) == expected


def test_suffix_stripping_does_not_eat_a_real_name():
    """`V` is a suffix; `Vic` is not. Over-eager stripping loses players."""
    assert normalize_name("Vic Beasley") == "vic beasley"
    assert normalize_name("Robert Griffin III") == "robert griffin"


def test_position_vocabularies_are_reconciled():
    """The ADP file says DEF; the league config says DST."""
    assert normalize_position("DEF") == "DST"
    assert normalize_position("D/ST") == "DST"
    assert normalize_position("rb") == "RB"


@pytest.mark.parametrize(
    "raw,expected",
    [("RB1", ("RB", 1)), ("WR12", ("WR", 12)), ("DEF17", ("DST", 17)), ("QB", ("QB", None))],
)
def test_position_adp_parsing(raw, expected):
    assert parse_position_adp(raw) == expected


def test_unparseable_position_is_empty_not_a_crash():
    assert parse_position_adp("???") == ("", None)
    assert parse_position_adp("") == ("", None)


# --- the resolution ladder -------------------------------------------------


def test_unique_name_and_position_links(index):
    resolution = resolve_row("Ja'Marr Chase", "WR", index, {})
    assert resolution.gsis_id == CHASE
    assert resolution.method == "auto"
    assert resolution.linked


def test_namesake_is_broken_by_who_actually_plays(index):
    """Two A.J. Browns exist. Only one is in the league, so this is not a guess."""
    resolution = resolve_row("A.J. Brown", "WR", index, {})
    assert resolution.gsis_id == AJ_BROWN
    assert resolution.method == "activity"


def test_namesakes_who_all_play_are_refused(index):
    """When the tiebreak does not decide, nothing is picked."""
    index.active.add(AJ_BROWN_RETIRED)
    resolution = resolve_row("A.J. Brown", "WR", index, {})
    assert resolution.gsis_id is None
    assert resolution.method == "unlinked"
    assert set(resolution.candidates) == {AJ_BROWN, AJ_BROWN_RETIRED}


def test_namesakes_where_none_play_are_refused(index):
    index.active.clear()
    assert resolve_row("A.J. Brown", "WR", index, {}).method == "unlinked"


def test_unknown_name_is_unlinked_with_no_candidates(index):
    resolution = resolve_row("Nathaniel Dell", "WR", index, {})
    assert resolution.method == "unlinked"
    assert resolution.candidates == ()


def test_a_confirmed_alias_beats_every_heuristic(index):
    """Human decisions are top of the ladder, including over a unique auto match."""
    aliases = {alias_key("Ja'Marr Chase", "WR"): "00-9999999"}
    resolution = resolve_row("Ja'Marr Chase", "WR", index, aliases)
    assert resolution.gsis_id == "00-9999999"
    assert resolution.method == "alias"


def test_an_alias_rescues_a_name_the_index_does_not_have(index):
    aliases = {alias_key("Nathaniel Dell", "WR"): TANK_DELL}
    assert resolve_row("Nathaniel Dell", "WR", index, aliases).gsis_id == TANK_DELL


def test_position_matters_to_the_match(index):
    """The same name at another position is a different player, not a fallback."""
    assert resolve_row("Ja'Marr Chase", "RB", index, {}).method == "unlinked"


def test_ids_in_the_file_are_used_directly(index):
    assert resolve_row("Whoever", "WR", index, {}, file_gsis_id=CHASE).method == "direct"
    assert resolve_row("Whoever", "WR", index, {}, file_sleeper_id="4034").gsis_id == CHASE


def test_an_unknown_sleeper_id_falls_through_to_name_matching(index):
    resolution = resolve_row("Ja'Marr Chase", "WR", index, {}, file_sleeper_id="nope")
    assert resolution.method == "auto"


# --- team as evidence ------------------------------------------------------
#
# The August 12 export packs the team into the player cell ("A.J. Brown WR PHI"),
# which is direct evidence about identity rather than the inference the activity
# tiebreak makes. It therefore sits above `activity` on the ladder.


def test_team_breaks_a_namesake_tie(index):
    resolution = resolve_row("A.J. Brown", "WR", index, {}, team="PHI")
    assert resolution.gsis_id == AJ_BROWN
    assert resolution.method == "team"


def test_team_outranks_the_activity_heuristic(index):
    """When both would fire, the stated team wins — it is evidence, not a guess.

    Here the retired namesake is also marked active, so activity cannot decide;
    only the team can.
    """
    index.active.add(AJ_BROWN_RETIRED)
    assert resolve_row("A.J. Brown", "WR", index, {}, team="TEN").gsis_id == AJ_BROWN_RETIRED


def test_a_team_nobody_matches_falls_through_rather_than_guessing(index):
    """A wrong team must not silently pick someone; it drops to the next rung."""
    resolution = resolve_row("A.J. Brown", "WR", index, {}, team="NYJ")
    assert resolution.method == "activity"
    assert resolution.gsis_id == AJ_BROWN


def test_team_spelling_variants_are_reconciled(index):
    """Exports say SF where this warehouse says SFO, and LAR where it says LA. A
    tiebreak that failed on spelling would quietly fall to a weaker rung."""
    assert index.canonical_team("LAR") == "LA"
    assert index.canonical_team("SF") == "SFO"
    assert index.canonical_team("CLE") == "CLE"
    assert index.canonical_team(None) is None
    # An unknown code passes through rather than being rejected: it can only
    # produce an empty intersection, which correctly falls through to the next
    # rung. Rejecting it here would couple the tiebreak to the `teams` table.
    assert index.canonical_team("XXX") == "XXX"


def test_team_is_optional(index):
    """Older exports carry no team column at all; the ladder must still work."""
    assert resolve_row("Ja'Marr Chase", "WR", index, {}).method == "auto"


# --- team defenses ---------------------------------------------------------


def test_team_defense_links_on_team_abbreviation(index):
    """A DST has no gsis_id; the team abbreviation is its join key."""
    resolution = resolve_row("Cleveland Browns", "DEF", index, {})
    assert resolution.method == "dst"
    assert resolution.team == "CLE"
    assert resolution.gsis_id is None
    assert resolution.linked, "a DST with a team is linked despite having no gsis_id"


def test_a_defense_with_its_abbreviation_needs_no_name_lookup(index):
    """The new export writes "Bills DST BUF" — a nickname the full-name map does
    not contain, but the abbreviation is right there and is the actual join key."""
    resolution = resolve_row("Bills", "DST", index, {}, team="BUF")
    assert resolution.method == "dst"
    assert resolution.team == "BUF"


def test_a_defense_abbreviation_is_normalized(index):
    """"Rams DST LAR" must land on LA, which is what dst_stats is keyed by."""
    assert resolve_row("Rams", "DST", index, {}, team="LAR").team == "LA"


def test_unknown_team_defense_is_unlinked(index):
    assert resolve_row("Toronto Argonauts", "DEF", index, {}).method == "unlinked"


def test_a_defense_with_a_bogus_abbreviation_is_refused(index):
    """For a defense the abbreviation IS the join key, so an unknown one must not
    pass through — that would be a linked row keyed to a team that does not
    exist, which is worse than an honest miss."""
    assert resolve_row("Argonauts", "DST", index, {}, team="TOR").method == "unlinked"


def test_defense_never_falls_through_to_the_player_index(index):
    """A team named like a player must not match a person."""
    index.by_name_position[("cleveland browns", "DST")] = {"00-0000001"}
    assert resolve_row("Cleveland Browns", "DEF", index, {}).gsis_id is None


# --- the alias file --------------------------------------------------------


def test_missing_alias_file_is_not_an_error(tmp_path):
    assert load_aliases(tmp_path / "nope.yml") == {}


def test_pending_is_regenerated_but_confirmed_is_preserved(tmp_path):
    """Confirming an entry must make it disappear from pending on the next run."""
    path = tmp_path / "adp_aliases.yml"
    write_pending(path, [("Nathaniel Dell", "WR", ())])
    assert "nathaniel dell|WR" in yaml.safe_load(path.read_text())["pending"]

    payload = yaml.safe_load(path.read_text())
    payload["confirmed"] = {"nathaniel dell|WR": {"gsis_id": TANK_DELL}}
    path.write_text(yaml.safe_dump(payload))

    write_pending(path, [])  # the next ingest resolves it, so nothing is pending
    reloaded = yaml.safe_load(path.read_text())
    assert reloaded["pending"] == {}
    assert load_aliases(path) == {"nathaniel dell|WR": TANK_DELL}


def test_pending_records_candidates_for_an_ambiguous_name(tmp_path):
    path = tmp_path / "adp_aliases.yml"
    write_pending(path, [("A.J. Brown", "WR", (AJ_BROWN, AJ_BROWN_RETIRED))])
    entry = yaml.safe_load(path.read_text())["pending"]["aj brown|WR"]
    assert entry["candidates"] == [AJ_BROWN, AJ_BROWN_RETIRED]
    assert "candidates" in entry["note"]


def test_alias_lookup_is_normalization_insensitive(tmp_path):
    """The file is hand-edited, so `A.J. Brown` and `AJ Brown` must be one key."""
    assert alias_key("A.J. Brown", "WR") == alias_key("AJ Brown", "wr")


# --- the two export layouts ------------------------------------------------
#
# Two shapes are in circulation and both must keep parsing, because
# config/market/history/ holds files in each and a board rebuilt from an older
# export should still work:
#
#   rank-only   Name,ADP,Position ADP           -> ADP *is* a dense rank
#   full        Rank,Player,Trend,Avg Pos,...   -> ADP is a real average pick


@pytest.mark.parametrize(
    "cell,expected",
    [
        ("Jahmyr Gibbs RB  DET", ("Jahmyr Gibbs", "RB", "DET")),
        ("Ja'Marr Chase WR  CIN", ("Ja'Marr Chase", "WR", "CIN")),
        ("Bills DST  BUF", ("Bills", "DST", "BUF")),
        ("Christian Kirk WR  SF", ("Christian Kirk", "WR", "SF")),
        ("Kenneth Walker III RB  SEA", ("Kenneth Walker III", "RB", "SEA")),
    ],
)
def test_the_packed_player_cell_splits(cell, expected):
    from ffdraft.ingest.sleeper import _split_player_cell

    assert _split_player_cell(cell) == expected


def test_a_bare_name_survives_the_split():
    """The older layout has a plain Name column with no trailing pair."""
    from ffdraft.ingest.sleeper import _split_player_cell

    assert _split_player_cell("Jahmyr Gibbs") == ("Jahmyr Gibbs", "", None)


def test_a_surname_that_looks_like_a_position_is_not_eaten():
    """The pattern is anchored at the end, so only a real trailing pos+team wins."""
    from ffdraft.ingest.sleeper import _split_player_cell

    assert _split_player_cell("Josh Allen QB  BUF") == ("Josh Allen", "QB", "BUF")
    assert _split_player_cell("Some Guy") == ("Some Guy", "", None)


@pytest.mark.parametrize(
    "raw,expected", [("1/3", (1.0, 3.0)), ("128/248", (128.0, 248.0)), ("", (None, None))]
)
def test_the_observed_range_parses(raw, expected):
    from ffdraft.ingest.sleeper import _parse_hi_lo

    assert _parse_hi_lo(raw) == expected


def test_trailing_legend_rows_are_not_players():
    """Exports append "Legend", "Injury", "News" rows with an empty rest-of-line.
    Parsed naively they become phantom players at the bottom of the board."""
    from ffdraft.ingest.sleeper import _header_map, _number

    columns = _header_map(["Rank", "Player", "Trend", "Avg Pos", "Hi/Lo", "Pct"])
    assert columns["adp"] == "Avg Pos", "Avg Pos must win over any other adp spelling"
    assert _number("") is None and _number(None) is None


def test_both_layouts_map_their_adp_column():
    from ffdraft.ingest.sleeper import _header_map

    full = _header_map(["Rank", "Player", "Trend", "Avg Pos", "Hi/Lo", "Pct"])
    assert full["adp"] == "Avg Pos" and full["rank"] == "Rank"

    rank_only = _header_map(["Name", "ADP", "Position ADP"])
    assert rank_only["adp"] == "ADP" and rank_only["player"] == "Name"
    assert "rank" not in rank_only, "the old layout has no separate rank column"


# --- staleness -------------------------------------------------------------
#
# The ADP file is the only input nothing can refresh automatically, so it
# silently ageing into September is the main new failure mode. It must warn, and
# it must never fail — a stale price still beats no price, as long as it is
# labelled.


def test_fresh_adp_produces_no_warning():
    from datetime import date, timedelta

    from ffdraft.ingest.sleeper import _adp_staleness_warning

    recent = date.today() - timedelta(days=2)
    assert _adp_staleness_warning({"as_of": recent, "max_age_days": 10}) == ""


def test_stale_adp_warns_with_the_age():
    from datetime import date, timedelta

    from ffdraft.ingest.sleeper import _adp_staleness_warning

    old = date.today() - timedelta(days=40)
    warning = _adp_staleness_warning({"as_of": old, "max_age_days": 10})
    assert "STALE" in warning
    assert "40 days" in warning


def test_missing_as_of_is_itself_a_warning():
    from ffdraft.ingest.sleeper import _adp_staleness_warning

    assert "no as_of" in _adp_staleness_warning({})


def test_unparseable_as_of_is_reported_not_raised():
    from ffdraft.ingest.sleeper import _adp_staleness_warning

    assert "not an ISO date" in _adp_staleness_warning({"as_of": "last tuesday"})


def test_iso_string_as_of_is_accepted():
    """PyYAML gives a date object, but a quoted value arrives as a string."""
    from datetime import date, timedelta

    from ffdraft.ingest.sleeper import _adp_staleness_warning

    recent = (date.today() - timedelta(days=1)).isoformat()
    assert _adp_staleness_warning({"as_of": recent, "max_age_days": 10}) == ""


def test_the_shipped_config_points_at_a_real_adp_file():
    """A path typo would degrade the board to ECR-only with only a skip line."""
    from ffdraft.config import load_sources, resolve

    cfg = load_sources()["sources"]["sleeper_adp_file"]
    assert resolve(cfg["path"]).is_file()
    assert cfg["as_of"], "as_of must be set; it is the date the board reports"
