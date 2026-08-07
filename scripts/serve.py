#!/usr/bin/env python
"""Run the local draft UI.

    uv run python scripts/serve.py            # http://127.0.0.1:8000
    uv run python scripts/serve.py --port 8080

Binds to localhost only, and refuses to do otherwise. The app has no
authentication and can start subprocesses; exposing it on a network interface
would hand anyone who can reach the port the ability to run commands as you.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import uvicorn  # noqa: E402

from ffdraft.config import load_sources, project_root  # noqa: E402

LOCAL_ONLY = {"127.0.0.1", "localhost", "::1"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    args = parser.parse_args()

    if args.host not in LOCAL_ONLY:
        parser.error(
            f"--host {args.host} is not local. This app has no auth and can run "
            "commands; bind it to 127.0.0.1 only."
        )

    warehouse = project_root() / load_sources()["warehouse"]["path"]
    if not warehouse.is_file():
        print(f"No warehouse at {warehouse}.", file=sys.stderr)
        print("Run: uv run python scripts/ingest.py", file=sys.stderr)
        return 1

    print(f"  draft board  ->  http://{args.host}:{args.port}")
    print(f"  warehouse    ->  {warehouse}")
    uvicorn.run(
        "ffdraft.web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
