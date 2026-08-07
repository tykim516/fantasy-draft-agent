"""The draft UI: state, pick math, command construction, and the API surface.

The theme here is that a draft is one shot. There is no rerunning it, so the
tests that matter are the ones about not losing or corrupting state: a pick
recorded must survive a restart, an undo must renumber, and a bad state file must
not make the app unstartable in the middle of round four.
"""

from __future__ import annotations

import json

import pytest

from ffdraft.web.board import picks_for_slot, player_key, survives_to
from ffdraft.web.draft import DraftStore
from ffdraft.web.runner import Runner


@pytest.fixture
def store(tmp_path) -> DraftStore:
    return DraftStore(tmp_path / "draft_state.json")


def take(store: DraftStore, key: str, position: str = "RB", mine: bool = False):
    return store.take(key, f"player {key}", position, "DET", mine)


# --- snake pick math -------------------------------------------------------


def test_picks_from_the_turn():
    """Slot 5 of 10: the gap between picks alternates, which is the whole reason
    draft slot changes strategy."""
    assert picks_for_slot(5, 10, 6) == [5, 16, 25, 36, 45, 56]


def test_slot_one_and_slot_last_are_mirror_images():
    first = picks_for_slot(1, 10, 4)
    last = picks_for_slot(10, 10, 4)
    assert first == [1, 20, 21, 40]
    assert last == [10, 11, 30, 31]


def test_every_pick_is_used_exactly_once_across_the_league():
    """A round of the draft must account for every pick with no overlap."""
    seen = [pick for slot in range(1, 11) for pick in picks_for_slot(slot, 10, 5)]
    assert sorted(seen) == list(range(1, 51))


def test_slot_outside_the_league_is_rejected():
    with pytest.raises(ValueError, match="outside"):
        picks_for_slot(11, 10, 3)


@pytest.mark.parametrize(
    "adp_rank,pick,expected",
    [(40, 25, "likely"), (10, 25, "gone"), (26, 25, "toss-up"), (None, 25, "unknown")],
)
def test_survival_is_coarse_and_three_valued(adp_rank, pick, expected):
    """ADP is a central tendency with real variance; a precise probability here
    would be false confidence."""
    assert survives_to(adp_rank, pick, teams=10) == expected


# --- player keys -----------------------------------------------------------


def test_key_prefers_gsis_id():
    assert player_key({"gsis_id": "00-0036900", "player": "Ja'Marr Chase"}) == "00-0036900"


def test_team_defenses_key_on_team():
    key = player_key({"gsis_id": None, "position": "DST", "team": "CLE", "player": "Browns"})
    assert key == "DST:CLE"


def test_unlinked_players_are_marked_as_such():
    """Falling back to a name silently would reintroduce the collision problem
    the warehouse works hard to avoid, so the key says what it is."""
    key = player_key({"gsis_id": None, "position": "WR", "player": "Some Rookie"})
    assert key.startswith("unlinked:")


# --- draft state -----------------------------------------------------------


def test_a_pick_is_recorded_with_its_order(store):
    state = take(store, "a")
    assert state["taken"] == ["a"]
    assert state["picks"][0]["overall"] == 1
    assert state["next_overall"] == 2


def test_double_clicking_a_player_is_not_an_error(store):
    take(store, "a")
    state = take(store, "a")
    assert len(state["picks"]) == 1


def test_rounds_advance_with_league_size(store):
    store.configure(slot=5, teams=10)
    for i in range(10):
        take(store, f"p{i}")
    assert store.snapshot()["round"] == 2


def test_release_renumbers_the_picks_after_it(store):
    """Leaving a gap would corrupt every round calculation downstream."""
    for key in "abc":
        take(store, key)
    state = store.release("b")
    assert [(p["overall"], p["key"]) for p in state["picks"]] == [(1, "a"), (2, "c")]


def test_undo_removes_the_last_pick_only(store):
    take(store, "a")
    take(store, "b")
    assert store.undo()["taken"] == ["a"]


def test_undo_on_an_empty_draft_is_harmless(store):
    assert store.undo()["picks"] == []


def test_ownership_flips_without_undrafting(store):
    """A mis-click on "mine" must not put the player back in the pool."""
    take(store, "a", mine=True)
    state = store.set_mine("a", False)
    assert state["taken"] == ["a"]
    assert not state["picks"][0]["mine"]


