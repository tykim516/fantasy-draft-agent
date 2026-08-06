"""DuckDB connection handling, idempotent writes, and freshness bookkeeping.

DuckDB is single-writer. An interactive session holding a read connection will
block a concurrent ingest, so a scheduled ingest should run under
`staged_build()`, which writes a separate file and atomically renames it over
the live warehouse when it finishes.

Every write records one row in `meta_ingest`, which is how agents check whether
the data behind a board is current before they use it.
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import polars as pl

from .config import load_sources, resolve

META_TABLE = "meta_ingest"

_META_DDL = f"""
CREATE TABLE IF NOT EXISTS {META_TABLE} (
    table_name VARCHAR PRIMARY KEY,
    row_count  BIGINT,
    source     VARCHAR,
    loaded_at  TIMESTAMPTZ
)
"""


def warehouse_path() -> Path:
    return resolve(load_sources()["warehouse"]["path"])


def swap_path() -> Path:
    return resolve(load_sources()["warehouse"]["swap_path"])


def connect(path: str | Path | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the warehouse, creating the parent directory and meta table as needed."""
    target = Path(path) if path is not None else warehouse_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if read_only and not target.exists():
        raise FileNotFoundError(
            f"no warehouse at {target}. Run `python scripts/ingest.py` to build it."
        )
    con = duckdb.connect(str(target), read_only=read_only)
    if not read_only:
        con.execute(_META_DDL)
    return con


def _sanitize(df: pl.DataFrame) -> pl.DataFrame:
    """Make a Polars frame safe to hand to DuckDB.

    Object and all-null columns have no DuckDB equivalent; everything else
    (including List and Struct columns) round-trips through Arrow fine.
    """
    casts = [
        pl.col(name).cast(pl.Utf8).alias(name)
        for name, dtype in df.schema.items()
        if dtype in (pl.Object, pl.Null)
    ]
    return df.with_columns(casts) if casts else df


def table_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    rows = con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name]
    ).fetchall()
    return bool(rows)


def row_count(con: duckdb.DuckDBPyConnection, name: str) -> int:
    if not table_exists(con, name):
        return 0
    return int(con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0])


def record_ingest(
    con: duckdb.DuckDBPyConnection, table_name: str, rows: int, source: str
) -> None:
    """One row per table, overwritten on each load."""
    con.execute(_META_DDL)
    con.execute(
        f"""
        INSERT INTO {META_TABLE} (table_name, row_count, source, loaded_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (table_name) DO UPDATE SET
            row_count = excluded.row_count,
            source    = excluded.source,
            loaded_at = excluded.loaded_at
        """,
        [table_name, int(rows), source, datetime.now(UTC)],
    )


