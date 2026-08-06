"""Source-by-source loaders that populate the DuckDB warehouse.

Each module exposes an `ingest_*` function returning a list of `IngestResult`.
No loader raises on a missing optional source: a source that cannot be reached
is reported as `skipped` with a reason, and the board states the gap rather than
silently substituting a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Status = Literal["loaded", "skipped", "failed"]


@dataclass
class IngestResult:
    table: str
    source: str
    status: Status
    rows: int = 0
    detail: str = ""

    def line(self) -> str:
        mark = {"loaded": "ok", "skipped": "--", "failed": "!!"}[self.status]
        rows = f"{self.rows:>9,}" if self.status == "loaded" else " " * 9
        tail = f"  {self.detail}" if self.detail else ""
        return f"  [{mark}] {self.table:<28} {rows}  {self.source}{tail}"


__all__ = ["IngestResult", "Status"]