def test_reset_clears_picks_but_keeps_the_slot(store):
    store.configure(slot=7, teams=10)
    take(store, "a")
    state = store.reset()
    assert state["picks"] == []
    assert state["slot"] == 7


# --- persistence -----------------------------------------------------------


def test_state_survives_a_restart(tmp_path):
    """The real failure this guards: a crashed server three rounds in."""
    path = tmp_path / "draft_state.json"
    first = DraftStore(path)
    first.configure(slot=5, teams=10)
    take(first, "a", mine=True)
    take(first, "b")

    reopened = DraftStore(path).snapshot()
    assert reopened["slot"] == 5
    assert reopened["taken"] == ["a", "b"]
    assert reopened["picks"][0]["mine"] is True


def test_a_corrupt_state_file_does_not_block_startup(tmp_path):
    """Mid-draft is the worst possible time to be unable to start the app."""
    path = tmp_path / "draft_state.json"
    path.write_text("{not json at all")
    store = DraftStore(path)
    assert store.snapshot()["picks"] == []
    assert path.with_suffix(".corrupt.json").is_file(), "the bad file is kept, not silently dropped"


def test_writes_are_atomic(tmp_path):
    """A torn write loses the draft, so the file is replaced, never appended."""
    path = tmp_path / "draft_state.json"
    store = DraftStore(path)
    take(store, "a")
    take(store, "b")
    payload = json.loads(path.read_text())
    assert len(payload["picks"]) == 2
    assert not list(tmp_path.glob("*.tmp")), "temp file was left behind"


# --- command construction --------------------------------------------------


@pytest.fixture
def runner() -> Runner:
    from ffdraft.config import project_root

    return Runner(project_root())


def test_refresh_runs_the_script_not_an_agent(runner):
    """/refresh is data-ingest and nothing else. Burning an agent turn to shell
    out to a script would be pure cost."""
    argv = runner.build("refresh")
    assert "ingest.py" in " ".join(argv)
    assert "claude" not in " ".join(argv)
    assert "--staged" in argv


def test_board_and_compare_go_through_claude(runner):
    for command in ("board", "compare"):
        argv = runner.build(command, "--slot 5")
        assert argv[0].endswith("claude")
        assert f"/{command} --slot 5" in argv


def test_a_leading_slash_is_accepted(runner):
    assert runner.build("/refresh") == runner.build("refresh")


def test_only_the_three_commands_are_allowed(runner):
    """The runner starts subprocesses, so the command list is a closed set."""
    for bad in ("rm", "sh -c whoami", "ingest", ""):
        with pytest.raises(ValueError, match="unknown command"):
            runner.build(bad)


def test_agent_runs_stream_their_progress(runner):
    """Plain `claude -p` writes nothing until the run ends, which makes a healthy
    five-minute /board indistinguishable from a crash. The stream flags are what
    make progress visible, so they are asserted rather than assumed."""
    argv = runner.build("board", "--slot 5")
    assert "--output-format" in argv
    assert "stream-json" in argv
    assert "--verbose" in argv


def test_refresh_does_not_ask_for_json(runner):
    """ingest.py prints human-readable lines; parsing them as JSON would drop
    every one of them."""
    assert "--output-format" not in runner.build("refresh")


# --- stream formatting -----------------------------------------------------


def test_dispatch_of_a_subagent_is_visible():
    """Watching the three analysts fan out is the clearest sign a board run is
    healthy, so a Task call names the agent."""
    from ffdraft.web.runner import format_event

    lines = format_event(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Task",
                        "input": {
                            "subagent_type": "market-analyst",
                            "description": "price the board",
                        },
                    }
                ]
            },
        }
    )
    assert lines == ["→ dispatch market-analyst: price the board"]


def test_subagent_output_is_indented():
    from ffdraft.web.runner import format_event

    lines = format_event(
        {
            "type": "assistant",
            "parent_tool_use_id": "toolu_1",
            "message": {"content": [{"type": "text", "text": "checking usage"}]},
        }
    )
    assert lines == ["   ↳ checking usage"]


def test_tool_results_are_summarised_not_dumped():
    """A single query result can be tens of thousands of characters; printing it
    buries the progress you opened the console to see."""
    from ffdraft.web.runner import format_event

    lines = format_event(
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "x" * 5000}]},
        }
    )
    assert lines == ["   ← 5,000 chars"]
    assert "xxxx" not in "".join(lines)