def replace_table(
    con: duckdb.DuckDBPyConnection, name: str, df: pl.DataFrame, source: str
) -> int:
    """Full reload. Idempotent by construction — rerunning yields the same table."""
    con.register("_incoming", _sanitize(df))
    try:
        con.execute(f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM _incoming')
    finally:
        con.unregister("_incoming")
    count = row_count(con, name)
    record_ingest(con, name, count, source)
    return count


def upsert_table(
    con: duckdb.DuckDBPyConnection,
    name: str,
    df: pl.DataFrame,
    source: str,
    keys: Sequence[str],
) -> int:
    """Incremental load: delete the incoming key tuples, then insert them.

    Delete-then-insert rather than a merge so that a re-ingest of the same weeks
    is a no-op on row count, which is what makes the operation idempotent.
    """
    if not table_exists(con, name):
        return replace_table(con, name, df, source)

    missing = [k for k in keys if k not in df.columns]
    if missing:
        raise ValueError(f"cannot upsert {name}: incoming frame lacks key column(s) {missing}")

    frame = _sanitize(df)
    con.register("_incoming", frame)
    try:
        predicate = " AND ".join(f't."{k}" = i."{k}"' for k in keys)
        con.execute(
            f'DELETE FROM "{name}" AS t WHERE EXISTS '
            f"(SELECT 1 FROM _incoming AS i WHERE {predicate})"
        )
        # Insert by explicit column list so a source that reorders or adds
        # columns fails loudly instead of shifting values into wrong columns.
        existing = [d[0] for d in con.execute(f'SELECT * FROM "{name}" LIMIT 0').description]
        columns = ", ".join(f'"{c}"' for c in existing)
        con.execute(f'INSERT INTO "{name}" ({columns}) SELECT {columns} FROM _incoming')
    finally:
        con.unregister("_incoming")

    count = row_count(con, name)
    record_ingest(con, name, count, source)
    return count


def freshness(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """The `meta_ingest` table plus an age column, newest first."""
    if not table_exists(con, META_TABLE):
        return pl.DataFrame(
            schema={
                "table_name": pl.Utf8,
                "row_count": pl.Int64,
                "source": pl.Utf8,
                "loaded_at": pl.Datetime(time_zone="UTC"),
                "age_hours": pl.Float64,
            }
        )
    return con.execute(
        f"""
        SELECT table_name,
               row_count,
               source,
               loaded_at,
               round(date_diff('minute', loaded_at, now()) / 60.0, 2) AS age_hours
        FROM {META_TABLE}
        ORDER BY loaded_at DESC
        """
    ).pl()


def named_query(name: str) -> str:
    """Read a query from `sql/`.

    Metric SQL lives in files, never inline in an agent's shell command. Two
    invocations that each compose their own target-share query will disagree
    slightly and nobody will notice; a file is diffable.
    """
    from .config import SQL_DIR

    path = SQL_DIR / (name if name.endswith(".sql") else f"{name}.sql")
    if not path.is_file():
        available = sorted(p.stem for p in SQL_DIR.glob("*.sql"))
        raise FileNotFoundError(f"no named query {name!r} in {SQL_DIR}; available: {available}")
    return path.read_text()


_PARAM_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def referenced_params(sql: str) -> set[str]:
    """Named `$params` a statement actually binds, ignoring ones named in comments."""
    return set(_PARAM_RE.findall(re.sub(r"--[^\n]*", "", sql)))


def bind_params(sql: str, params: dict) -> dict:
    """Narrow a parameter dict to what this statement references.

    DuckDB rejects extra bindings rather than ignoring them, so callers would
    otherwise need to know each query's exact signature. Filtering here means one
    parameter dict serves every named query, and adding a parameter to a query
    does not mean editing every caller.
    """
    wanted = referenced_params(sql)
    missing = wanted - params.keys()
    if missing:
        raise ValueError(f"query needs parameter(s) {sorted(missing)}; got {sorted(params)}")
    return {k: v for k, v in params.items() if k in wanted}


def run_named(
    con: duckdb.DuckDBPyConnection, name: str, params: dict | None = None
) -> pl.DataFrame:
    """Execute a query from `sql/` with named `$param` bindings."""
    sql = named_query(name)
    return con.execute(sql, bind_params(sql, params or {})).pl()


def stale_tables(con: duckdb.DuckDBPyConnection, max_age_hours: float) -> list[str]:
    df = freshness(con)
    if df.is_empty():
        return []
    return df.filter(pl.col("age_hours") > max_age_hours)["table_name"].to_list()


@contextlib.contextmanager
def staged_build() -> Iterator[duckdb.DuckDBPyConnection]:
    """Build into a temp file, then atomically swap it over the live warehouse.

    Use this for scheduled ingests. A session with the live warehouse open keeps
    reading the old file until the rename lands, so there is no lock contention
    and no window where the warehouse is half-written.
    """
    live = warehouse_path()
    staging = swap_path()
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.unlink(missing_ok=True)

    con = connect(staging)
    try:
        yield con
    except BaseException:
        con.close()
        staging.unlink(missing_ok=True)
        raise
    con.close()

    check = duckdb.connect(str(staging), read_only=True)
    try:
        ingested = row_count(check, META_TABLE)
    finally:
        check.close()
    if ingested == 0:
        staging.unlink(missing_ok=True)
        raise RuntimeError("refusing to swap in a staged warehouse that ingested nothing")

    os.replace(staging, live)
