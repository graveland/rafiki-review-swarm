# tests: pr_state, the state.json round trip and the friction log
import datetime
import json

import pytest

from review_swarm.common import Fail
from review_swarm.pr_state import append_friction, load_state, save_state


def test_save_state_load_state_round_trips(tmp_path):
    state = {"stage": "gather", "seats": {"review:finder": "v1"}, "findings": [], "n": 0}
    save_state(tmp_path, state)
    assert load_state(tmp_path) == state


def test_save_state_leaves_no_temp_file_behind(tmp_path):
    save_state(tmp_path, {"a": 1})
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_save_state_sorts_keys(tmp_path):
    save_state(tmp_path, {"b": 2, "a": 1})
    assert (tmp_path / "state.json").read_text() == '{\n "a": 1,\n "b": 2\n}\n'


def test_load_state_on_a_missing_file_raises_fail(tmp_path):
    with pytest.raises(Fail):
        load_state(tmp_path / "no-such-run")


def test_load_state_on_a_corrupt_file_raises_fail_naming_the_run(tmp_path):
    (tmp_path / "state.json").write_text('{"stage": "gather"')
    with pytest.raises(Fail, match=str(tmp_path)):
        load_state(tmp_path)


def test_save_state_wraps_an_unserializable_value_and_cleans_up(tmp_path):
    with pytest.raises(Fail, match=str(tmp_path)):
        save_state(tmp_path, {"when": object()})
    assert [p.name for p in tmp_path.iterdir()] == []


def test_save_state_wraps_a_missing_run_directory(tmp_path):
    with pytest.raises(Fail, match=str(tmp_path)):
        save_state(tmp_path / "no-such-run", {"a": 1})


def test_append_friction_wraps_a_missing_run_directory(tmp_path):
    with pytest.raises(Fail, match=str(tmp_path)):
        append_friction(tmp_path / "no-such-run", {"kind": "requeue"})


def test_append_friction_writes_one_sorted_line_per_call(tmp_path):
    append_friction(tmp_path, {"kind": "requeue", "seat": "review:finder"})
    append_friction(tmp_path, {"kind": "drop", "seat": "review:verifier", "why": "at cap"})
    lines = (tmp_path / "friction.jsonl").read_text().splitlines()
    assert len(lines) == 2
    for line, kind in zip(lines, ["requeue", "drop"]):
        rec = json.loads(line)
        assert rec["kind"] == kind
        assert list(rec) == sorted(rec)
        assert line == json.dumps(rec, sort_keys=True)
        datetime.datetime.fromisoformat(rec["at"])
