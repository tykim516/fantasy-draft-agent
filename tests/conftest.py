"""Shared fixtures.

Unit tests never touch the warehouse — they run on hand-built frames so they
work on a fresh clone before any ingest. The tests that do need real data are
marked `warehouse` and skip cleanly when `data/ff.duckdb` is absent.
"""

from __future__ import annotations

import copy

import pytest

from ffdraft.config import SCHEMA_PATH, load_yaml
from ffdraft.league import league_path, load_league
from ffdraft.warehouse import warehouse_path


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "warehouse: needs a populated data/ff.duckdb (run scripts/ingest.py)"
    )


@pytest.fixture(scope="session")
def league():
    """The real main.yml, validated."""
    return load_league("main")


@pytest.fixture
def raw_league() -> dict:
    """A mutable copy of main.yml, for building invalid configs."""
    return copy.deepcopy(load_yaml(league_path("main")))


@pytest.fixture(scope="session")
def schema() -> dict:
    return load_yaml(SCHEMA_PATH)


@pytest.fixture(scope="session")
def warehouse():
    """A read-only connection, or a skip when the warehouse has not been built."""
    from ffdraft.warehouse import connect

    if not warehouse_path().exists():
        pytest.skip(f"no warehouse at {warehouse_path()}; run scripts/ingest.py")
    con = connect(read_only=True)
    yield con
    con.close()
