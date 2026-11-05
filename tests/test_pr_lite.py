# review-swarm: the lite stage - one gatherer, a forked panel, one consolidator
"""The contract these tests pin down: the gatherer and every claude-kind cell
start together; no fork is spawned until the gatherer has settled, and every
fork names the gatherer as its source; fork cells go seat-major with one warm
cell per seat whose first turn must land before the seat's other cells start;
a failed gather or a failed warm cell spawns nothing that depends on it and
says so in friction; a pack too big for a seat skips that seat rather than
trimming; the consolidator's verdicts land in verify-pr's shape with nothing
dropped, and the report shows refuted findings beside their refutation.
"""

import json
from types import SimpleNamespace

import pytest

from review_swarm import pr_gather, pr_lite, pr_report, pr_runs, pr_state
from review_swarm.common import Fail
from tests.conftest import FakeRafiki
from tests.test_pr_gather import (
    DAEMON,
    fake_gh,
    make_home,
    two_commit_repo,
    write_lenses,
)

LITE = {"gather": "review-lite:gather", "panel": ["review-lite:panel-1", "review-lite:panel-2"],
        "consolidate": "review-lite:consolidate"}
SEATS = {"review-lite:gather": {"kind": "fundi", "model": "m/gather"},
         "review-lite:panel-1": {"kind": "claude", "model": "anthropic/opus"},
         "review-lite:panel-2": {"kind": "fundi", "model": "m/astra"},
         "review-lite:consolidate": {"kind": "fundi", "model": "m/sol"}}

GATHER = '```json\n{"left_out": [{"what": "tests", "why": "unchanged"}], "unresolved": [], "notes": []}\n```'
CELL_HIT = """```json
{"checklist": [{"lens": "always-lens", "answer": "findings", "reason": "src/a.py:1"}],
 "findings": [{"lens": "always-lens", "title": "Stale a", "category": "defect", "file": "src/a.py", "line": 1,
   "severity": "S2", "claim": "a is read before it is set", "evidence": "`a = 2` at src/a.py:1",
   "repro": "import twice"}],
 "process_notes": []}
```"""
CELL_OTHER = """```json
{"checklist": [{"lens": "always-lens", "answer": "findings", "reason": "src/b.py:1"}],
 "findings": [{"lens": "always-lens", "title": "B unused", "category": "quality", "file": "src/b.py", "line": 1,
   "severity": "S3", "claim": "b is never read", "evidence": "src/b.py:1", "repro": "grep b"}],
 "process_notes": []}
```"""


def consolidate_answer(ids, verdicts):
    rows = [{"id": i, "verdict": v, "reasoning": f"because {v}"} for i, v in zip(ids, verdicts, strict=True)]
    return "```json\n" + json.dumps({"verdicts": rows, "process_notes": []}) + "\n```"


class LiteFake(FakeRafiki):
    """FakeRafiki plus an event log in spawn/stop order, per-child costs (a cell
    whose first turn has not landed reports 0) and an optional model catalog."""

    def __init__(self):
        super().__init__()
        self.events: list[tuple] = []
        self.costs: dict[str, float] = {}
        self.cost_after_polls: dict[str, int] = {}
        self.polls: dict[str, int] = {}
        self.busy_polls: dict[str, int] = {}
        self.catalog: list[dict] | None = None

    def spawn_seat(self, name, **kw):
        cid, sid, delivery = super().spawn_seat(name, **kw)
        self.events.append(("seat", name, cid))
        return cid, sid, delivery

    def spawn_fork(self, name, **kw):
        cid, sid = super().spawn_fork(name, **kw)
        self.events.append(("fork", name, cid, kw["source"]))
        return cid, sid

    def stop(self, child_id):
        super().stop(child_id)
        self.events.append(("stop", child_id))

    def child_info(self, child_id):
        info = super().child_info(child_id)
        self.polls[child_id] = self.polls.get(child_id, 0) + 1
        name = self._children.get(child_id, {}).get("name", "")
        cost = 0.01
        for prefix, c in self.costs.items():
            if name.startswith(prefix):
                cost = c
        for prefix, n in self.cost_after_polls.items():
            if name.startswith(prefix) and self.polls[child_id] <= n:
                cost = 0.0
        for prefix, n in self.busy_polls.items():
            if name.startswith(prefix) and self.polls[child_id] <= n:
                info["status"] = "running"
        info["cost_usd"] = cost
        return info

    def list_models(self):
        return self.catalog or []


