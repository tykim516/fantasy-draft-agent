"""Background jobs for the three commands, streamed back to the browser.

`/board`, `/refresh` and `/compare` are Claude Code slash commands that fan out
to sub-agents. They are not CLI programs, so the UI runs them the only way they
can be run headlessly: `claude -p "/board --slot 5"`.

That takes minutes and spends tokens, which is why the native board exists
alongside it. This is the night-before tool.

`/refresh` is the exception: it is `data-ingest` and nothing else, and
`scripts/ingest.py` does the same work with no agent, no latency and no cost. So
refresh runs the script directly. Doing otherwise would burn an agent turn to
shell out to a script.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

Status = Literal["running", "done", "failed", "cancelled"]

# Enough scrollback to hold a full board without letting a runaway job eat memory.
MAX_LINES = 4000


@dataclass
class Job:
    id: str
    command: str
    argv: list[str]
    status: Status = "running"
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    finished_at: str | None = None
    exit_code: int | None = None
    streaming: bool = False
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LINES))
    _started_monotonic: float = field(default_factory=time.monotonic)
    _process: subprocess.Popen | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def elapsed_seconds(self) -> int:
        return int(time.monotonic() - self._started_monotonic)

    def append(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)

    def view(self, since: int = 0) -> dict[str, Any]:
        """Lines from `since` onward, for incremental polling.

        If the deque has already discarded what the client last saw, the offset
        is reported honestly rather than silently replaying from a wrong point.
        """
        with self._lock:
            lines = list(self.lines)
        total = len(lines)
        start = max(0, min(since, total))
        return {
            "id": self.id,
            "command": self.command,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "elapsed_seconds": self.elapsed_seconds,
            "lines": lines[start:],
            "next_offset": total,
            "truncated": total == MAX_LINES,
        }


def _brief(value: Any, limit: int = 80) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_event(payload: dict) -> list[str]:
    """Turn one stream-json event into display lines.

    Deliberately lossy. The raw stream carries entire tool results — a board
    query's full output — and dumping that into the console buries the thing you
    actually want to know, which is *where the run has got to*. So tool results
    are summarised to a size, and only assistant prose and tool calls are shown
    in full.

    Sub-agent work is indented. `parent_tool_use_id` is set on any message
    produced inside a Task call, which is exactly the fan-out to usage-analyst,
    market-analyst and news-scout — seeing those three interleave is the clearest
    signal that a /board run is healthy.
    """
    kind = payload.get("type")
    nested = bool(payload.get("parent_tool_use_id"))
    prefix = "   ↳ " if nested else ""
    lines: list[str] = []

    if kind == "system" and payload.get("subtype") == "init":
        model = payload.get("model", "?")
        agents = payload.get("agents") or []
        return [f"· session started · model {model} · {len(agents)} sub-agents available"]

    if kind == "assistant":
        for block in payload.get("message", {}).get("content", []):
            btype = block.get("type")
            if btype == "text" and block.get("text", "").strip():
                lines.append(prefix + block["text"].rstrip())
            elif btype == "tool_use":
                name = block.get("name", "tool")
                args = block.get("input", {}) or {}
                if name == "Task":
                    who = args.get("subagent_type", "agent")
                    lines.append(f"{prefix}→ dispatch {who}: {_brief(args.get('description', ''))}")
                elif name == "Bash":
                    lines.append(f"{prefix}→ bash: {_brief(args.get('command', ''))}")
                else:
                    hint = args.get("file_path") or args.get("pattern") or args.get("query") or ""
                    lines.append(f"{prefix}→ {name}{': ' + _brief(hint) if hint else ''}")
        return lines

    if kind == "user":
        # Tool results: report size only. The content is often a whole table.
        for block in payload.get("message", {}).get("content", []):
            if block.get("type") == "tool_result":
                content = block.get("content")
                size = len(str(content)) if content is not None else 0
                flag = " (error)" if block.get("is_error") else ""
                lines.append(f"{prefix}   ← {size:,} chars{flag}")
        return lines

    if kind == "result":
        text = payload.get("result") or ""
        seconds = (payload.get("duration_ms") or 0) / 1000
        cost = payload.get("total_cost_usd")
        lines.append("")
        lines.append("=" * 60)
        if text:
            lines.extend(str(text).splitlines())
        tail = f"· finished in {seconds:.0f}s"
        if cost is not None:
            tail += f" · ${cost:.2f}"
        if payload.get("is_error"):
            tail += " · ERROR"
        lines.append(tail)
        return lines

    return []


class Runner:
    """Owns the job table. One job per command at a time."""

    def __init__(self, project_root: Path) -> None:
        self.root = project_root
        self.jobs: dict[str, Job] = {}
        self._by_command: dict[str, str] = {}
        self._lock = threading.Lock()

    # --- building the command line ----------------------------------------

    def _claude_argv(self, prompt: str) -> list[str]:
        claude = shutil.which("claude")
        if not claude:
            raise FileNotFoundError(
                "the `claude` CLI is not on PATH — the agent commands need Claude "
                "Code installed. The native board does not."
            )
        # stream-json is not cosmetic. Plain `claude -p` writes NOTHING until the
        # whole run finishes, so a five-minute /board looks identical to a job
        # that crashed on startup — you cannot tell progress from failure. The
        # NDJSON stream emits an event per step, including which sub-agent is
        # working, so the console shows the fan-out as it happens.
        #
        # --permission-mode acceptEdits keeps a headless run from stalling on a
        # prompt nobody is watching. The agents only write to data/exports.
        return [
            claude,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "acceptEdits",
        ]

    def build(self, command: str, args: str = "") -> list[str]:
        command = command.strip().lstrip("/")
        if command not in {"board", "refresh", "compare"}:
            raise ValueError(f"unknown command {command!r}")

        if command == "refresh":
            # No agent needed: /refresh is data-ingest, which is this script.
            argv = [sys.executable, str(self.root / "scripts" / "ingest.py"), "--staged"]
            argv += shlex.split(args)
            return argv

        prompt = f"/{command} {args}".strip()
        return self._claude_argv(prompt)

    # --- lifecycle ---------------------------------------------------------

    def start(self, command: str, args: str = "") -> Job:
        argv = self.build(command, args)
        with self._lock:
            running = self._by_command.get(command)
            if running and self.jobs[running].status == "running":
                raise RuntimeError(f"/{command} is already running")

            job = Job(
                id=uuid.uuid4().hex[:12],
                command=command,
                argv=argv,
                streaming="--output-format" in argv,
            )
            self.jobs[job.id] = job
            self._by_command[command] = job.id

        job.append(f"$ {' '.join(shlex.quote(part) for part in argv)}")
        if command != "refresh":
            job.append("# agent run — fans out to sub-agents; expect several minutes")
        threading.Thread(target=self._pump, args=(job,), daemon=True).start()
        return job

    def _pump(self, job: Job) -> None:
        try:
            process = subprocess.Popen(
                job.argv,
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as exc:  # noqa: BLE001
            job.append(f"failed to start: {type(exc).__name__}: {exc}")
            job.status = "failed"
            job.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
            return

        job._process = process
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip("\n")
            if not job.streaming:
                job.append(line)
                continue
            # A stream-json run still emits plain text on stderr (merged here), so
            # anything unparseable is passed through rather than swallowed —
            # otherwise a startup error would vanish and the job would look hung.
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                job.append(line)
                continue
            for formatted in format_event(payload):
                job.append(formatted)
        process.wait()

        job.exit_code = process.returncode
        job.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
        if job.status == "cancelled":
            job.append("-- cancelled --")
        else:
            job.status = "done" if process.returncode == 0 else "failed"
            job.append(f"-- exit {process.returncode} --")

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.jobs[job_id]
        if job.status == "running" and job._process:
            job.status = "cancelled"
            job._process.terminate()
        return job.view(since=0)

    def get(self, job_id: str) -> Job:
        return self.jobs[job_id]

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        jobs = sorted(self.jobs.values(), key=lambda j: j.started_at, reverse=True)
        return [
            {
                "id": j.id,
                "command": j.command,
                "status": j.status,
                "started_at": j.started_at,
                "finished_at": j.finished_at,
                "elapsed_seconds": j.elapsed_seconds,
            }
            for j in jobs[:limit]
        ]

    def running(self, command: str) -> Job | None:
        """The live job for a command, if any.

        The UI needs this to re-attach after a page reload — without it a running
        job is orphaned and the only visible sign of it is a 409 on the next
        attempt, which reads as an error rather than as "it's still going".
        """
        job_id = self._by_command.get(command)
        if job_id and self.jobs[job_id].status == "running":
            return self.jobs[job_id]
        return None
