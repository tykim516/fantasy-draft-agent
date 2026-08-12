"""FastAPI app for the local draft UI.

Local-only by design. It binds to 127.0.0.1, has no auth, and can start
subprocesses — none of which is safe on a network interface. `scripts/serve.py`
enforces the bind address.
"""

from __future__ import annotations

from typing import Any

import polars as pl
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import project_root
from ..league import load_league
from .board import load_board, picks_for_slot, survives_to
from .draft import DraftStore
from .runner import Runner

STATIC_DIR = project_root() / "src" / "ffdraft" / "web" / "static"

app = FastAPI(title="fantasy-draft-agent", docs_url="/api/docs")
store = DraftStore()
runner = Runner(project_root())

# The board is rebuilt on demand but cached, because re-running four named
# queries on every keystroke would make the table feel broken. /api/refresh-board
# and a completed ingest both drop it.
_cache: dict[str, Any] = {"board": None, "meta": None}


def _board() -> tuple[pl.DataFrame, Any]:
    if _cache["board"] is None:
        board, meta = load_board()
        _cache["board"], _cache["meta"] = board, meta
    return _cache["board"], _cache["meta"]


def _clean(value: Any) -> Any:
    """NaN and inf are not JSON. Polars hands them over freely."""
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
    return value


# --- board -----------------------------------------------------------------


@app.get("/api/board")
def get_board(
    top: int = 300,
    position: str | None = None,
    search: str | None = None,
    include_taken: bool = True,
) -> dict[str, Any]:
    board, meta = _board()
    state = store.snapshot()
    taken = set(state["taken"])

    frame = board
    if position and position.upper() != "ALL":
        wanted = {p.strip().upper() for p in position.split(",")}
        frame = frame.filter(pl.col("position").is_in(list(wanted)))
    if search:
        frame = frame.filter(pl.col("player").str.to_lowercase().str.contains(search.lower()))
    if not include_taken:
        frame = frame.filter(~pl.col("key").is_in(list(taken)))

    rows = []
    for row in frame.head(top).to_dicts():
        row = {key: _clean(value) for key, value in row.items()}
        row["taken"] = row["key"] in taken
        rows.append(row)

    # Availability at the user's next pick, if a slot is set.
    next_pick = None
    slot = state.get("slot")
    if slot:
        every = picks_for_slot(slot, state["teams"], 20)
        upcoming = [p for p in every if p >= state["next_overall"]]
        next_pick = upcoming[0] if upcoming else None
        for row in rows:
            row["survives"] = survives_to(
                row.get("adp_rank_adj"),
                next_pick,
                state["teams"],
                earliest=row.get("adp_earliest"),
                latest=row.get("adp_latest"),
            )

    return {
        "rows": rows,
        "meta": meta.as_dict(),
        "draft": state,
        "next_pick": next_pick,
        "total": frame.height,
    }


@app.post("/api/board/reload")
def reload_board() -> dict[str, Any]:
    _cache["board"] = None
    board, meta = _board()
    return {"rows": board.height, "meta": meta.as_dict()}


@app.get("/api/compare")
def compare(a: str, b: str) -> dict[str, Any]:
    """Native head-to-head. The agent version adds news; this is the data."""
    board, _ = _board()
    rows = []
    for name in (a, b):
        match = board.filter(pl.col("player").str.to_lowercase().str.contains(name.lower()))
        if match.is_empty():
            raise HTTPException(404, f"no player matching {name!r}")
        rows.append({k: _clean(v) for k, v in match.head(1).to_dicts()[0].items()})

    fields = [
        ("ecr", "ECR", "lower"),
        ("adp", "Sleeper ADP", "lower"),
        ("ecr_vs_adp", "ECR vs ADP", "higher"),
        ("tier", "Tier (in position)", "lower"),
        ("target_share", "Target share", "higher"),
        ("wopr", "WOPR", "higher"),
        ("snap_pct", "Snap %", "higher"),
        ("rz20_per_game", "RZ20 touches/gm", "higher"),
        ("poe_per_game", "Points over expected/gm", "higher"),
        ("team_target_share", "Share of team targets", "higher"),
    ]
    comparison = []
    for key, label, better in fields:
        left, right = rows[0].get(key), rows[1].get(key)
        winner = None
        if isinstance(left, int | float) and isinstance(right, int | float) and left != right:
            winner = "a" if ((left < right) == (better == "lower")) else "b"
        comparison.append({"key": key, "label": label, "a": left, "b": right, "winner": winner})

    return {"a": rows[0], "b": rows[1], "fields": comparison}