@pytest.fixture
def lite(tmp_path, monkeypatch):
    target = tmp_path / "repo"
    first, head = two_commit_repo(target, remote="https://github.com/o/r.git")
    home = make_home(tmp_path, target, pr_json={"lite": LITE})
    monkeypatch.setattr(pr_lite, "LENSES", write_lenses(tmp_path / "lenses"))
    monkeypatch.setattr(pr_lite, "POLL", 0)
    monkeypatch.setattr(pr_runs, "gh_pr_view",
                        lambda pr, repo: {"baseRefOid": first, "headRefOid": head, "title": "Add b",
                                          "body": "Why b exists."})
    monkeypatch.setattr(pr_gather, "gh_json", fake_gh)
    rdir, run = pr_runs.pr_run_new(home, "demo", "pr", pr=7, daemon=DAEMON)
    rk = LiteFake()
    rk.presets.update(SEATS)
    p = f"lite-{run['run_id']}-"
    rk.answers[p + "gather"] = GATHER
    rk.answers[p + "panel-1--whole"] = CELL_OTHER
    rk.answers[p + "panel-2--"] = CELL_HIT
    return SimpleNamespace(rdir=rdir, run=run, home=home, rk=rk, prefix=p)


def run_stage(lite, **kw):
    args = SimpleNamespace(run=lite.run["run_id"], focus=kw.pop("focus", None), parallel=kw.pop("parallel", 2),
                           timeout=60)
    return pr_lite.stage_lite(lite.rk, lite.home, args)


def friction_of(rdir, kind):
    f = rdir / "friction.jsonl"
    return [e for e in (json.loads(l) for l in f.read_text().splitlines()) if e["kind"] == kind] if f.exists() else []


def ids_from_task(rdir):
    task = next(rdir.glob("verify/*/consolidate/task.md")).read_text()
    block = task.split("FINDINGS:\n```json\n", 1)[1].split("\n```", 1)[0]
    return [u["id"] for b in json.loads(block) for u in b["findings"]]


# --- planning ---------------------------------------------------------------------


FOCUSES = [{"name": "general", "prompt": ""}, {"name": "l1", "prompt": "L1"}, {"name": "l2", "prompt": "L2"}]
SPECS = {"s:claude": {"kind": "claude"}, "s:f1": {"kind": "fundi"}, "s:f2": {"kind": "fundi"}}


def test_a_fork_seat_gets_one_cell_per_focus_and_a_claude_seat_one_whole_cell():
    cells = pr_lite.plan_cells(["s:claude", "s:f1", "s:f2"], SPECS, FOCUSES)
    assert [(c["seat"], c["focus"], c["delivery"]) for c in cells] == [
        ("s:claude", "general", "independent"),
        ("s:f1", "general", "fork"), ("s:f1", "l1", "fork"), ("s:f1", "l2", "fork"),
        ("s:f2", "general", "fork"), ("s:f2", "l1", "fork"), ("s:f2", "l2", "fork")]
    # The independent cell carries every lens; a lens cell its one lens.
    assert cells[0]["always"] == ["l1", "l2"]
    assert cells[2]["always"] == ["l1"] and cells[2]["lens"] == "l1"
    assert cells[1]["lens"] is None and cells[1]["always"] == ["l1", "l2"]


def test_the_grant_needed_is_the_independent_cells_plus_the_parallel_forks():
    cells = pr_lite.plan_cells(["s:claude", "s:f1", "s:f2"], SPECS, FOCUSES)
    assert pr_lite.required_children(cells, 4) == 5
    assert pr_lite.required_children(cells, 0) == 2


def test_the_pack_budget_is_a_share_of_the_smallest_fork_consumers_window():
    cfg = {"consolidate": "s:f2"}
    specs = {"s:claude": {"kind": "claude", "window": 10_000},
             "s:f1": {"kind": "fundi", "window": 200_000},
             "s:f2": {"kind": "fundi", "window": 200_000}}
    cells = pr_lite.plan_cells(["s:claude", "s:f1"], specs, FOCUSES)
    budget, usable = pr_lite.pack_budget(specs, cfg, cells)
    # The claude seat never holds the pack; the consolidator is charged its reserve.
    assert usable == {"s:f1": 200_000, "s:f2": 200_000 - pr_lite.CONSOLIDATE_RESERVE_TOKENS}
    assert budget == int((200_000 - pr_lite.CONSOLIDATE_RESERVE_TOKENS) * pr_lite.PACK_FRACTION)


def test_with_no_window_reported_the_fallback_budget_is_stated():
    specs = {"s:f1": {"kind": "fundi", "window": None}, "s:c": {"kind": "claude", "window": None}}
    cells = pr_lite.plan_cells(["s:f1"], specs, FOCUSES)
    assert pr_lite.pack_budget(specs, {"consolidate": "s:c"}, cells) == (pr_lite.FALLBACK_PACK_TOKENS, {})


