"""Assemble the live-draft board from the named queries.

One board frame, built by joining the four named queries on `gsis_id`. It is
deliberately assembled from `sql/` rather than fresh SQL: metric definitions must
stay diffable, and a UI computing target share slightly differently from the
agents is exactly the drift `CLAUDE.md` warns about.

**This board is market-ordered, not projection-ordered.** It ranks by ECR and
shows usage context beside it. It does not show `proj_pts` or VOR, because this
project produces neither outside an agent run — inventing them here would mean
inventing numbers, which is worse than not having them. The "run agent analysis"
button exists for the real thing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import polars as pl

from ..config import load_sources
from ..league import League, load_league
from ..metrics.tiers import add_tiers
from ..warehouse import connect, freshness, run_named

# Columns pulled from each context query. Kept narrow: a live draft board that
# ships 40 columns per player is unreadable at speed.
_USAGE_COLUMNS = [
    "player_id",
    "games",
    "snap_pct",
    "target_share",
    "wopr",
    "rz20_per_game",
    "target_share_trend",
    "data_status",
]
_ROSTER_COLUMNS = [
    "player_id",
    "role_status",
    "contingent_role",
    "ir_eligible",
    "rookie",
    "depth_rank",
    "team_target_share",
    "team_carry_share",
]
_POE_COLUMNS = ["player_id", "poe_per_game", "read"]


@dataclass(frozen=True)
class BoardMeta:
    """What the board is standing on, shown in the UI's assumptions strip."""

    league: str
    teams: int
    scoring: str
    roster_spots: int
    rostered_players: int
    excluded_positions: list[str]
    usage_season: int
    draft_season: int
    market_source: str | None
    market_as_of: str | None
    adp_source: str | None
    adp_as_of: str | None
    warehouse_age_hours: float | None
    notes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def player_key(row: dict) -> str:
    """A stable id for a board row.

    Team defenses have no `gsis_id`, so they key on position and team. Falling
    back to the player's name would reintroduce the name-collision problem the
    warehouse spends so much effort avoiding, but a key is needed for *something*
    — so unlinked rows get an explicit `unlinked:` prefix that makes their status
    visible rather than silently blending in with real ids.
    """
    if row.get("gsis_id"):
        return str(row["gsis_id"])
    if row.get("position") == "DST" and row.get("team"):
        return f"DST:{row['team']}"
    return f"unlinked:{row.get('position')}:{row.get('player')}"


def load_board(
    league: League | None = None, max_tier_size: int = 6
) -> tuple[pl.DataFrame, BoardMeta]:
    """Build the board frame and its provenance."""
    league = league or load_league()
    sources = load_sources()
    usage_season = sources["seasons"]["history"][-1]
    draft_season = sources["seasons"]["draft_season"]
    notes: list[str] = []

    # Read-only, deliberately. The UI never writes, and DuckDB is single-writer:
    # a read-write handle held by a browser tab would lock out `/refresh` running
    # ingest in the background, which is the one thing the two must be able to do
    # at the same time.
    con = connect(sources["warehouse"]["path"], read_only=True)
    try:
        board = run_named(
            con,
            "adp_deltas",
            {
                "page_type": "redraft-overall",
                "teams": league.teams,
                "exclude_positions": list(league.excluded_positions),
            },
        )
        board = _attach(
            con, board, "usage_profile", {"season": usage_season}, _USAGE_COLUMNS, notes
        )
        board = _attach(
            con,
            board,
            "roster_context",
            {"season": usage_season, "draft_season": draft_season},
            _ROSTER_COLUMNS,
            notes,
        )
        board = _attach(
            con,
            board,
            "points_over_expected",
            {"season": usage_season, "min_games": 4},
            _POE_COLUMNS,
            notes,
        )
        ages = freshness(con)
        age = float(ages["age_hours"].max()) if ages.height else None
    finally:
        con.close()

    # Tiers over ordinals: within a position, players whose ECR gaps are small are
    # interchangeable. Negated because add_tiers ranks descending by value and a
    # lower ECR is better.
    board = board.with_columns(ecr_value=-pl.col("ecr"))
    board = add_tiers(board, "ecr_value", over="position", max_tier_size=max_tier_size)
    board = board.drop("ecr_value")

    board = board.with_columns(
        key=pl.struct(["gsis_id", "position", "team", "player"]).map_elements(
            player_key, return_dtype=pl.Utf8
        )
    )

    first = board.head(1).to_dicts()[0] if board.height else {}
    meta = BoardMeta(
        league=league.name,
        teams=league.teams,
        scoring=league.assumptions()["scoring"],
        roster_spots=league.roster_spots,
        rostered_players=league.rostered_players,
        excluded_positions=list(league.excluded_positions),
        usage_season=usage_season,
        draft_season=draft_season,
        market_source=first.get("market_source"),
        market_as_of=first.get("market_as_of"),
        adp_source=_first_non_null(board, "adp_source"),
        adp_as_of=_first_non_null(board, "adp_as_of"),
        warehouse_age_hours=age,
        notes=notes
        + [
            "Ordered by ECR (expert consensus), not by projected points — this "
            "project has no projections outside an agent run.",
            "ADP drives availability; ECR drives value. ecr_vs_adp is the gap.",
        ],
    )
    return board, meta


