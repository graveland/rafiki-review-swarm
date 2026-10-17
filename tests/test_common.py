# tests: the shared prompt/json helpers every stage leans on, plus the
# conftest fake itself (proving the harness before the stages lean on it)
import pytest

from review_swarm import common
from review_swarm.common import AtCap, Fail


def test_last_json_block_takes_the_last_fence():
    text = 'reasoning\n```json\n{"n": 1}\n```\nmore reasoning\n```json\n{"n": 2}\n```\n'
    assert common.last_json_block(text) == {"n": 2}


def test_last_json_block_leaves_earlier_commas_alone():
    text = '```json\n{"a": 1,}\n```\n```json\n{"b": [1, 2]}\n```'
    assert common.last_json_block(text) == {"b": [1, 2]}


def test_last_json_block_repairs_trailing_commas():
    assert common.last_json_block('```json\n{"a": [1,],}\n```') == {"a": [1]}


@pytest.mark.parametrize("text", ["no fence here", "", None])
def test_last_json_block_without_a_fence_raises(text):
    with pytest.raises(ValueError):
        common.last_json_block(text)


def test_render_prompt_fills_only_passed_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "PROMPTS", tmp_path)
    (tmp_path / "task.md").write_text('answer with {"verdict": "ok"} for {seat} about {subject}')
    assert common.render_prompt("task.md", seat="review:finder") == \
        'answer with {"verdict": "ok"} for review:finder about {subject}'


def test_render_prompt_keeps_braces_in_a_filled_value(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "PROMPTS", tmp_path)
    (tmp_path / "task.md").write_text("lens: {lens}")
    assert common.render_prompt("task.md", lens='{"a": 1}') == 'lens: {"a": 1}'


def test_fake_spawn_records_the_call_and_numbers_children(fake_rafiki):
    cid, sid = fake_rafiki.spawn("find-1", cwd="/w", prefill_lines=["a.py"], labels={"arm": "seam"},
                                 preset="review:finder")
    assert (cid, sid) == ("c_1", "s_1")
    assert fake_rafiki.spawns[0]["name"] == "find-1"
    assert fake_rafiki.spawns[0]["prefill_lines"] == ["a.py"]
    assert fake_rafiki.child_info(cid)["labels"] == {"arm": "seam"}


def test_fake_refuses_the_next_spawn_once(fake_rafiki):
    fake_rafiki.refuse_next = 1
    with pytest.raises(AtCap):
        fake_rafiki.spawn("find-1", cwd="/w", prefill_lines=[], labels={}, preset="review:finder")
    cid, _ = fake_rafiki.spawn("find-1", cwd="/w", prefill_lines=[], labels={}, preset="review:finder")
    assert (cid, fake_rafiki.refuse_next, len(fake_rafiki.spawns)) == ("c_1", 0, 1)


def test_fake_export_serves_the_answer_configured_for_the_name_prefix(fake_rafiki):
    fake_rafiki.answers["find"] = '{"findings": []}'
    cid, _ = fake_rafiki.spawn("find-1", cwd="/w", prefill_lines=[], labels={}, preset="review:finder")
    assert fake_rafiki.export(cid)["turns"][0]["content"][0]["text"] == '{"findings": []}'


def test_fake_seat_spawn_delivery_follows_the_preset_kind(fake_rafiki):
    fake_rafiki.presets = {"review:finder": {"kind": "fundi"}, "review:probe": {"kind": "claude"}}
    args = {"cwd": "/w", "packet_files": ["packet.json"], "instruction": "read it", "labels": {}}
    assert fake_rafiki.spawn_seat("a", preset="review:finder", **args)[2] == "prefill"
    assert fake_rafiki.spawn_seat("b", preset="review:probe", **args)[2] == "read-list"
    with pytest.raises(Fail):
        fake_rafiki.spawn_seat("c", preset="review:nope", **args)
    assert [s["name"] for s in fake_rafiki.seat_spawns] == ["a", "b"]