def test_focus_narrows_and_refuses_an_unknown_name():
    lenses = [{"name": "l1", "prompt": "L1"}, {"name": "l2", "prompt": "L2"}]
    assert [f["name"] for f in pr_lite.focus_list(lenses)] == ["general", "l1", "l2"]
    assert [f["name"] for f in pr_lite.focus_list(lenses, ["l2"])] == ["l2"]
    with pytest.raises(Fail, match="nope"):
        pr_lite.focus_list(lenses, ["nope"])


def test_consolidator_words_map_onto_votes_and_an_unknown_word_stays_unsettled():
    doc = {"verdicts": [{"id": "a", "verdict": "confirmed"}, {"id": "b", "verdict": "refuted"},
                        {"id": "c", "verdict": "uncertain"}, {"id": "d", "verdict": "maybe"},
                        {"id": "a", "verdict": "refuted"}]}
    votes, notes = pr_lite.consolidator_votes(doc, "s:c")
    assert votes["a"]["refuted"] is False and votes["b"]["refuted"] is True
    assert votes["c"]["refuted"] is None and votes["c"]["uncertain"] is True
    assert votes["d"]["refuted"] is None and votes["d"]["uncertain"] is False
    assert any("second verdict" in n for n in notes) and any("'maybe'" in n for n in notes)


# --- the stage ----------------------------------------------------------------------


def test_the_stage_gathers_forks_and_consolidates(lite):
    rk, rdir = lite.rk, lite.rdir
    # Two findings (one per seat); the consolidator refutes the fork's, upholds the other.
    rk.answers[lite.prefix + "consolidate"] = "placeholder"
    calls = {}

    def answer(child_id, orig=rk.answer):
        name = rk._children[child_id]["name"]
        if name.endswith("consolidate"):
            ids = ids_from_task(rdir)
            calls["ids"] = ids
            return consolidate_answer(ids, ["refuted", "confirmed"][: len(ids)])
        return orig(child_id)

    rk.answer = answer
    out = run_stage(lite)
    assert out["stage"] == "lite"

    gather_cid = next(e[2] for e in rk.events if e[0] == "seat" and e[1].endswith("gather"))
    forks = [e for e in rk.events if e[0] == "fork"]
    # Every fork (the panel cells and the consolidator) borrows the gatherer.
    assert forks and all(e[3] == gather_cid for e in forks)
    # No fork before the gatherer was stopped, i.e. after it settled.
    stop_at = rk.events.index(("stop", gather_cid))
    assert all(rk.events.index(e) > stop_at for e in forks)
    # The claude cell started beside the gatherer, independently, with the change to read.
    claude = next(s for s in rk.seat_spawns if s["preset"] == "review-lite:panel-1")
    assert claude["packet_files"] == ["packet/intent.md", "packet/diff.patch"]
    assert "DELIVERY: INDEPENDENT" in claude["instruction"]
    fork_prompts = [s["prompt"] for s in rk.fork_spawns if s["preset"] == "review-lite:panel-2"]
    assert fork_prompts and all(p.startswith("DELIVERY: SHARED PACK") for p in fork_prompts)

    vdir = next(rdir.glob("verify/*"))
    rows = [json.loads(l) for l in (vdir / "verdicts.jsonl").read_text().splitlines()]
    verdicts = sorted(m["verdict"] for r in rows for m in r["members"])
    assert verdicts == ["confirmed", "refuted"]
    state = pr_state.load_state(rdir)
    assert state["status"] == "done"
    assert json.loads((vdir / "state.json").read_text())["stage"] == "lite"
    assert not (rdir / pr_lite.CLAIM).exists()

    report = pr_report.write_report(rdir, rdir.parent / "report.md").read_text()
    assert "## Refuted" in report and "because refuted" in report
    assert "Not shown" not in report
    assert "(fork)" in report and "(independent)" in report
    assert "not independent corroboration" in report


