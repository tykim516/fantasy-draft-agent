"""nflverse loaders — the backbone of the warehouse.

Uses `nflreadpy`, which returns Polars frames. `nfl_data_py` is deprecated and
unmaintained; nothing here should be ported to it.

Which tables load, from which loader, over which seasons, and with which kwargs
all come from `config/sources.yml`. This module is the executor, not the
catalogue, so pinning a different season set never means editing Python.

Two tables are derived rather than loaded:

* `pbp_redzone` — per player-week red-zone and inside-the-10 touches. There is
  no free table for this, so it is aggregated from play-by-play.
* `dst_stats` — per team-week defensive scoring inputs, including points and
  yards allowed, which live on the *opponent's* row and in the schedule.
"""

from __future__ import annotations

import duckdb
import nflreadpy as nfl
import polars as pl

from ..config import season_set
from . import IngestResult

SOURCE = "nflverse"

# Incremental loads key on this tuple where the table supports it.
INCREMENTAL_KEYS = ("player_id", "season", "week")


def _load_one(table: str, cfg: dict, sources: dict) -> tuple[pl.DataFrame, str]:
    """Load one table, returning (frame, detail).

    nflreadpy rejects an entire call if any single requested season is not yet
    published upstream — which happens every preseason, when the draft season
    has rosters but no injuries or snap counts. Rather than lose the whole
    table, retry season by season and keep what exists, reporting which seasons
    were dropped so the gap is visible instead of silent.
    """
    loader = getattr(nfl, cfg["loader"], None)
    if loader is None:
        raise AttributeError(
            f"nflreadpy has no loader {cfg['loader']!r} for table {table!r}; "
            f"config/sources.yml is out of date with the installed version"
        )
    kwargs = dict(cfg.get("args") or {})
    seasons = season_set(sources, cfg["seasons"])
    if not seasons:
        return loader(**kwargs), ""

    try:
        return loader(seasons=seasons, **kwargs), ""
    except Exception as whole_call_error:  # noqa: BLE001
        frames, kept, dropped = [], [], []
        for season in seasons:
            try:
                frames.append(loader(seasons=[season], **kwargs))
                kept.append(season)
            except Exception:  # noqa: BLE001, PERF203
                dropped.append(season)
        if not frames:
            raise whole_call_error
        return (
            pl.concat(frames, how="diagonal_relaxed"),
            f"seasons {kept} loaded; {dropped} not yet published upstream",
        )


def ingest_nflverse(
    con: duckdb.DuckDBPyConnection,
    sources: dict,
    tables: list[str] | None = None,
    mode: str = "replace",
) -> list[IngestResult]:
    from ..warehouse import replace_table, upsert_table

    catalogue: dict[str, dict] = sources["sources"]["nflverse"]["tables"]
    wanted = tables or list(catalogue)
    results: list[IngestResult] = []

    for table in wanted:
        cfg = catalogue.get(table)
        if cfg is None:
            results.append(
                IngestResult(table, SOURCE, "failed", detail="not declared in sources.yml")
            )
            continue
        try:
            df, detail = _load_one(table, cfg, sources)
        except Exception as exc:  # noqa: BLE001 — one bad table must not kill the run
            results.append(
                IngestResult(table, SOURCE, "failed", detail=f"{type(exc).__name__}: {exc}")
            )
            continue

        if df.is_empty():
            # Expected in preseason: the draft season has no snap counts yet.
            # Not an error, but the board must know the table is absent.
            results.append(
                IngestResult(table, SOURCE, "skipped", detail="source returned 0 rows")
            )
            continue

        keys = [k for k in INCREMENTAL_KEYS if k in df.columns]
        if mode == "upsert" and len(keys) == len(INCREMENTAL_KEYS):
            rows = upsert_table(con, table, df, SOURCE, keys)
        else:
            rows = replace_table(con, table, df, SOURCE)
        results.append(IngestResult(table, SOURCE, "loaded", rows=rows, detail=detail))

    return results


