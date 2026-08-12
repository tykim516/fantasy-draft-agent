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

import csv
import json
import re
import time
from datetime import date
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

    # The hand-maintained ADP file is preferred over the draft-scraping proxy:
    # it is Sleeper's own published number rather than our resampling of it.
    file_result = _ingest_adp_file(con, sources)
    results.append(file_result)
    if file_result.status == "loaded":
        results.append(
            IngestResult(
                "sleeper_adp",
                SOURCE,
                "skipped",
                detail="static ADP file loaded; draft proxy not needed",
            )
        )
    else:
        results.append(_ingest_adp_proxy(con, sources, base, cache_cfg, ttls, force))
    return results


# Header spellings accepted from an ADP export, normalized to lowercase with
# non-letters stripped. Spelled out so a changed export fails loudly on the
# column it could not find rather than silently ranking on the wrong one.
#
# Two layouts are in use and both must keep working, because history/ holds files
# in each and a board rebuilt from an older export should still work:
#
#   rank-only   Name,ADP,Position ADP            ADP *is* a dense rank
#   full        Rank,Player,Trend,Avg Pos,Hi/Lo,Pct
#               ADP is a real average pick, and Player packs name+position+team
_ADP_COLUMNS = {
    "player": ("name", "player", "playername", "fullname"),
    "adp": ("avgpos", "adp", "avgpick", "averagedraftposition", "avg", "overall"),
    "rank": ("rank", "overallrank", "adprank"),
    "position": ("positionadp", "position", "pos"),
    "team": ("team", "tm"),
    "hi_lo": ("hilo", "highlow", "range"),
    "pct": ("pct", "percent", "drafted"),
    "trend": ("trend",),
    "gsis_id": ("gsisid", "gsis"),
    "sleeper_id": ("sleeperid", "playerid"),
}

# "Jahmyr Gibbs RB  DET", "Bills DST  BUF" — name, position and team packed into
# one cell. Anchored at the end so a surname that happens to look like a position
# cannot win over the real trailing pair.
_COMBINED_PLAYER_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<pos>QB|RB|WR|TE|K|DST|DEF|FB)\s+(?P<team>[A-Z]{2,3})$"
)


def _header_map(fieldnames: list[str]) -> dict[str, str]:
    """Map our canonical column names onto whatever the file actually used."""
    seen = {re.sub(r"[^a-z0-9]", "", (f or "").lower()): f for f in fieldnames}
    found = {}
    for canonical, spellings in _ADP_COLUMNS.items():
        for spelling in spellings:
            if spelling in seen:
                found[canonical] = seen[spelling]
                break
    return found


def _split_player_cell(cell: str) -> tuple[str, str, str | None]:
    """Split "Jahmyr Gibbs RB  DET" into name, position and team.

    Returns the cell unchanged as the name when it carries no trailing
    position/team pair, which is how the older `Name` column behaves.
    """
    match = _COMBINED_PLAYER_RE.match((cell or "").strip())
    if not match:
        return (cell or "").strip(), "", None
    return match.group("name").strip(), match.group("pos"), match.group("team")


def _number(value: str | None) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_hi_lo(value: str | None) -> tuple[float | None, float | None]:
    """Split "3/11" into (earliest, latest) pick.

    This is the honest uncertainty on an ADP: a player whose average is 24 but
    who has gone as early as 9 is a different draft-day problem from one who has
    never gone before 22, and averaging that away loses the distinction.
    """
    text = (value or "").strip()
    if "/" not in text:
        return None, None
    first, _, second = text.partition("/")
    return _number(first), _number(second)


