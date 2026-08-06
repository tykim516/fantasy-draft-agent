"""Warehouse writes, freshness bookkeeping, and named-query binding.

Every test here builds its own DuckDB file in tmp_path, so none of it depends on
an ingest having been run.
"""

from __future__ import annotations

import sys

import polars as pl
import pytest

from ffdraft.warehouse import (
    META_TABLE,
    bind_params,
    connect,
    freshness,
    named_query,
    record_ingest,
    referenced_params,
    replace_table,
    row_count,
    stale_tables,
    table_exists,
    upsert_table,
)


@pytest.fixture
def con(tmp_path):
    connection = connect(tmp_path / "test.duckdb")
    yield connection
    connection.close()


def frame(weeks, season=2025, player="A", value=1.0) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "player_id": [player] * len(weeks),
            "season": [season] * len(weeks),
            "week": list(weeks),
            "value": [value] * len(weeks),
        }
    )


# --- idempotency -----------------------------------------------------------


def test_replace_table_is_idempotent(con):
    for _ in range(3):
        rows = replace_table(con, "t", frame([1, 2, 3]), "test")
        assert rows == 3
    assert row_count(con, "t") == 3


def test_replace_table_swaps_content(con):
    replace_table(con, "t", frame([1, 2, 3]), "test")
    replace_table(con, "t", frame([9]), "test")
    assert row_count(con, "t") == 1


def test_upsert_reingesting_the_same_weeks_is_a_noop(con):
    replace_table(con, "t", frame([1, 2, 3]), "test")
    for _ in range(3):
        upsert_table(con, "t", frame([2, 3]), "test", ["player_id", "season", "week"])
    assert row_count(con, "t") == 3


def test_upsert_adds_new_weeks_and_updates_old(con):
    replace_table(con, "t", frame([1, 2], value=1.0), "test")
    upsert_table(con, "t", frame([2, 3], value=99.0), "test", ["player_id", "season", "week"])
    rows = con.execute('SELECT week, value FROM "t" ORDER BY week').fetchall()
    assert rows == [(1, 1.0), (2, 99.0), (3, 99.0)]


def test_upsert_creates_the_table_when_absent(con):
    assert upsert_table(con, "fresh", frame([1]), "test", ["player_id", "season", "week"]) == 1


def test_upsert_requires_its_key_columns(con):
    replace_table(con, "t", frame([1]), "test")
    with pytest.raises(ValueError, match="lacks key column"):
        upsert_table(con, "t", pl.DataFrame({"other": [1]}), "test", ["player_id"])


# --- freshness -------------------------------------------------------------


def test_every_write_records_freshness(con):
    replace_table(con, "t", frame([1, 2]), "nflverse")
    meta = freshness(con)
    assert meta.height == 1
    row = meta.to_dicts()[0]
    assert row["table_name"] == "t"
    assert row["row_count"] == 2
    assert row["source"] == "nflverse"
    assert row["age_hours"] < 1


def test_freshness_keeps_one_row_per_table(con):
    for _ in range(3):
        replace_table(con, "t", frame([1]), "nflverse")
    assert freshness(con).height == 1


def test_freshness_on_empty_warehouse_is_empty_not_an_error(con):
    assert freshness(con).is_empty()


def test_stale_tables_detects_age(con):
    record_ingest(con, "old", 1, "test")
    con.execute(
        f"UPDATE {META_TABLE} SET loaded_at = now() - INTERVAL 48 HOUR WHERE table_name = 'old'"
    )
    assert stale_tables(con, max_age_hours=24) == ["old"]
    assert stale_tables(con, max_age_hours=72) == []


def test_table_exists(con):
    assert not table_exists(con, "nope")
    replace_table(con, "yes", frame([1]), "test")
    assert table_exists(con, "yes")


def test_row_count_of_absent_table_is_zero(con):
    assert row_count(con, "nope") == 0


# --- named queries ---------------------------------------------------------


def test_named_query_reads_from_sql_dir():
    assert "usage_profile" in named_query("usage_profile")
    assert named_query("usage_profile.sql") == named_query("usage_profile")


def test_unknown_named_query_lists_what_is_available():
    with pytest.raises(FileNotFoundError, match="available"):
        named_query("does_not_exist")


def test_referenced_params_ignores_comments():
    sql = "-- mentions $ghost in a comment\nSELECT * FROM t WHERE season = $season"
    assert referenced_params(sql) == {"season"}


def test_bind_params_narrows_to_what_the_query_uses():
    """DuckDB rejects extra bindings, so one dict must serve every query."""
    sql = "SELECT $season"
    assert bind_params(sql, {"season": 2025, "teams": 10, "unused": 1}) == {"season": 2025}


def test_bind_params_raises_on_a_missing_parameter():
    with pytest.raises(ValueError, match="needs parameter"):
        bind_params("SELECT $season", {"teams": 10})


def test_query_runner_supplies_every_parameter_the_queries_need():
    """The runner and the SQL files must not drift apart.

    Adding a `$param` to a named query without teaching scripts/query.py to
    supply it would fail only at call time, inside an agent, with a message the
    agent cannot act on.
    """
    import argparse

    from ffdraft.config import SQL_DIR

    sys.path.insert(0, str(SQL_DIR.parent / "scripts"))
    import query as query_script

    supplied = set(
        query_script.build_params(
            argparse.Namespace(
                league="main", season=None, draft_season=None, min_games=4,
                teams=None, page_type="redraft-overall", exclude_positions=None,
            )
        )
    )

    files = sorted(SQL_DIR.glob("*.sql"))
    assert files, "expected named queries in sql/"
    for path in files:
        needed = referenced_params(path.read_text())
        missing = needed - supplied
        assert not missing, f"{path.name} needs {sorted(missing)}, which query.py does not supply"
