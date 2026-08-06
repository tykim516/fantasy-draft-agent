#!/usr/bin/env python
"""Validate league YAML against config/leagues/_schema.yml.

    python scripts/validate_league.py            # every league in config/leagues/
    python scripts/validate_league.py main       # one league
    python scripts/validate_league.py --summary  # also print derived league shape

Exits non-zero if any league fails. Failure is loud and lists every problem at
once rather than stopping at the first.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ffdraft.config import LEAGUES_DIR  # noqa: E402
from ffdraft.league import LeagueValidationError, league_path, load_league  # noqa: E402


def discover() -> list[Path]:
    return sorted(p for p in LEAGUES_DIR.glob("*.yml") if not p.name.startswith("_"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("leagues", nargs="*", help="league name or path; default is all")
    parser.add_argument("--summary", action="store_true", help="print the derived league shape")
    args = parser.parse_args(argv)

    targets = [league_path(name) for name in args.leagues] if args.leagues else discover()
    if not targets:
        print(f"no league files found in {LEAGUES_DIR}")
        return 1

    failures = 0
    for path in targets:
        try:
            league = load_league(path)
        except LeagueValidationError as exc:
            failures += 1
            print(f"FAIL {path}")
            for i, error in enumerate(exc.errors, 1):
                print(f"  {i}. {error}")
            continue
        except (FileNotFoundError, ValueError) as exc:
            failures += 1
            print(f"FAIL {path}\n  {exc}")
            continue

        print(f"OK   {path}")
        if args.summary:
            print(json.dumps(league.assumptions(), indent=2))
            print(f"  flex slots leaguewide: {league.flex_slots_total()}")
            print(f"  flex eligibility: {league.flex_slots}")

    if failures:
        print(f"\n{failures} of {len(targets)} league file(s) failed validation")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
