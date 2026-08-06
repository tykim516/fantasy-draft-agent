"""FantasyPros consensus ECR/ADP — optional, keyed, and never load-bearing.

The free tier is prototype-only, so this source is best-effort by design. If
`FANTASYPROS_API_KEY` is unset the source is skipped with a reason and ingest
carries on; `market.adp_order` in `config/sources.yml` then falls through to the
Sleeper proxy, and `market.ecr_order` to the ffverse `ff_rankings` table, which
carries the same FantasyPros consensus without a key.

Nothing here may raise into the caller. A rate limit or a schema change on a
prototype API is not a reason for a board to fail to build.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import duckdb
import httpx
import polars as pl

from ..config import resolve
from . import IngestResult

SOURCE = "fantasypros"


def _cache_path(cache_cfg: dict, name: str) -> Path:
    directory = resolve(cache_cfg["dir"])
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"fantasypros_{name}.json"


def ingest_fantasypros(
    con: duckdb.DuckDBPyConnection, sources: dict, force: bool = False
) -> list[IngestResult]:
    from ..warehouse import replace_table

    cfg = sources["sources"]["fantasypros"]
    if not cfg.get("enabled", True):
        return [
            IngestResult("fantasypros_adp", SOURCE, "skipped", detail="disabled in sources.yml")
        ]

    key = os.environ.get(cfg["requires_env"], "").strip()
    if not key:
        return [
            IngestResult(
                "fantasypros_adp",
                SOURCE,
                "skipped",
                detail=(
                    f"{cfg['requires_env']} unset — falling back to ff_rankings for ECR "
                    f"and the Sleeper proxy for ADP"
                ),
            )
        ]

    season = sources["seasons"]["draft_season"]
    ttl = sources["cache"]["ttl_hours"]["fantasypros"]
    cache = _cache_path(sources["cache"], f"consensus_{season}")
    url = f"{cfg['base_url'].rstrip('/')}/{season}/consensus-rankings"

    fresh = cache.is_file() and (time.time() - cache.stat().st_mtime) < ttl * 3600
    try:
        if fresh and not force:
            payload = json.loads(cache.read_text())
        else:
            response = httpx.get(
                url,
                headers={"x-api-key": key},
                params={"position": "ALL", "type": "draft", "scoring": "PPR"},
                timeout=60.0,
            )
            response.raise_for_status()
            payload = response.json()
            cache.write_text(json.dumps(payload))
    except Exception as exc:  # noqa: BLE001 — optional source, never fatal
        return [
            IngestResult(
                "fantasypros_adp",
                SOURCE,
                "skipped",
                detail=f"fetch failed ({type(exc).__name__}: {exc}); falling back",
            )
        ]

    players = payload.get("players") if isinstance(payload, dict) else payload
    if not players:
        return [
            IngestResult(
                "fantasypros_adp", SOURCE, "skipped", detail="response carried no player rows"
            )
        ]

    try:
        df = pl.DataFrame(players, infer_schema_length=None)
    except Exception as exc:  # noqa: BLE001
        return [
            IngestResult(
                "fantasypros_adp",
                SOURCE,
                "skipped",
                detail=f"unrecognised response shape ({type(exc).__name__}: {exc})",
            )
        ]

    df = df.with_columns(
        pl.lit(payload.get("published") if isinstance(payload, dict) else None).alias("published")
    )
    rows = replace_table(con, "fantasypros_adp", df, SOURCE)
    return [IngestResult("fantasypros_adp", SOURCE, "loaded", rows=rows)]