# --- draft state -----------------------------------------------------------


class ConfigureBody(BaseModel):
    slot: int | None = None
    teams: int | None = None


class PickBody(BaseModel):
    key: str
    player: str = ""
    position: str = ""
    team: str | None = None
    mine: bool = False


@app.get("/api/draft")
def get_draft() -> dict[str, Any]:
    state = store.snapshot()
    slot = state.get("slot")
    if slot:
        state["my_picks"] = picks_for_slot(slot, state["teams"], 20)
    return state


@app.post("/api/draft/configure")
def configure(body: ConfigureBody) -> dict[str, Any]:
    league = load_league()
    teams = body.teams or league.teams
    if body.slot is not None and not 1 <= body.slot <= teams:
        raise HTTPException(400, f"slot must be between 1 and {teams}")
    return store.configure(body.slot, teams)


@app.post("/api/draft/pick")
def take(body: PickBody) -> dict[str, Any]:
    return store.take(body.key, body.player, body.position, body.team, body.mine)


@app.delete("/api/draft/pick/{key:path}")
def release(key: str) -> dict[str, Any]:
    return store.release(key)


class MineBody(BaseModel):
    key: str
    mine: bool


@app.post("/api/draft/mine")
def set_mine(body: MineBody) -> dict[str, Any]:
    """Flip whose pick it was, without un-drafting the player."""
    return store.set_mine(body.key, body.mine)


@app.post("/api/draft/undo")
def undo() -> dict[str, Any]:
    return store.undo()


@app.post("/api/draft/reset")
def reset() -> dict[str, Any]:
    return store.reset()


# --- agent commands --------------------------------------------------------


class RunBody(BaseModel):
    command: str
    args: str = ""


@app.post("/api/run")
def run(body: RunBody) -> dict[str, Any]:
    """Start a command, or hand back the one already running.

    Re-attaching rather than 409-ing is the point: a long agent run that produces
    no visible output looks broken, so the natural move is to press the button
    again. That must show you the run in progress, not an error.
    """
    try:
        job = runner.start(body.command, body.args)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError:
        existing = runner.running(body.command.strip().lstrip("/"))
        if existing is None:
            raise
        view = existing.view()
        view["attached"] = True
        return view
    return job.view()


@app.get("/api/run")
def current(command: str) -> dict[str, Any]:
    """The live job for a command, so a reloaded page can re-attach to it."""
    job = runner.running(command.strip().lstrip("/"))
    if job is None:
        raise HTTPException(404, f"no running /{command}")
    return job.view()


@app.get("/api/run/{job_id}")
def poll(job_id: str, since: int = 0) -> dict[str, Any]:
    try:
        job = runner.get(job_id)
    except KeyError as exc:
        raise HTTPException(404, f"no job {job_id}") from exc
    view = job.view(since)
    # A finished ingest changes the warehouse, so the cached board is now stale.
    if job.command == "refresh" and job.status == "done":
        _cache["board"] = None
    return view


@app.post("/api/run/{job_id}/cancel")
def cancel(job_id: str) -> dict[str, Any]:
    try:
        return runner.cancel(job_id)
    except KeyError as exc:
        raise HTTPException(404, f"no job {job_id}") from exc


@app.get("/api/runs")
def runs() -> list[dict[str, Any]]:
    return runner.recent()


# --- static ----------------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