# --- derived: red-zone usage ----------------------------------------------

_RZ_COLUMNS = [
    "season",
    "week",
    "posteam",
    "yardline_100",
    "rush_attempt",
    "pass_attempt",
    "rusher_player_id",
    "receiver_player_id",
    "td_player_id",
]


def build_redzone(con: duckdb.DuckDBPyConnection, sources: dict) -> IngestResult:
    """Aggregate play-by-play into per player-week red-zone touches.

    A "touch" is a carry or a target — targets rather than receptions, because
    the point of the metric is opportunity, not conversion. Player ids in
    play-by-play are gsis ids, so this joins directly to `player_stats`.
    """
    from ..warehouse import replace_table

    seasons = season_set(sources, "history")
    try:
        pbp = nfl.load_pbp(seasons=seasons).select(_RZ_COLUMNS)
    except Exception as exc:  # noqa: BLE001
        return IngestResult("pbp_redzone", SOURCE, "failed", detail=f"{type(exc).__name__}: {exc}")

    pbp = pbp.filter(pl.col("yardline_100").is_not_null())

    carries = (
        pbp.filter((pl.col("rush_attempt") == 1) & pl.col("rusher_player_id").is_not_null())
        .select(
            "season",
            "week",
            pl.col("posteam").alias("team"),
            pl.col("rusher_player_id").alias("player_id"),
            "yardline_100",
            pl.lit("carry").alias("touch_type"),
            (pl.col("td_player_id") == pl.col("rusher_player_id")).alias("scored"),
        )
    )
    targets = (
        pbp.filter((pl.col("pass_attempt") == 1) & pl.col("receiver_player_id").is_not_null())
        .select(
            "season",
            "week",
            pl.col("posteam").alias("team"),
            pl.col("receiver_player_id").alias("player_id"),
            "yardline_100",
            pl.lit("target").alias("touch_type"),
            (pl.col("td_player_id") == pl.col("receiver_player_id")).alias("scored"),
        )
    )

    touches = pl.concat([carries, targets])
    inside_20 = pl.col("yardline_100") <= 20
    inside_10 = pl.col("yardline_100") <= 10
    is_carry = pl.col("touch_type") == "carry"

    agg = (
        touches.group_by("player_id", "season", "week", "team")
        .agg(
            rz20_carries=(inside_20 & is_carry).sum(),
            rz20_targets=(inside_20 & ~is_carry).sum(),
            rz10_carries=(inside_10 & is_carry).sum(),
            rz10_targets=(inside_10 & ~is_carry).sum(),
            rz20_touchdowns=(inside_20 & pl.col("scored").fill_null(False)).sum(),
        )
        .with_columns(
            rz20_touches=pl.col("rz20_carries") + pl.col("rz20_targets"),
            rz10_touches=pl.col("rz10_carries") + pl.col("rz10_targets"),
        )
        .filter(pl.col("rz20_touches") > 0)
        .sort("season", "week", "player_id")
    )

    rows = replace_table(con, "pbp_redzone", agg, f"{SOURCE} (derived from load_pbp)")
    return IngestResult("pbp_redzone", SOURCE, "loaded", rows=rows)


# --- derived: DST scoring inputs ------------------------------------------