def _attach(
    con,
    board: pl.DataFrame,
    query: str,
    params: dict,
    columns: list[str],
    notes: list[str],
) -> pl.DataFrame:
    """Left-join a context query, tolerating its absence.

    A missing context table degrades the board rather than blanking it — during a
    draft a board with no usage column still beats no board at all, as long as it
    says which context is missing.
    """
    try:
        extra = run_named(con, query, params)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"{query} unavailable ({type(exc).__name__}); its columns are blank")
        return board

    present = [c for c in columns if c in extra.columns]
    missing = set(columns) - set(present)
    if missing:
        notes.append(f"{query} is missing {sorted(missing)}")
    if "player_id" not in present:
        notes.append(f"{query} has no player_id to join on; skipped")
        return board

    extra = extra.select(present).unique(subset=["player_id"], keep="first")
    # Prefix collisions rather than letting a suffix silently appear.
    renames = {c: c for c in present if c != "player_id"}
    for column in list(renames):
        if column in board.columns:
            renames[column] = f"{query.split('_')[0]}_{column}"
    extra = extra.rename(renames)
    return board.join(extra, left_on="gsis_id", right_on="player_id", how="left")


def _first_non_null(df: pl.DataFrame, column: str) -> str | None:
    if column not in df.columns:
        return None
    values = df[column].drop_nulls()
    return str(values[0]) if values.len() else None


# --- snake draft math ------------------------------------------------------


def picks_for_slot(slot: int, teams: int, rounds: int) -> list[int]:
    """Overall pick numbers for a slot in a snake draft.

    Odd rounds run 1..teams, even rounds reverse. From slot 5 in a 10-team league:
    5, 16, 25, 36 — the back-to-back at the turn is the whole reason slot matters.
    """
    if not 1 <= slot <= teams:
        raise ValueError(f"slot {slot} is outside 1..{teams}")
    picks = []
    for rnd in range(1, rounds + 1):
        offset = slot if rnd % 2 else teams - slot + 1
        picks.append((rnd - 1) * teams + offset)
    return picks


def survives_to(
    adp_rank: float | None,
    pick: int,
    teams: int,
    cushion: float = 0.5,
    earliest: float | None = None,
    latest: float | None = None,
) -> str:
    """A blunt read on whether a player lasts to a given pick.

    Deliberately three-valued. ADP is a central tendency and a precise
    probability would be false confidence, so this never claims more than
    likely / toss-up / gone.

    When the export publishes the observed range (`earliest`/`latest`), that is
    used in preference to a fixed cushion, because it is the real spread rather
    than a guess at one. A player never taken before pick 40 is genuinely safe at
    30 even if his average is 35; a player who has gone as early as 9 is not safe
    at 20 whatever his average says. Without a range, fall back to half a round
    either side, which is about the resolution a bare average supports.
    """
    if adp_rank is None:
        return "unknown"

    if earliest is not None and latest is not None:
        if earliest > pick:
            return "likely"  # never seen taken this early
        if latest < pick:
            return "gone"  # never seen lasting this long
        return "toss-up"

    margin = adp_rank - pick
    if margin > teams * cushion:
        return "likely"
    if margin < -teams * cushion:
        return "gone"
    return "toss-up"
