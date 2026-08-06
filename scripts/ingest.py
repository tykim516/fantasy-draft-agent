#!/usr/bin/env python
"""Thin CLI over `ffdraft.ingest`. All behaviour lives in the package.

    python scripts/ingest.py                       # full reload, all sources
    python scripts/ingest.py --tables player_stats # one table
    python scripts/ingest.py --incremental         # upsert on (player_id, season, week)
    python scripts/ingest.py --staged              # safe while a session is open
    python scripts/ingest.py --freshness           # just print meta_ingest

`--staged` builds into a separate file and atomically renames it over the live
warehouse, which is how a scheduled run avoids DuckDB's single-writer lock.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ffdraft.config import load_sources  # noqa: E402
from ffdraft.ingest import IngestResult  # noqa: E402
from ffdraft.ingest.fantasypros import ingest_fantasypros  # noqa: E402
from ffdraft.ingest.nflverse import (  # noqa: E402
    build_depth_chart_current,
    build_dst_stats,
    build_redzone,
    ingest_nflverse,
)
from ffdraft.ingest.sleeper import ingest_sleeper  # noqa: E402
from ffdraft.warehouse import connect, freshness, staged_build, warehouse_path  # noqa: E402

ALL_SOURCES = ("nflverse", "sleeper", "fantasypros")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--sources", nargs="+", choices=ALL_SOURCES, default=list(ALL_SOURCES))
    parser.add_argument("--tables", nargs="+", help="limit the nflverse load to these tables")
    parser.add_argument(
        "--seasons",
        nargs="+",
        type=int,
        help="override the pinned history seasons (a board built this way is not reproducible)",
    )
    parser.add_argument("--force", action="store_true", help="ignore cache TTLs")
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="upsert on (player_id, season, week) instead of a full reload",
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="build into a temp file and atomically swap; use for scheduled runs",
    )
    parser.add_argument(
        "--skip-derived", action="store_true", help="skip pbp_redzone and dst_stats"
    )
    parser.add_argument("--freshness", action="store_true", help="print meta_ingest and exit")
    return parser.parse_args(argv)


def print_freshness() -> int:
    if not warehouse_path().exists():
        print(f"no warehouse at {warehouse_path()} — run `python scripts/ingest.py` first")
        return 1
    con = connect(read_only=True)
    try:
        df = freshness(con)
    finally:
        con.close()
    if df.is_empty():
        print("warehouse exists but meta_ingest is empty")
        return 1
    with __import__("polars").Config(tbl_rows=100, tbl_width_chars=200):
        print(df)
    return 0


def run(args: argparse.Namespace) -> int:
    sources = load_sources()
    if args.seasons:
        sources["seasons"]["history"] = list(args.seasons)
        print(f"! season override in effect: {args.seasons} — this board is not reproducible")

    manager = staged_build() if args.staged else contextlib.nullcontext(connect())
    results: list[IngestResult] = []

    with manager as con:
        try:
            if "nflverse" in args.sources:
                print("nflverse")
                results += ingest_nflverse(
                    con,
                    sources,
                    tables=args.tables,
                    mode="upsert" if args.incremental else "replace",
                )
                for line in results:
                    print(line.line())
                if not args.skip_derived and not args.tables:
                    print("derived")
                    derived = (
                        build_redzone(con, sources),
                        build_dst_stats(con),
                        build_depth_chart_current(con),
                    )
                    for result in derived:
                        results.append(result)
                        print(result.line())

            if "sleeper" in args.sources:
                print("sleeper")
                sleeper_results = ingest_sleeper(con, sources, force=args.force)
                results += sleeper_results
                for line in sleeper_results:
                    print(line.line())

            if "fantasypros" in args.sources:
                print("fantasypros")
                fp_results = ingest_fantasypros(con, sources, force=args.force)
                results += fp_results
                for line in fp_results:
                    print(line.line())
        finally:
            if not args.staged:
                con.close()

    loaded = [r for r in results if r.status == "loaded"]
    skipped = [r for r in results if r.status == "skipped"]
    failed = [r for r in results if r.status == "failed"]
    print(
        f"\n{len(loaded)} loaded, {len(skipped)} skipped, {len(failed)} failed"
        f"  ->  {warehouse_path()}"
    )
    if failed:
        print("\nfailures:")
        for result in failed:
            print(f"  {result.table}: {result.detail}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.freshness:
        return print_freshness()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