# Points allowed comes from the schedule, not from team_stats. Yards allowed is
# the opponent's net offence. Blocked kicks credited to a defense are the kicks
# the *opponent* had blocked. Each of those is a join the scoring code should
# never have to know about, so it is resolved here once.
#
# Two column semantics verified against the real 2024 table rather than assumed:
#
#   * `sack_yards_lost` is stored NEGATIVE (2024 mean -15.9), so net offence
#     ADDS it. Subtracting credited the offence with its own sack yardage and
#     inflated league-average yards allowed to 369/game against a true 337.
#   * `def_fumbles` is not fumble recoveries — it averages 0.11 per team-game,
#     far too low. `fumble_recovery_opp` (0.49) is recoveries of the opponent's
#     fumbles, which is what a DST actually scores for.
_DST_SQL = """
CREATE OR REPLACE TABLE dst_stats AS
WITH game_scores AS (
    SELECT game_id, season, week, home_team AS team, away_score AS points_allowed FROM schedules
    UNION ALL
    SELECT game_id, season, week, away_team AS team, home_score AS points_allowed FROM schedules
),
offense AS (
    SELECT season, week, team,
           coalesce(passing_yards, 0) + coalesce(sack_yards_lost, 0)
             + coalesce(rushing_yards, 0)          AS net_yards,
           coalesce(fg_blocked, 0) + coalesce(pat_blocked, 0)
             + coalesce(pt_blocked, 0)             AS kicks_blocked_against
    FROM team_stats
    WHERE season_type = 'REG'
)
SELECT
    t.team,
    t.season,
    t.week,
    t.opponent_team,
    coalesce(t.def_sacks, 0)             AS sacks,
    coalesce(t.def_interceptions, 0)     AS interceptions,
    coalesce(t.fumble_recovery_opp, 0)   AS fumble_recoveries,
    coalesce(t.def_fumbles_forced, 0)    AS forced_fumbles,
    coalesce(t.def_safeties, 0)          AS safeties,
    coalesce(t.def_tds, 0)               AS def_tds,
    coalesce(t.special_teams_tds, 0)     AS st_tds,
    coalesce(o.kicks_blocked_against, 0) AS blocked_kicks,
    s.points_allowed,
    o.net_yards                          AS yards_allowed
FROM team_stats t
LEFT JOIN game_scores s
       ON s.team = t.team AND s.season = t.season AND s.week = t.week
LEFT JOIN offense o
       ON o.team = t.opponent_team AND o.season = t.season AND o.week = t.week
WHERE t.season_type = 'REG'
"""


# `depth_charts` is a snapshot table, not a season table: it carries every
# scrape since March (138 of them as of this build), and the `seasons` argument
# does not filter it. Pinning the latest snapshot once, here, stops two agents
# from reading two different depth charts and quietly disagreeing.
_DEPTH_CHART_SQL = """
CREATE OR REPLACE TABLE depth_chart_current AS
SELECT * FROM depth_charts
WHERE dt = (SELECT max(dt) FROM depth_charts)
"""


def build_depth_chart_current(con: duckdb.DuckDBPyConnection) -> IngestResult:
    """Pin the most recent depth-chart snapshot as its own table."""
    from ..warehouse import record_ingest, row_count, table_exists

    if not table_exists(con, "depth_charts"):
        return IngestResult(
            "depth_chart_current", SOURCE, "skipped", detail="requires depth_charts"
        )
    con.execute(_DEPTH_CHART_SQL)
    rows = row_count(con, "depth_chart_current")
    as_of = con.execute("SELECT max(dt) FROM depth_chart_current").fetchone()[0]
    record_ingest(con, "depth_chart_current", rows, f"{SOURCE} (snapshot {as_of})")
    return IngestResult("depth_chart_current", SOURCE, "loaded", rows=rows, detail=f"as of {as_of}")


def build_dst_stats(con: duckdb.DuckDBPyConnection) -> IngestResult:
    """Assemble per team-week defensive scoring inputs from team_stats + schedules."""
    from ..warehouse import record_ingest, row_count, table_exists

    for required in ("team_stats", "schedules"):
        if not table_exists(con, required):
            return IngestResult(
                "dst_stats", SOURCE, "skipped", detail=f"requires {required}, which is not loaded"
            )
    try:
        con.execute(_DST_SQL)
    except Exception as exc:  # noqa: BLE001
        return IngestResult("dst_stats", SOURCE, "failed", detail=f"{type(exc).__name__}: {exc}")

    rows = row_count(con, "dst_stats")
    record_ingest(con, "dst_stats", rows, f"{SOURCE} (derived from team_stats + schedules)")
    return IngestResult("dst_stats", SOURCE, "loaded", rows=rows)
