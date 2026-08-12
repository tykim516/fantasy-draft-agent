"""Link a name-keyed market file to `gsis_id` without ever guessing.

This project forbids joining on player name (`config/sources.yml`, `CLAUDE.md`,
`README.md`) because suffix and collision mismatches corrupt a board silently.
The Sleeper ADP file carries only names, so it needs a way in that does not break
that rule.

The way in is this ladder. Every link is either unambiguous or a recorded human
decision; nothing is resolved by picking the likeliest of several candidates:

    alias     a confirmed entry in adp_aliases.yml. Human decisions win.
    direct    the file carried a gsis_id or sleeper_id.
    auto      exactly one candidate for normalized name + position.
    team      several candidates, exactly one of whom plays for the team the
              export names. Direct evidence, so it outranks `activity`.
    activity  several candidates, exactly one of whom plays now.
    dst       a team defense, keyed on its abbreviation (no gsis_id exists).
    unlinked  everything else — written to the alias file as `pending`.

The activity rung is what makes this practical rather than theoretical. Twelve of
the 242 non-kicker players in the August 2026 file match several `gsis_id`s,
every one of them because a retired namesake shares the name and position: there
are two A.J. Browns, two Josh Jacobses, two Terry McLaurins. Filtering candidates
to players on a current roster or with recent stats resolves all twelve without
a judgement call, because only one of each pair is in the league.

Unlinked rows are returned, not dropped. A player silently missing from the board
is worse than one visibly flagged as unjoinable.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import yaml

# Generational suffixes carry no identity here and are inconsistently present
# across sources: nflverse has "Kenneth Walker III", exports often do not.
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

# "RB1", "DEF17", "K10" — the position label and its reported positional rank.
_POSITION_ADP_RE = re.compile(r"^\s*([A-Za-z]+)\s*(\d*)\s*$")

# The ADP file's vocabulary for a team defense; the league config says DST.
_POSITION_ALIASES = {"DEF": "DST", "D/ST": "DST", "DEFENSE": "DST", "PK": "K"}


def normalize_name(name: str) -> str:
    """Casefold, strip punctuation and suffixes, collapse whitespace.

    `Ja'Marr Chase` -> `jamarr chase`, `A.J. Brown` -> `aj brown`,
    `Kenneth Walker III` -> `kenneth walker`.
    """
    if not name:
        return ""
    text = name.lower().replace(".", "").replace("'", "").replace("’", "")
    text = text.replace("-", " ")
    text = re.sub(r"[^a-z ]", "", text)
    parts = [p for p in text.split() if p and p not in _SUFFIXES]
    return " ".join(parts)


def normalize_position(position: str) -> str:
    """Map a source's position vocabulary onto the league config's."""
    if not position:
        return ""
    upper = position.strip().upper()
    return _POSITION_ALIASES.get(upper, upper)


def parse_position_adp(value: str) -> tuple[str, int | None]:
    """Split `RB12` into `('RB', 12)`. A bare `RB` yields `('RB', None)`.

    The trailing rank is read but deliberately not trusted — in the August 2026
    export it disagrees with the overall ordering (Chase is ADP 3 / WR2 while
    Nacua is ADP 4 / WR1), so the two columns come from different computations
    upstream. Only the label is used downstream.
    """
    match = _POSITION_ADP_RE.match(value or "")
    if not match:
        return "", None
    label, digits = match.groups()
    return normalize_position(label), int(digits) if digits else None


def alias_key(name: str, position: str) -> str:
    """The stable key an alias entry is filed under."""
    return f"{normalize_name(name)}|{normalize_position(position)}"


@dataclass(frozen=True)
class Resolution:
    """How one market row was linked, and by what evidence."""

    gsis_id: str | None
    team: str | None
    method: str
    candidates: tuple[str, ...] = ()

    @property
    def linked(self) -> bool:
        # A team defense links on team abbreviation; no gsis_id exists for one.
        return self.gsis_id is not None or (self.method == "dst" and self.team is not None)

    @property
    def status(self) -> str:
        return "linked" if self.linked else "unlinked"


@dataclass
class ResolverStats:
    """Counts by method, for the summary line ingest prints."""

    counts: defaultdict = field(default_factory=lambda: defaultdict(int))
    pending: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)

    def record(self, resolution: Resolution, name: str, position: str) -> None:
        self.counts[resolution.method] += 1
        if not resolution.linked:
            self.pending.append((name, position, resolution.candidates))

    def summary(self) -> str:
        order = ("alias", "direct", "auto", "team", "activity", "dst", "unlinked")
        parts = [f"{key} {self.counts[key]}" for key in order if self.counts[key]]
        return ", ".join(parts)


# --- the candidate index ---------------------------------------------------


class Index:
    """Name+position -> gsis_id candidates, plus the sets used to break ties.

    Built from the union of `ff_playerids` and `sleeper_players`. Neither alone
    is sufficient: `sleeper_players` carries `gsis_id` for only ~32% of its rows,
    and `ff_playerids` misses players Sleeper has that nflverse has not absorbed.
    """

    def __init__(
        self,
        by_name_position: dict[tuple[str, str], set[str]],
        active: set[str],
        teams_by_name: dict[str, str],
        sleeper_to_gsis: dict[str, str],
        teams_for: dict[str, set[str]] | None = None,
        team_aliases: dict[str, str] | None = None,
    ) -> None:
        self.by_name_position = by_name_position
        self.active = active
        self.teams_by_name = teams_by_name
        self.sleeper_to_gsis = sleeper_to_gsis
        # gsis_id -> every team abbreviation he has appeared for. A set, not a
        # single value, because a player who changed teams must still match an
        # export listing either one.
        self.teams_for = teams_for or {}
        # Spelling variants -> the abbreviation this warehouse uses. Exports say
        # SF and the warehouse says SFO, or LAR against LA; a team tiebreak that
        # fails on spelling is worse than no tiebreak, because it silently falls
        # through to a weaker rung.
        self.team_aliases = team_aliases or {}

    def candidates(self, name: str, position: str) -> set[str]:
        key = (normalize_name(name), normalize_position(position))
        return self.by_name_position.get(key, set())

    def canonical_team(self, team: str | None) -> str | None:
        """Map an export's team spelling onto the warehouse's.

        An unrecognised code is returned as-is rather than rejected. It is only
        ever used to intersect against a candidate's known teams, so a wrong code
        yields an empty intersection and the ladder falls through to the next
        rung — which is the correct outcome. Rejecting it here instead would tie
        the team tiebreak to the completeness of the `teams` table, so a warehouse
        missing that table would silently stop breaking ties at all.
        """
        if not team:
            return None
        code = team.strip().upper()
        return self.team_aliases.get(code, code)

    @property
    def known_teams(self) -> set[str]:
        return set(self.teams_by_name.values())


def _table_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    from ..warehouse import table_exists

    return table_exists(con, name)


def build_index(con: duckdb.DuckDBPyConnection, recent_season: int = 2024) -> Index:
    """Read the crosswalk tables once, into memory. ~12k rows; not worth a join."""
    by_name_position: dict[tuple[str, str], set[str]] = defaultdict(set)
    sleeper_to_gsis: dict[str, str] = {}

    if _table_exists(con, "ff_playerids"):
        rows = con.execute(
            "SELECT name, position, gsis_id, sleeper_id FROM ff_playerids "
            "WHERE gsis_id IS NOT NULL"
        ).fetchall()
        for name, position, gsis_id, sleeper_id in rows:
            if name and position:
                by_name_position[(normalize_name(name), normalize_position(position))].add(gsis_id)
            if sleeper_id:
                sleeper_to_gsis.setdefault(str(sleeper_id), gsis_id)

    if _table_exists(con, "sleeper_players"):
        rows = con.execute(
            "SELECT full_name, position, gsis_id, player_id FROM sleeper_players "
            "WHERE gsis_id IS NOT NULL"
        ).fetchall()
        for name, position, gsis_id, player_id in rows:
            if name and position:
                by_name_position[(normalize_name(name), normalize_position(position))].add(gsis_id)
            if player_id:
                sleeper_to_gsis.setdefault(str(player_id), gsis_id)

    # "Plays now" — a current roster spot, or stats in a recent season. Used only
    # to break ties between namesakes, never to admit or reject a lone candidate.
    active: set[str] = set()
    if _table_exists(con, "rosters"):
        active |= {
            row[0]
            for row in con.execute(
                "SELECT DISTINCT gsis_id FROM rosters WHERE gsis_id IS NOT NULL"
            ).fetchall()
        }
    if _table_exists(con, "player_stats"):
        active |= {
            row[0]
            for row in con.execute(
                "SELECT DISTINCT player_id FROM player_stats WHERE season >= ?", [recent_season]
            ).fetchall()
        }

    # The teams table carries historical franchises, and one name maps to two
    # abbreviations: the Rams are both LA and LAR. The warehouse uses LA
    # everywhere (rosters, team_stats, dst_stats), so prefer whichever code the
    # data actually uses — picking the wrong one gives a DST a team code that
    # joins to nothing downstream.
    in_use: set[str] = set()
    if _table_exists(con, "rosters"):
        in_use = {
            row[0]
            for row in con.execute(
                "SELECT DISTINCT team FROM rosters WHERE team IS NOT NULL"
            ).fetchall()
        }

    teams_by_name: dict[str, str] = {}
    if _table_exists(con, "teams"):
        for name, abbr in con.execute("SELECT team_name, team_abbr FROM teams").fetchall():
            if not name or not abbr:
                continue
            key = normalize_name(name)
            if key not in teams_by_name or (abbr in in_use and teams_by_name[key] not in in_use):
                teams_by_name[key] = abbr

    # Which teams each player has appeared for, used to break namesake ties on
    # direct evidence rather than on the activity heuristic.
    teams_for: dict[str, set[str]] = defaultdict(set)
    if _table_exists(con, "rosters"):
        for gsis_id, team in con.execute(
            "SELECT gsis_id, team FROM rosters WHERE gsis_id IS NOT NULL AND team IS NOT NULL"
        ).fetchall():
            teams_for[gsis_id].add(team)
    if _table_exists(con, "player_stats"):
        for gsis_id, team in con.execute(
            "SELECT DISTINCT player_id, team FROM player_stats "
            "WHERE player_id IS NOT NULL AND team IS NOT NULL AND season >= ?",
            [recent_season],
        ).fetchall():
            teams_for[gsis_id].add(team)

    team_aliases = _team_aliases(set(teams_by_name.values()))
    return Index(
        by_name_position, active, teams_by_name, sleeper_to_gsis, dict(teams_for), team_aliases
    )


# Spellings an export may use that this warehouse does not. Only pairs where both
# sides are unambiguous; anything requiring a judgement call is left to fail
# loudly rather than be guessed at.
_TEAM_SPELLINGS = {
    "SF": "SFO", "SFO": "SF",
    "KC": "KCC", "KCC": "KC",
    "TB": "TBB", "TBB": "TB",
    "GB": "GBP", "GBP": "GB",
    "NO": "NOS", "NOS": "NO",
    "NE": "NEP", "NEP": "NE",
    "LV": "LVR", "LVR": "LV",
    "JAX": "JAC", "JAC": "JAX",
    "LA": "LAR", "LAR": "LA",
    "WAS": "WSH", "WSH": "WAS",
    "ARI": "ARZ", "ARZ": "ARI",
    "BAL": "BLT", "BLT": "BAL",
    "CLE": "CLV", "CLV": "CLE",
    "HOU": "HST", "HST": "HOU",
}


def _team_aliases(known: set[str]) -> dict[str, str]:
    """Build variant -> warehouse-spelling for the teams this warehouse knows."""
    aliases = {}
    for variant, canonical in _TEAM_SPELLINGS.items():
        if canonical in known and variant not in known:
            aliases[variant] = canonical
    return aliases


# --- the alias map ---------------------------------------------------------


def load_aliases(path: Path) -> dict[str, str]:
    """Read confirmed name+position -> gsis_id decisions. Missing file is fine."""
    if not path.is_file():
        return {}
    payload = yaml.safe_load(path.read_text()) or {}
    confirmed = payload.get("confirmed") or {}
    out: dict[str, str] = {}
    for key, value in confirmed.items():
        gsis_id = value.get("gsis_id") if isinstance(value, dict) else value
        if gsis_id:
            out[str(key)] = str(gsis_id)
    return out


def write_pending(path: Path, pending: list[tuple[str, str, tuple[str, ...]]]) -> None:
    """Rewrite the `pending` block, preserving `confirmed` verbatim.

    `pending` is derived, so it is regenerated every run — which means confirming
    an entry (moving it into `confirmed`) makes it disappear from `pending` on
    the next ingest. The file cleans itself up as decisions get made.
    """
    payload = {}
    if path.is_file():
        payload = yaml.safe_load(path.read_text()) or {}
    payload.setdefault("confirmed", {})

    payload["pending"] = {
        alias_key(name, position): {
            "player": name,
            "position": position,
            "candidates": list(candidates),
            "note": (
                f"{len(candidates)} candidates, none uniquely active — pick one"
                if candidates
                else "no candidate found — look up the gsis_id and add it below"
            ),
        }
        for name, position, candidates in pending
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_ALIAS_HEADER + yaml.safe_dump(payload, sort_keys=True, allow_unicode=True))


_ALIAS_HEADER = """\
# Reviewed name -> gsis_id decisions for the Sleeper ADP file.
#
# This project forbids joining on player name. This file is how a name-keyed
# market file is admitted anyway: every entry here is a human decision, recorded
# and diffable, rather than a fuzzy match made silently at runtime.
#
# `pending` is REGENERATED on every ingest — do not edit it. To resolve an entry,
# move it into `confirmed` with the right gsis_id:
#
#   confirmed:
#     nathaniel dell|WR:
#       gsis_id: 00-0038996
#       note: nflverse lists him as Tank Dell
#
# It then vanishes from `pending` on the next run. Players left pending are still
# returned on the board, flagged `unlinked` — never dropped — but they carry no
# usage or projection data, because nothing can be joined to them.
"""


# --- the ladder ------------------------------------------------------------


def resolve_row(
    name: str,
    position: str,
    index: Index,
    aliases: dict[str, str],
    team: str | None = None,
    file_gsis_id: str | None = None,
    file_sleeper_id: str | None = None,
) -> Resolution:
    """Apply the resolution ladder to one market row.

    `team` is optional because not every export carries one. When it is present
    it is used to break namesake ties, which is stronger evidence than the
    activity heuristic below and needs no assumption about who is still playing.
    """
    position = normalize_position(position)

    # 1. A recorded human decision beats every heuristic below it.
    alias = aliases.get(alias_key(name, position))
    if alias:
        return Resolution(alias, None, "alias")

    # 2. A team defense has no gsis_id; it keys on the team abbreviation, which
    #    is how dst_stats and every other team-level table is keyed. An export
    #    that already carries the abbreviation ("Bills DST BUF") needs no lookup
    #    at all; otherwise fall back to matching the full team name.
    if position == "DST":
        # Validated against the real team list here, unlike the player tiebreak
        # above: for a defense the abbreviation IS the join key, so an unknown
        # code would not fall through harmlessly — it would produce a linked row
        # keyed to a team that does not exist.
        abbr = index.canonical_team(team)
        if abbr not in index.known_teams:
            abbr = index.teams_by_name.get(normalize_name(name))
        return Resolution(None, abbr, "dst" if abbr else "unlinked")

    # 3. Ids in the file, if a richer export ever supplies them.
    if file_gsis_id:
        return Resolution(str(file_gsis_id), None, "direct")
    if file_sleeper_id:
        mapped = index.sleeper_to_gsis.get(str(file_sleeper_id))
        if mapped:
            return Resolution(mapped, None, "direct")

    candidates = index.candidates(name, position)

    # 4. Exactly one candidate: unambiguous, no judgement involved.
    if len(candidates) == 1:
        return Resolution(next(iter(candidates)), team, "auto")

    if len(candidates) > 1:
        # 5. Several candidates, but the export says which team. That is direct
        #    evidence rather than an inference, so it outranks the activity
        #    heuristic below.
        abbr = index.canonical_team(team) if team else None
        if abbr:
            on_team = {g for g in candidates if abbr in index.teams_for.get(g, set())}
            if len(on_team) == 1:
                return Resolution(next(iter(on_team)), team, "team")

        # 6. No team, or it did not decide: keep only candidates who are in the
        #    league today. The others are retired namesakes. Still not a
        #    judgement call — a unique survivor or nothing.
        playing = {gsis_id for gsis_id in candidates if gsis_id in index.active}
        if len(playing) == 1:
            return Resolution(next(iter(playing)), team, "activity")
        return Resolution(None, team, "unlinked", tuple(sorted(candidates)))

    return Resolution(None, team, "unlinked", ())
