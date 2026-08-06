"""Sleeper public API — player master list, trending adds/drops, ADP proxy.

No auth required. The player dump is large (~14 MB as of the 2026 preseason,
larger than the docs suggest), so it is cached to `data/raw/` and refetched only
once its TTL from `config/sources.yml` expires.

The stored player table is deliberately slim. The full dump carries dozens of
fields per player and the warehouse only needs identity, position, depth-chart
position, and injury status — everything else is noise that makes the table
harder to diff.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import duckdb
import httpx
import polars as pl

from ..config import resolve
from . import IngestResult

SOURCE = "sleeper"

# Fields kept from the player dump. `gsis_id` is the join key to nflverse;
# everything else is context an analyst actually reads.
_PLAYER_FIELDS = [
    "player_id",
    "gsis_id",
    "espn_id",
    "yahoo_id",
    "full_name",
    "position",
    "fantasy_positions",
    "team",
    "age",
    "years_exp",
    "depth_chart_position",
    "depth_chart_order",
    "status",
    "injury_status",
    "injury_body_part",
    "active",
]


def _cache_path(cache_cfg: dict, name: str) -> Path:
    directory = resolve(cache_cfg["dir"])
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"sleeper_{name}.json"


def _fetch_cached(
    url: str, path: Path, ttl_hours: float, force: bool, timeout: float = 90.0
) -> tuple[Any, bool]:
    """Return (payload, from_cache). Falls back to a stale cache if the fetch fails."""
    fresh = path.is_file() and (time.time() - path.stat().st_mtime) < ttl_hours * 3600
    if fresh and not force:
        return json.loads(path.read_text()), True

    try:
        response = httpx.get(url, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        if path.is_file():
            # A stale cache beats no data, but the caller must say it is stale.
            return json.loads(path.read_text()), True
        raise
    path.write_text(json.dumps(payload))
    return payload, False


def ingest_sleeper(
    con: duckdb.DuckDBPyConnection, sources: dict, force: bool = False
) -> list[IngestResult]:
    from ..warehouse import replace_table

    cfg = sources["sources"]["sleeper"]
    results: list[IngestResult] = []
    if not cfg.get("enabled", True):
        return [
            IngestResult("sleeper_players", SOURCE, "skipped", detail="disabled in sources.yml")
        ]

    base = cfg["base_url"].rstrip("/")
    ttls = sources["cache"]["ttl_hours"]
    cache_cfg = sources["cache"]

    # --- player master list ------------------------------------------------
    try:
        payload, cached = _fetch_cached(
            base + cfg["endpoints"]["players"],
            _cache_path(cache_cfg, "players"),
            ttls["sleeper_players"],
            force,
        )
        rows = []
        for player_id, record in payload.items():
            row = {field: record.get(field) for field in _PLAYER_FIELDS}
            row["player_id"] = player_id
            row["fantasy_positions"] = (
                ",".join(record.get("fantasy_positions") or []) or None
            )
            rows.append(row)
        df = pl.DataFrame(rows, infer_schema_length=None).with_columns(
            pl.col("age").cast(pl.Float64, strict=False),
            pl.col("years_exp").cast(pl.Int64, strict=False),
            pl.col("depth_chart_order").cast(pl.Int64, strict=False),
        )
        count = replace_table(con, "sleeper_players", df, SOURCE)
        results.append(
            IngestResult(
                "sleeper_players",
                SOURCE,
                "loaded",
                rows=count,
                detail="from cache" if cached else "",
            )
        )
    except Exception as exc:  # noqa: BLE001
        results.append(
            IngestResult("sleeper_players", SOURCE, "failed", detail=f"{type(exc).__name__}: {exc}")
        )

    # --- trending adds / drops --------------------------------------------
    trending_frames = []
    for direction in ("add", "drop"):
        endpoint = cfg["endpoints"].get(f"trending_{direction}")
        if not endpoint:
            continue
        try:
            payload, _ = _fetch_cached(
                f"{base}{endpoint}?lookback_hours=24&limit=200",
                _cache_path(cache_cfg, f"trending_{direction}"),
                ttls["sleeper_trending"],
                force,
            )
            trending_frames.append(
                pl.DataFrame(payload).with_columns(direction=pl.lit(direction))
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                IngestResult(
                    f"sleeper_trending_{direction}",
                    SOURCE,
                    "failed",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )

    if trending_frames:
        combined = pl.concat(trending_frames).select(
            pl.col("player_id").cast(pl.Utf8), pl.col("count").cast(pl.Int64), "direction"
        )
        count = replace_table(con, "sleeper_trending", combined, SOURCE)
        results.append(IngestResult("sleeper_trending", SOURCE, "loaded", rows=count))

    results.append(_ingest_adp_proxy(con, sources, base, cache_cfg, ttls, force))
    return results


def _ingest_adp_proxy(
    con: duckdb.DuckDBPyConnection,
    sources: dict,
    base: str,
    cache_cfg: dict,
    ttls: dict,
    force: bool,
) -> IngestResult:
    """Average draft position aggregated from public Sleeper drafts.

    Sleeper has no endpoint that lists public drafts, only ones that read a
    draft by id — so this needs seed ids in `sources.yml`. With none configured
    it reports unavailable rather than publishing an ADP with no drafts behind
    it, and `market.adp_order` falls through to the next source.
    """
    from ..warehouse import replace_table

    cfg = sources["sources"]["sleeper"].get("adp_proxy", {})
    if not cfg.get("enabled", False):
        return IngestResult("sleeper_adp", SOURCE, "skipped", detail="adp_proxy disabled")

    draft_ids = list(cfg.get("draft_ids") or [])
    if not draft_ids:
        return IngestResult(
            "sleeper_adp",
            SOURCE,
            "skipped",
            detail=(
                "no draft_ids configured — Sleeper exposes no public draft-discovery "
                "endpoint, so seed ids in sources.yml to enable this proxy"
            ),
        )

    picks: list[dict] = []
    failures = 0
    for draft_id in draft_ids:
        try:
            payload, _ = _fetch_cached(
                f"{base}/draft/{draft_id}/picks",
                _cache_path(cache_cfg, f"draft_{draft_id}"),
                ttls["sleeper_drafts"],
                force,
            )
            for pick in payload:
                picks.append(
                    {
                        "draft_id": draft_id,
                        "player_id": pick.get("player_id"),
                        "pick_no": pick.get("pick_no"),
                    }
                )
        except Exception:  # noqa: BLE001
            failures += 1

    if not picks:
        return IngestResult(
            "sleeper_adp", SOURCE, "failed", detail=f"all {len(draft_ids)} draft fetches failed"
        )

    df = pl.DataFrame(picks).drop_nulls(["player_id", "pick_no"])
    drafts_seen = df["draft_id"].n_unique()
    adp = (
        df.group_by("player_id")
        .agg(
            adp=pl.col("pick_no").mean(),
            adp_sd=pl.col("pick_no").std(),
            adp_min=pl.col("pick_no").min(),
            adp_max=pl.col("pick_no").max(),
            drafts=pl.len(),
        )
        .with_columns(source_drafts=pl.lit(drafts_seen))
        .sort("adp")
    )

    minimum = cfg.get("min_drafts", 0)
    detail = f"{drafts_seen} drafts"
    if failures:
        detail += f", {failures} fetch failure(s)"
    if drafts_seen < minimum:
        detail += f" — below min_drafts={minimum}, treat as low confidence"

    rows = replace_table(con, "sleeper_adp", adp, SOURCE)
    return IngestResult("sleeper_adp", SOURCE, "loaded", rows=rows, detail=detail)