def test_the_final_result_carries_cost_and_duration():
    from ffdraft.web.runner import format_event

    lines = format_event(
        {
            "type": "result",
            "subtype": "success",
            "result": "the board",
            "duration_ms": 184000,
            "total_cost_usd": 1.234,
        }
    )
    joined = "\n".join(lines)
    assert "the board" in joined
    assert "184s" in joined
    assert "$1.23" in joined


def test_an_errored_result_says_so():
    from ffdraft.web.runner import format_event

    lines = format_event({"type": "result", "is_error": True, "result": "boom", "duration_ms": 10})
    assert "ERROR" in "\n".join(lines)


def test_unknown_event_types_are_dropped_quietly():
    from ffdraft.web.runner import format_event

    assert format_event({"type": "rate_limit_event", "rate_limit_info": {}}) == []


def test_arguments_are_not_shell_interpreted(runner):
    """argv is passed as a list and never through a shell, so metacharacters are
    inert rather than injectable."""
    argv = runner.build("board", "--slot 5; rm -rf /")
    assert argv[2] == "/board --slot 5; rm -rf /"
    assert not any(part == "sh" or part == "-c" for part in argv)


# --- API surface -----------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from ffdraft.web import app as app_module

    monkeypatch.setattr(app_module, "store", DraftStore(tmp_path / "draft_state.json"))
    return TestClient(app_module.app)


@pytest.mark.warehouse
def test_board_endpoint_returns_rows_and_provenance(client):
    payload = client.get("/api/board?top=5").json()
    assert len(payload["rows"]) == 5
    assert payload["meta"]["teams"] == 10
    assert payload["meta"]["adp_as_of"], "the board must always say how old its ADP is"
    assert "K" in payload["meta"]["excluded_positions"]


@pytest.mark.warehouse
def test_board_marks_taken_players(client):
    first = client.get("/api/board?top=1").json()["rows"][0]
    client.post(
        "/api/draft/pick",
        json={"key": first["key"], "player": first["player"], "position": first["position"]},
    )
    again = client.get("/api/board?top=1").json()["rows"][0]
    assert again["taken"] is True


@pytest.mark.warehouse
def test_hiding_taken_players_removes_them(client):
    first = client.get("/api/board?top=1").json()["rows"][0]
    client.post(
        "/api/draft/pick",
        json={"key": first["key"], "player": first["player"], "position": first["position"]},
    )
    rows = client.get("/api/board?top=5&include_taken=false").json()["rows"]
    assert first["key"] not in {row["key"] for row in rows}


@pytest.mark.warehouse
def test_excluded_positions_never_reach_the_ui(client):
    rows = client.get("/api/board?top=300").json()["rows"]
    assert "K" not in {row["position"] for row in rows}


@pytest.mark.warehouse
def test_setting_a_slot_produces_survival_reads(client):
    client.post("/api/draft/configure", json={"slot": 5})
    payload = client.get("/api/board?top=40").json()
    assert payload["next_pick"] == 5
    assert {row["survives"] for row in payload["rows"]} <= {"likely", "gone", "toss-up", "unknown"}


@pytest.mark.warehouse
def test_an_out_of_range_slot_is_rejected(client):
    assert client.post("/api/draft/configure", json={"slot": 99}).status_code == 400


@pytest.mark.warehouse
def test_compare_names_a_winner_per_field(client):
    payload = client.get("/api/compare?a=Gibbs&b=Bijan").json()
    assert payload["a"]["player"] and payload["b"]["player"]
    assert {field["winner"] for field in payload["fields"]} <= {"a", "b", None}


@pytest.mark.warehouse
def test_compare_with_an_unknown_player_is_a_404(client):
    assert client.get("/api/compare?a=Nobody At All&b=Gibbs").status_code == 404


@pytest.mark.warehouse
def test_an_unknown_command_is_rejected_by_the_api(client):
    assert client.post("/api/run", json={"command": "rm -rf"}).status_code == 400


@pytest.mark.warehouse
def test_the_board_opens_the_warehouse_read_only(warehouse):
    """DuckDB is single-writer. A read-write handle held by an open browser tab
    would lock out the ingest that /refresh kicks off — and those two must be
    able to run at once, since refreshing mid-session is the point.

    The `warehouse` fixture already holds a connection, so this test only passes
    if load_board can coexist with it.
    """
    from ffdraft.web.board import load_board

    board, meta = load_board()
    assert board.height > 100
    assert meta.teams == 10
