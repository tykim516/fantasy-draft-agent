"""Live draft state: who is gone, who is mine, in what order.

Persisted to disk on every mutation. A draft is one shot — a browser refresh, a
laptop sleep, or a crashed server three rounds in must not lose the board, so
nothing lives only in memory or only in the page.

Picks are an ordered list, not a set, because order is the whole record: it gives
you the round each player went in, lets undo mean "take back the last thing",
and lets the roster reconstruct itself.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_STATE_PATH = "data/draft_state.json"


@dataclass
class Pick:
    key: str
    player: str
    position: str
    team: str | None
    mine: bool
    overall: int
    at: str

    @property
    def round_number_unknown(self) -> None:  # pragma: no cover - documentation
        """Round is derived from `overall` and league size, never stored.

        Storing it would let the two disagree after a league-size change.
        """


@dataclass
class DraftState:
    slot: int | None = None
    teams: int = 10
    picks: list[Pick] = field(default_factory=list)

    def taken_keys(self) -> set[str]:
        return {pick.key for pick in self.picks}

    def my_picks(self) -> list[Pick]:
        return [pick for pick in self.picks if pick.mine]

    def as_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "teams": self.teams,
            "picks": [asdict(pick) for pick in self.picks],
            "taken": sorted(self.taken_keys()),
            "next_overall": len(self.picks) + 1,
            "round": len(self.picks) // self.teams + 1,
        }


class DraftStore:
    """Thread-safe, file-backed draft state.

    Uvicorn serves requests from a thread pool, and a fast drafter clicking while
    a board request is in flight is a genuine race — so every mutation takes a
    lock and rewrites the file atomically.
    """

    def __init__(self, path: str | Path = DEFAULT_STATE_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._state = self._read()

    # --- persistence -------------------------------------------------------

    def _read(self) -> DraftState:
        if not self.path.is_file():
            return DraftState()
        try:
            payload = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            # A corrupt state file must not make the app unstartable mid-draft.
            # The old file is kept for inspection rather than overwritten.
            broken = self.path.with_suffix(".corrupt.json")
            try:
                self.path.replace(broken)
            except OSError:
                pass
            return DraftState()
        return DraftState(
            slot=payload.get("slot"),
            teams=payload.get("teams", 10),
            picks=[Pick(**pick) for pick in payload.get("picks", [])],
        )

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "slot": self._state.slot,
                    "teams": self._state.teams,
                    "picks": [asdict(pick) for pick in self._state.picks],
                },
                indent=2,
            )
        )
        os.replace(tmp, self.path)  # atomic; a torn write here loses the draft

    # --- reads -------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._state.as_dict()

    # --- mutations ---------------------------------------------------------

    def configure(self, slot: int | None, teams: int | None = None) -> dict[str, Any]:
        with self._lock:
            if teams is not None:
                self._state.teams = teams
            self._state.slot = slot
            self._write()
            return self._state.as_dict()

    def take(self, key: str, player: str, position: str, team: str | None, mine: bool) -> dict:
        """Record a pick. Taking an already-taken player is a no-op, not an error —
        a double-click during a draft should not raise in your face."""
        with self._lock:
            if key in self._state.taken_keys():
                return self._state.as_dict()
            self._state.picks.append(
                Pick(
                    key=key,
                    player=player,
                    position=position,
                    team=team,
                    mine=mine,
                    overall=len(self._state.picks) + 1,
                    at=datetime.now(UTC).isoformat(timespec="seconds"),
                )
            )
            self._write()
            return self._state.as_dict()

    def release(self, key: str) -> dict[str, Any]:
        """Un-take a player and renumber everything after him.

        Renumbering matters: if the third pick is removed, the fourth really did
        become the third. Leaving gaps would silently corrupt every round
        calculation downstream.
        """
        with self._lock:
            self._state.picks = [pick for pick in self._state.picks if pick.key != key]
            for index, pick in enumerate(self._state.picks, start=1):
                pick.overall = index
            self._write()
            return self._state.as_dict()

    def undo(self) -> dict[str, Any]:
        with self._lock:
            if self._state.picks:
                self._state.picks.pop()
                self._write()
            return self._state.as_dict()

    def set_mine(self, key: str, mine: bool) -> dict[str, Any]:
        with self._lock:
            for pick in self._state.picks:
                if pick.key == key:
                    pick.mine = mine
                    break
            self._write()
            return self._state.as_dict()

    def reset(self) -> dict[str, Any]:
        with self._lock:
            slot, teams = self._state.slot, self._state.teams
            self._state = DraftState(slot=slot, teams=teams)
            self._write()
            return self._state.as_dict()
