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

import shlex
import shutil
import subprocess
import sys
import threading
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
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LINES))
    _process: subprocess.Popen | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

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
            "lines": lines[start:],
            "next_offset": total,
            "truncated": total == MAX_LINES,
        }


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
        # --permission-mode acceptEdits keeps a headless run from stalling on a
        # prompt nobody is watching. The agents only write to data/exports.
        return [claude, "-p", prompt, "--permission-mode", "acceptEdits"]

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

            job = Job(id=uuid.uuid4().hex[:12], command=command, argv=argv)
            self.jobs[job.id] = job
            self._by_command[command] = job.id

        job.append(f"$ {' '.join(shlex.quote(part) for part in argv)}")
        if command != "refresh":
            job.append("# agent run — this fans out to sub-agents and takes minutes")
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
            job.append(line.rstrip("\n"))
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
            }
            for j in jobs[:limit]
        ]
