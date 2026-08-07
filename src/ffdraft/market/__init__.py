"""Market data: identity resolution for externally-sourced rankings.

`metrics/` is frame math over data already keyed to `gsis_id`. This package is
the step before that — turning a name-keyed file supplied by a human into rows
the warehouse can join.
"""

from __future__ import annotations

__all__ = ["resolve"]