def _ingest_adp_file(con: duckdb.DuckDBPyConnection, sources: dict) -> IngestResult:
    """Sleeper's published ADP, supplied as a file a human maintains.

    Sleeper exposes no ADP endpoint — `/players/nfl/adp` and `/adp/nfl/{season}`
    both 404, and nflreadpy has no ADP loader — so the number the other managers
    in the league are looking at while they draft can only be copied in by hand.

    ADP is a price, ECR is a value, and the board keeps both. This feeds
    availability and slot-survival math; `ff_rankings` stays the pricing anchor.
    """
    from ..market.resolve import (
        ResolverStats,
        build_index,
        load_aliases,
        normalize_position,
        parse_position_adp,
        resolve_row,
        write_pending,
    )
    from ..warehouse import replace_table

    cfg = sources["sources"].get("sleeper_adp_file", {})
    table = "sleeper_adp"
    if not cfg.get("enabled", False):
        return IngestResult(table, SOURCE, "skipped", detail="sleeper_adp_file disabled")

    path = resolve(cfg["path"])
    if not path.is_file():
        return IngestResult(
            table, SOURCE, "skipped", detail=f"no ADP file at {cfg['path']} — see config/market/"
        )

    as_of = str(cfg.get("as_of") or "")
    source_label = f"{SOURCE}_file (as of {as_of or 'unknown'})"

    try:
        with path.open(newline="") as handle:
            reader = csv.DictReader(handle)
            columns = _header_map(reader.fieldnames or [])
            missing = [key for key in ("player", "adp") if key not in columns]
            if missing:
                return IngestResult(
                    table,
                    SOURCE,
                    "failed",
                    detail=(
                        f"{path.name} is missing {missing}; found columns "
                        f"{reader.fieldnames}"
                    ),
                )
            raw_rows = list(reader)
    except Exception as exc:  # noqa: BLE001
        return IngestResult(table, SOURCE, "failed", detail=f"{type(exc).__name__}: {exc}")

    index = build_index(con)
    aliases = load_aliases(resolve(cfg.get("aliases", "config/market/adp_aliases.yml")))
    stats = ResolverStats()

    rows = []
    skipped = 0
    for raw in raw_rows:
        cell = (raw.get(columns["player"]) or "").strip()
        if not cell:
            continue

        # Exports carry trailing non-player rows — "Legend", "Injury", "News" —
        # which have a value in the first column and nothing else. A row with no
        # usable ADP is not a player, so it is dropped rather than parsed into a
        # phantom entry sitting at the bottom of the board.
        adp = _number(raw.get(columns["adp"]))
        if adp is None:
            skipped += 1
            continue

        name, packed_position, packed_team = _split_player_cell(cell)
        # A dedicated position column wins if present; otherwise take the one
        # packed into the player cell.
        position, reported_rank = parse_position_adp(raw.get(columns.get("position", ""), ""))
        if not position:
            position = normalize_position(packed_position)
        team = (raw.get(columns.get("team", "")) or "").strip() or packed_team

        resolution = resolve_row(
            name,
            position,
            index,
            aliases,
            team=team,
            file_gsis_id=raw.get(columns.get("gsis_id", "")),
            file_sleeper_id=raw.get(columns.get("sleeper_id", "")),
        )
        stats.record(resolution, name, position)

        hi, lo = _parse_hi_lo(raw.get(columns.get("hi_lo", "")))
        rows.append(
            {
                "gsis_id": resolution.gsis_id,
                "team": resolution.team or team or None,
                "player": name,
                "position": position or None,
                # The average pick when the export gives one, else the dense rank
                # the older layout published. `adp_is_average_pick` says which,
                # because pick math is only valid against a real pick number.
                "adp": adp,
                "adp_rank": int(_number(raw.get(columns.get("rank", ""))) or 0) or None,
                "adp_is_average_pick": "rank" in columns,
                "adp_earliest": hi,
                "adp_latest": lo,
                "adp_drafted_pct": _number(raw.get(columns.get("pct", ""))),
                "position_rank_reported": reported_rank,
                "link_method": resolution.method,
                "crosswalk_status": resolution.status,
                "adp_source": "sleeper_file",
                "adp_as_of": as_of or None,
            }
        )

    if not rows:
        return IngestResult(table, SOURCE, "failed", detail=f"{path.name} parsed to 0 usable rows")

    write_pending(resolve(cfg.get("aliases", "config/market/adp_aliases.yml")), stats.pending)

    df = pl.DataFrame(rows, infer_schema_length=None).sort("adp")
    count = replace_table(con, table, df, source_label)

    detail = stats.summary()
    if stats.pending:
        names = ", ".join(name for name, _, _ in stats.pending[:5])
        detail += f" — needs review: {names}"
        if len(stats.pending) > 5:
            detail += f" (+{len(stats.pending) - 5} more)"
    stale = _adp_staleness_warning(cfg)
    if stale:
        detail += f" — {stale}"
    return IngestResult(table, SOURCE, "loaded", rows=count, detail=detail)


def _adp_staleness_warning(cfg: dict) -> str:
    """ADP moves fast in August; a silently ageing file is the main failure mode."""
    as_of = cfg.get("as_of")
    if not as_of:
        return "no as_of set in sources.yml; board cannot report the ADP date"
    if isinstance(as_of, str):
        try:
            as_of = date.fromisoformat(as_of)
        except ValueError:
            return f"as_of {as_of!r} is not an ISO date"
    age = (date.today() - as_of).days
    limit = cfg.get("max_age_days", 10)
    if age > limit:
        return f"STALE: ADP is {age} days old (limit {limit}); refresh config/market/"
    return ""


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
