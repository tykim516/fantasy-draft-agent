"""Project paths and the `config/sources.yml` reader.

Small on purpose: everything else in the package resolves paths through here so
that a relative path in a YAML file means the same thing whether it was read
from a script, a test, or a sub-agent's shell command.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml


@functools.cache
def project_root() -> Path:
    """Walk up from this file until we find the directory holding pyproject.toml."""
    here = Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError(f"could not locate project root above {here}")


def resolve(path: str | Path) -> Path:
    """Interpret a config-relative path against the project root."""
    p = Path(path)
    return p if p.is_absolute() else project_root() / p


CONFIG_DIR = project_root() / "config"
LEAGUES_DIR = CONFIG_DIR / "leagues"
SCHEMA_PATH = LEAGUES_DIR / "_schema.yml"
SOURCES_PATH = CONFIG_DIR / "sources.yml"
SQL_DIR = project_root() / "sql"


def load_yaml(path: str | Path) -> dict[str, Any]:
    path = resolve(path)
    if not path.is_file():
        raise FileNotFoundError(f"no such config file: {path}")
    with path.open() as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping (got {type(data).__name__})")
    return data


def load_sources() -> dict[str, Any]:
    return load_yaml(SOURCES_PATH)


def season_set(sources: dict[str, Any], which: str | list[str]) -> list[int]:
    """Expand a sources.yml season-set name into concrete seasons.

    Accepts a list of names to union them — `schedules` needs both the completed
    seasons (for historical points allowed) and the upcoming one (for byes and
    strength of schedule). `static` means the table is not season-scoped and
    contributes nothing.
    """
    if isinstance(which, list):
        merged: list[int] = []
        for name in which:
            merged.extend(season_set(sources, name))
        return sorted(set(merged))

    seasons = sources["seasons"]
    if which == "history":
        return list(seasons["history"])
    if which == "forward_looking":
        return [seasons["draft_season"]]
    if which == "static":
        return []
    raise ValueError(f"unknown season set {which!r} (expected history/forward_looking/static)")
