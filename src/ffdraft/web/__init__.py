"""A local web UI for running a live draft off this warehouse.

Two speeds, deliberately separate:

- **Native views** (`board.py`) read the warehouse directly and render instantly.
  This is what you use while the draft clock is running.
- **Agent runs** (`runner.py`) shell out to Claude Code and take minutes. This is
  what you use the night before.

The native board is market-ordered, not projection-ordered, and says so. This
project has no projections table — `proj_pts` and VOR are produced by
`ranking-synthesizer` at agent time, so a native board cannot honestly show them.
It shows the market's ordering plus the usage context needed to disagree with it.
"""

from __future__ import annotations

__all__ = ["app"]
