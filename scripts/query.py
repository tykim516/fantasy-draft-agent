#!/usr/bin/env python
"""Run a named query from `sql/` against the warehouse.

    python scripts/query.py --list
    python scripts/query.py usage_profile --season 2025 --limit 20
    python scripts/query.py points_over_expected --season 2025 --min-games 8
    python scripts/query.py adp_deltas --league main --limit 30
    python scripts/query.py roster_context --season 2025 --limit 20 --csv

Agents call queries by filename so that a metric has exactly one definition. If
an agent composes its own SQL each run, two invocations will compute target
share slightly differently and nobody will notice.

League-dependent parameters (`--teams`, `--exclude-positions`) default from the
league config rather than from a hardcoded 12-team assumption.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import polars as pl  # noqa: E402

from ffdraft.config import SQL_DIR, load_sources  # noqa: E402
from ffdraft.league import load_league  # noqa: E402
from ffdraft.warehouse import connect, named_query, run_named  # noqa: E402


def available() -> list[str]:
    return sorted(p.stem for p in SQL_DIR.glob("*.sql"))


def build_params(args: argparse.Namespace) -> dict:
    """Supply every parameter any query might need.

    `warehouse.run_named` narrows this to what each statement actually
    references, so one parameter dictionary serves all of them and adding a
    parameter to a query does not mean editing this script.
    """
    league = load_league(args.league)
    sources = load_sources()
    history = sources["seasons"]["history"]

    return {
        "season": args.season or history[-1],
        "draft_season": args.draft_season or sources["seasons"]["draft_season"],
        "min_games": args.min_games,
        "teams": args.teams or league.teams,
        "page_type": args.page_type,
        "exclude_positions": (
            args.exclude_positions.split(",")
            if args.exclude_positions
            else list(league.excluded_positions)
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("query", nargs="?", help="named query, without the .sql")
    parser.add_argument("--list", action="store_true", help="list available queries")
    parser.add_argument("--show", action="store_true", help="print the SQL instead of running it")
    parser.add_argument("--league", default="main")
    parser.add_argument("--season", type=int, help="defaults to the newest pinned history season")
    parser.add_argument("--draft-season", type=int)
    parser.add_argument("--min-games", type=int, default=4)
    parser.add_argument("--teams", type=int, help="defaults to the league config")
    parser.add_argument("--page-type", default="redraft-overall")
    parser.add_argument(
        "--exclude-positions", help="comma separated; defaults to the league config"
    )
    parser.add_argument("--limit", type=int, help="print only the first N rows")
    parser.add_argument("--csv", action="store_true", help="emit CSV instead of a table")
    parser.add_argument("--columns", help="comma separated subset of columns to print")
    args = parser.parse_args(argv)

    if args.list or not args.query:
        print("named queries in sql/:")
        for name in available():
            print(f"  {name}")
        return 0 if args.list else 1

    if args.show:
        print(named_query(args.query))
        return 0

    params = build_params(args)
    con = connect(read_only=True)
    try:
        df: pl.DataFrame = run_named(con, args.query, params)
    finally:
        con.close()

    if args.columns:
        wanted = [c.strip() for c in args.columns.split(",")]
        missing = [c for c in wanted if c not in df.columns]
        if missing:
            print(f"no such column(s): {missing}\navailable: {df.columns}", file=sys.stderr)
            return 1
        df = df.select(wanted)
    if args.limit:
        df = df.head(args.limit)

    if args.csv:
        sys.stdout.write(df.write_csv())
    else:
        with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=400, fmt_str_lengths=40):
            print(df)
        print(f"\n{df.height} rows  |  params: {params}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
