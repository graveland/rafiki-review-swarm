# tests: a project's tools.md reaches every whole-repo seat task, placed before the
# answer contract, and a project without one gets the task unchanged.
import json

from review_swarm import runs, stages

TOOLS = "Use mcp__codescan__code_search on tree code.proj for callers and callees."


def rdir_with(tmp_path, tools):
    rdir = tmp_path / "run"
    (rdir / "project").mkdir(parents=True)
    (rdir / "project" / "concerns.json").write_text(json.dumps(
        {"concerns": [{"name": "security", "category": "defect", "prompt": "Hunt it."}]}))
    if tools is not None:
        (rdir / "project" / "tools.md").write_text(tools + "\n")
    return rdir


def cell():
    return {"scope": "component `x`", "notes": "", "arm": "swarm", "name": "x",
            "concern": {"name": "security", "category": "defect", "prompt": "Hunt it."}}


def finding(category):
    return {"id": "f", "category": category, "concern": "security", "file": "a.go", "line": 1,
            "title": "t", "claim": "c", "severity": "S3", "effort": "S", "doc": "README.md"}


def test_find_and_every_verify_kind_carry_the_tools_before_the_answer_contract(tmp_path):
    rdir = rdir_with(tmp_path, TOOLS)
    tasks = [stages.find_task(rdir, cell(), []), stages.find_task(rdir, cell(), [], context="ctx")]
    for category in ("defect", "quality", "drift"):
        f = finding(category)
        (job,) = stages.verify_jobs(f)
        tasks.append(stages.verify_task(rdir, f, job))
    for t in tasks:
        assert TOOLS in t
        assert t.index(TOOLS) < t.rindex(runs.ANSWER_CONTRACT)


def test_without_tools_md_a_task_is_unchanged(tmp_path):
    rdir = rdir_with(tmp_path, None)
    assert "TOOLS AVAILABLE" not in stages.find_task(rdir, cell(), [])
    assert runs.with_tools(rdir, "do it") == "do it"


def test_a_task_with_no_answer_contract_gets_the_tools_appended(tmp_path):
    rdir = rdir_with(tmp_path, TOOLS)
    assert runs.with_tools(rdir, "do it").startswith("do it\n\nTOOLS AVAILABLE FOR THIS PROJECT:\n" + TOOLS)
