"""Metric computation over warehouse frames.

These modules take DataFrames and return DataFrames. They deliberately do not
run their own joins against the warehouse — the joins live in `sql/` as named
queries so that a metric has exactly one definition and a change to it shows up
in a diff. Keeping the Python pure also makes every metric testable without a
database.
"""

from __future__ import annotations

INSUFFICIENT_DATA = "insufficient_data"

__all__ = ["INSUFFICIENT_DATA"]