def test_fork_cells_wait_for_their_seats_warm_cell(lite):
    rk = lite.rk
    # The warm cell (the seat's general cell) is still running for its first
    # polls and reports no cost for the first three: its first turn has not landed.
    rk.cost_after_polls[lite.prefix + "panel-2--general"] = 3
    rk.busy_polls[lite.prefix + "panel-2--general"] = 10
    seen = []
    orig = rk.spawn_fork

    def spawn_fork(name, **kw):
        warm = next((c for c, ch in rk._children.items() if ch["name"].endswith("panel-2--general")), None)
        seen.append((name, rk.polls.get(warm, 0)))
        return orig(name, **kw)

    rk.spawn_fork = spawn_fork
    run_stage(lite, parallel=4)
    panel = [(n, p) for n, p in seen if "panel-2" in n]
    assert panel[0][0].endswith("panel-2--general")
    # Its siblings were spawned only once a poll saw a cost above zero (the 4th),
    # and while the warm cell was still running: the gate is the first turn, not
    # the whole cell.
    assert len(panel) == 3 and all(p >= 4 for _, p in panel[1:])
    assert all(p <= 10 for _, p in panel[1:])


def test_a_warm_cell_that_never_lands_stops_its_seat(lite):
    rk = lite.rk
    # Answers match by sorted prefix, so the seat-wide answer is replaced by
    # per-cell ones for the warm cell to be the one that fails.
    del rk.answers[lite.prefix + "panel-2--"]
    rk.answers[lite.prefix + "panel-2--general"] = ""
    rk.answers[lite.prefix + "panel-2--always"] = CELL_HIT
    rk.answers[lite.prefix + "panel-2--paths"] = CELL_HIT
    rk.costs[lite.prefix + "panel-2--general"] = 0.0
    run_stage(lite)
    forks = [e for e in rk.events if e[0] == "fork" and "panel-2" in e[1]]
    assert [e[1].rsplit("-", 1)[-1] for e in forks] == ["general"]
    assert friction_of(lite.rdir, "warm-failed")


def test_a_failed_gather_spawns_no_panel_fork_but_keeps_the_independent_seat(lite):
    rk = lite.rk
    rk.answers[lite.prefix + "gather"] = ""
    run_stage(lite)
    panel_forks = [e for e in rk.events if e[0] == "fork" and "consolidate" not in e[1]]
    assert panel_forks == []
    assert any(s["preset"] == "review-lite:panel-1" for s in rk.seat_spawns)
    assert friction_of(lite.rdir, "gather-failed") and friction_of(lite.rdir, "no-pack")
    # The consolidator has no pack to fork, so it reads independently.
    cons = next(s for s in rk.seat_spawns if s["preset"] == "review-lite:consolidate")
    assert "DELIVERY: INDEPENDENT" in cons["instruction"]


def test_a_pack_too_big_for_a_seat_skips_the_seat_instead_of_trimming(lite):
    rk = lite.rk
    # The fake gatherer's last turn is 15 tokens; a 20-token window holds 12.
    rk.catalog = [{"id": "m/astra", "context_window": 20}, {"id": "m/sol", "context_window": 10**6}]
    run_stage(lite)
    assert not [e for e in rk.events if e[0] == "fork" and "panel-2" in e[1]]
    assert friction_of(lite.rdir, "pack-exceeds-window")


def test_a_finding_the_consolidator_skips_stays_incomplete(lite):
    rk = lite.rk

    def answer(child_id, orig=rk.answer):
        if rk._children[child_id]["name"].endswith("consolidate"):
            return consolidate_answer(ids_from_task(lite.rdir)[:1], ["uncertain"])
        return orig(child_id)

    rk.answer = answer
    run_stage(lite)
    rows = [json.loads(l) for l in next(lite.rdir.glob("verify/*/verdicts.jsonl")).read_text().splitlines()]
    assert sorted(m["verdict"] for r in rows for m in r["members"]) == ["incomplete", "incomplete"]
    assert friction_of(lite.rdir, "instruction-breach")
    report = pr_report.write_report(lite.rdir, lite.rdir.parent / "r.md").read_text()
    assert "uncertain (the code does not settle it)" in report


def test_a_delta_run_is_refused(lite, monkeypatch):
    run = dict(lite.run, mode="delta")
    monkeypatch.setattr(lite.home, "load_run", lambda rid: (lite.rdir, run))
    with pytest.raises(Fail, match="delta"):
        run_stage(lite)


def test_a_run_another_stage_has_moved_is_refused_and_the_claim_released(lite):
    st = pr_state.load_state(lite.rdir)
    st["status"] = "confirmed"
    pr_state.save_state(lite.rdir, st)
    with pytest.raises(Fail, match="fresh run"):
        run_stage(lite)
    assert not (lite.rdir / pr_lite.CLAIM).exists()
    assert lite.rk.events == []


def test_a_cap_refusal_names_the_grant_needed(lite):
    lite.rk.refuse_next = 1
    with pytest.raises(Fail, match="max_children >= 3"):
        run_stage(lite)
    assert pr_state.load_state(lite.rdir)["status"] == "lite-failed"
