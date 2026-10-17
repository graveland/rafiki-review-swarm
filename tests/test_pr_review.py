# review-swarm: the PR review stage - lens x file-group cells, the panel, and the pools
"""The contract these tests pin down: every seat is handed packet/files.txt
verbatim and in order (that order is the shared prompt-cache prefix), finder
cells multiply by gather's file groups, the panel gets the always-on lens list
and its mode's extra rules, and the pools carry the seat that produced each
record. A hallucinated reference or a breached instruction is friction, not a
dropped result: the verifiers decide what is real, never this stage. The fake
stands in for the daemon and replays scripted answers.
"""

import hashlib
import json
from types import SimpleNamespace

import pytest

from review_swarm import pr_review, runs
from review_swarm.common import AtCap, Fail
from tests.conftest import FakeRafiki

FILES = ["packet/intent.md", "packet/diff.patch", "packet/threads.md", "packet/lenses.json",
         "tree/a.py", "tree/b.py:1-10"]
LENSES = [
    {"name": "liveness", "category": "defect", "prompt": "LIVENESS lens body.", "always": True},
    {"name": "stale-state", "category": "quality", "prompt": "STALE STATE lens body.", "always": False},
]
GROUPS = [{"name": "alpha", "paths": ["a.py", "a_test.py"]}, {"name": "beta", "paths": ["b.py"]}]
PANEL = ["review-pr:panel-1", "review-pr:panel-2"]
SEATS = {"review-pr:finder": {"kind": "fundi"},
         "review-pr:panel-1": {"kind": "claude"},   # cannot be pre-filled: reads the packet
         "review-pr:panel-2": {"kind": "fundi"}}

FINDER_HIT = """Answer.

```json
{"findings": [{"title": "Race  on the counter", "category": "defect", "file": "a.py", "line": 3,
  "severity": "S2", "claim": "two writers lose an increment", "evidence": "`counter += 1` at a.py:3",
  "repro": "two concurrent requests"}],
 "process_notes": ["checked the callers of a.py"]}
```"""
FINDER_EMPTY = '```json\n{"findings": [], "process_notes": []}\n```'
FINDER_GHOST = """```json
{"findings": [{"title": "Ghost file", "category": "defect", "file": "ghost.py", "line": 1,
   "severity": "S2", "claim": "x", "evidence": "y", "repro": "z"},
  {"title": "Far line", "category": "defect", "file": "a.py", "line": 9999,
   "severity": "S2", "claim": "x", "evidence": "y", "repro": "z"}],
 "process_notes": []}
```"""
PANEL_HIT = """```json
{"checklist": [{"lens": "liveness", "answer": "findings",
   "reason": "the queue at a.py:20"}],
 "findings": [{"lens": "liveness", "title": "Unbounded queue", "category": "quality", "file": "a.py", "line": 20,
   "severity": "S3", "claim": "the queue grows without bound", "evidence": "a.py:20 appends",
   "repro": "keep posting"}],
 "process_notes": ["the lock is at a.py:10"]}
```"""
PANEL_EMPTY_OK = """```json
{"checklist": [{"lens": "liveness", "answer": "nothing",
   "reason": "a.py:12 raises before the state is written"}],
 "findings": [], "process_notes": []}
```"""
PANEL_DESIGN = """```json
{"checklist": [{"lens": "liveness", "answer": "nothing",
   "reason": "the design adds no new blocking wait"}],
 "findings": [], "questions": ["What happens to in-flight reads on restart?"], "process_notes": []}
```"""
PANEL_DELTA = """```json
{"checklist": [{"lens": "liveness", "answer": "nothing",
   "reason": "a.py:12 still raises before the state is written"}],
 "findings": [], "prior": [{"id": "abc123", "status": "not-fixed",
   "reason": "a.py:7 still reads the stale field"}], "process_notes": []}
```"""
PANEL_MISSING_LENS = """```json
{"checklist": [{"lens": "stale-state", "answer": "nothing", "reason": "a.py:9 refreshes it"}],
 "findings": [], "process_notes": []}
```"""
PANEL_EMPTY_REASON = """```json
{"checklist": [{"lens": "liveness", "answer": "nothing", "reason": "   "}],
 "findings": [], "process_notes": []}
```"""
# A finding row that is not an object at all, and a line that is not a number.
FINDER_JUNK = """```json
{"findings": ["not an object", {"title": "Bad line", "category": "defect", "file": "a.py",
   "line": "x", "severity": "S2", "claim": "x", "evidence": "y", "repro": "z"}],
 "process_notes": []}
```"""
# Same for a checklist row: the lens it should have answered goes unanswered.
PANEL_JUNK = """```json
{"checklist": ["liveness", {"lens": "liveness", "answer": "nothing",
   "reason": "a.py:1 raises first"}], "findings": [], "process_notes": []}
```"""

# A panel finding that names no lens: the schema asks for one, so it pools with
# "" and backs no "findings" checklist answer.
PANEL_NO_LENS = """```json
{"checklist": [{"lens": "liveness", "answer": "nothing",
   "reason": "a.py:12 raises first"}],
 "findings": [{"title": "Unanchored to a lens", "category": "quality", "file": "a.py", "line": 20,
   "severity": "S3", "claim": "x", "evidence": "y", "repro": "z"}],
 "process_notes": []}
```"""

# A finding with no anchor at all: the schema promises a line.
FINDER_NO_LINE = """```json
{"findings": [{"title": "No anchor", "category": "defect", "file": "a.py",
   "severity": "S2", "claim": "x", "evidence": "y", "repro": "z"}],
 "process_notes": []}
```"""

# Next.js-style routes: a fundi pre-fill would glob the bracket path (and match
# nothing), while a claude seat reads it literally.
GLOB_FILE = "tree/apps/studio/pages/project/[ref]/settings.tsx"
HETERO_FILES = ["packet/intent.md", "packet/diff.patch", GLOB_FILE, "tree/a.py:1-10"]


class RefusingFake(FakeRafiki):
    """FakeRafiki's refuse_next refuses from the first spawn on, which is the
    hard-Fail arm of the cap rule. The requeue arm needs the refusal to land
    while our own children are still live, so this one refuses once, when
    `refuse_when_live` of them exist."""

    def __init__(self, refuse_when_live):
        super().__init__()
        self.refuse_when_live = refuse_when_live
        self.refused = False

    def _accept(self, name, labels):
        if not self.refused and len(self._children) >= self.refuse_when_live:
            self.refused = True
            raise AtCap("live agent(s) already running")
        return super()._accept(name, labels)


class FailingFake(FakeRafiki):
    """Fails one cell's spawn with a non-cap Fail, the way a mis-set preset
    would: the stage must record it and keep the other seats' answers."""

    def __init__(self, fail_cell):
        super().__init__()
        self.fail_cell = fail_cell

    def spawn_seat(self, name, *, preset, cwd, packet_files, instruction, labels, model=None):
        if name.endswith(self.fail_cell):
            raise Fail(f"preset {preset}: kind 'script' is not a review seat kind")
        return super().spawn_seat(name, preset=preset, cwd=cwd, packet_files=packet_files,
                                  instruction=instruction, labels=labels, model=model)


class WatchingFake(FakeRafiki):
    """Records what the run said about itself at the moment each child was stopped
    (its status, and whether the claim file was still held), and can lose the
    daemon after N child_info calls: a Fail mid-stage, children already live."""

    def __init__(self, rdir, fail_child_info_after=None):
        super().__init__()
        self.rdir = rdir
        self.fail_child_info_after = fail_child_info_after
        self.info_calls = 0
        self.seen = []

    def child_info(self, child_id):
        self.info_calls += 1
        if self.fail_child_info_after is not None and self.info_calls > self.fail_child_info_after:
            raise Fail("child_info: daemon gone")
        return super().child_info(child_id)

    def stop(self, child_id):
        self.seen.append((json.loads((self.rdir / "state.json").read_text())["status"],
                          (self.rdir / "review.lock").exists()))
        super().stop(child_id)


class CrashingFake(FakeRafiki):
    """A non-Fail exception partway through the stage."""

    def export(self, child_id):
        raise OSError("daemon went away")


class DeadDaemonFake(FakeRafiki):
    """The daemon is gone by the time the first child settles: `stop` fails for
    every child, so the stage notices the loss at the stop it makes after
    collecting that child, and every stop the unwind then makes fails too."""

    def stop(self, child_id):
        self.stops.append(child_id)
        raise Fail(f"stop {child_id}: daemon unreachable")


def args(**kw):
    return SimpleNamespace(**{"run": "20260101-000000", "timeout": 1800, "model": None, "parallel": 24, **kw})


# The wide fan-out, so the tests below pin the machinery each setting widens;
# the lean defaults have their own tests (fan={}).
WIDE = {"finders": "all", "split_groups": True, "verify_claims": True}


def make_run(tmp_path, *, mode="pr", status="confirmed", lenses=None, groups=None, panel=None,
             packet=None, fan=WIDE):
    """A PR run directory as gather leaves it: run.json, the project's pr.json,
    the packet, and one 40-line tree file to cite."""
    home = runs.Home(tmp_path)
    rdir = home.runs / "proj" / "20260101-000000"
    for d in ("project", "packet", "tree"):
        (rdir / d).mkdir(parents=True)
    (rdir / "tree" / "a.py").write_text("".join(f"line {i}\n" for i in range(1, 41)))
    (rdir / "run.json").write_text(json.dumps(
        {"run_id": "20260101-000000", "project": "proj", "mode": mode, "kind": "pr",
         "tree": str(rdir / "tree"), "files_changed": ["a.py"]}))
    (rdir / "project" / "pr.json").write_text(json.dumps(
        {"confirm_over_tokens": 150000, "panel": PANEL if panel is None else panel, "verifiers": {},
         "finder": "review-pr:finder", "gather": "review-pr:gather", "lens_dir": None, **fan}))
    (rdir / "packet" / "files.txt").write_text("\n".join(FILES if packet is None else packet) + "\n")
    (rdir / "packet" / "lenses.json").write_text(json.dumps(
        {"lenses": LENSES if lenses is None else lenses}))
    (rdir / "packet" / "context.json").write_text(json.dumps(
        {"context": [], "file_groups": groups or []}))
    (rdir / "state.json").write_text(json.dumps({"status": status}))
    return home, rdir


def scripted(fake, **answers):
    fake.answers = {f"review-01-{k.replace('_', '-')}": v for k, v in answers.items()}


def lines(path):
    return [json.loads(l) for l in path.open()] if path.exists() else None


def friction(rdir):
    return lines(rdir / "friction.jsonl")


def test_every_seat_receives_the_packet_in_order(fake_rafiki, tmp_path):
    fake = fake_rafiki
    fake.presets = SEATS
    home, rdir = make_run(tmp_path)
    scripted(fake, finder_liveness=FINDER_HIT, finder_stale_state=FINDER_EMPTY,
             panel_1=PANEL_HIT, panel_2=PANEL_EMPTY_OK)

    out = pr_review.stage_review(fake, home, args())

    assert [s["packet_files"] for s in fake.seat_spawns] == [FILES] * 4
    # cwd is the run directory on both delivery paths: every packet path is
    # relative to it (packet/... and tree/...).
    assert [s["cwd"] for s in fake.seat_spawns] == [str(rdir)] * 4
    assert {s["preset"] for s in fake.seat_spawns} == set(SEATS)
    assert "the whole change" in fake.seat_spawns[0]["instruction"]
    assert out == {"stage": "review", "run": "20260101-000000", "attempt": "01", "children": 4,
                   "candidates": 2, "parse_failures": 0}
    assert json.loads((rdir / "state.json").read_text())["status"] == "verifying"


def test_candidates_pool_with_id_seat_and_source(fake_rafiki, tmp_path):
    fake = fake_rafiki
    fake.presets = SEATS
    home, rdir = make_run(tmp_path)
    scripted(fake, finder_liveness=FINDER_HIT, finder_stale_state=FINDER_EMPTY,
             panel_1=PANEL_HIT, panel_2=PANEL_EMPTY_OK)
    pr_review.stage_review(fake, home, args())

    rows = lines(rdir / "review" / "01" / "candidates.jsonl")
    by_source = {r["source"]: r for r in rows}
    # The id is the anchor plus the title with its whitespace collapsed and
    # lowercased, so two seats raising one issue share it.
    assert by_source["finder:liveness"]["id"] == hashlib.sha256(
        b"a.py\x003\x00race on the counter").hexdigest()[:12]
    assert by_source["finder:liveness"]["raised_by"] == "review-pr:finder"
    assert by_source["panel"]["raised_by"] == "review-pr:panel-1"
    assert by_source["panel"]["id"] == hashlib.sha256(
        b"a.py\x0020\x00unbounded queue").hexdigest()[:12]
    # A finding carries the always-on lens it serves: a panel seat names it (the
    # panel schema has the field), a finder cell is the lens itself. The pooled
    # lens is what a panel "findings" checklist answer is checked against later.
    assert by_source["panel"]["lens"] == "liveness"
    assert by_source["finder:liveness"]["lens"] == "liveness"
    # Every pooled line is sort_keys: the attempt's bytes must not depend on the
    # order children settled in.
    for r in rows:
        assert list(r) == sorted(r)

    checklist = lines(rdir / "review" / "01" / "checklist.jsonl")
    assert checklist == [
        {"lens": "liveness", "answer": "findings", "reason": "the queue at a.py:20",
         "raised_by": "review-pr:panel-1"},
        {"lens": "liveness", "answer": "nothing",
         "reason": "a.py:12 raises before the state is written", "raised_by": "review-pr:panel-2"}]


def test_a_panel_finding_names_the_lens_it_serves(fake_rafiki, tmp_path):
    # F5: a panel seat names the always-on lens each finding serves, and the lens
    # is pooled with the finding, so a "findings" checklist answer has something
    # to be checked against. A finding that names no lens pools as "" and backs
    # no answer.
    fake = fake_rafiki
    fake.presets = SEATS
    home, rdir = make_run(tmp_path)
    scripted(fake, finder_liveness=FINDER_HIT, finder_stale_state=FINDER_EMPTY,
             panel_1=PANEL_HIT, panel_2=PANEL_NO_LENS)
    pr_review.stage_review(fake, home, args())

    by_title = {r["title"]: r for r in lines(rdir / "review" / "01" / "candidates.jsonl")}
    assert by_title["Unbounded queue"]["lens"] == "liveness"
    assert by_title["Race  on the counter"]["lens"] == "liveness"
    assert by_title["Unanchored to a lens"]["lens"] == ""


def test_lens_cells_multiply_by_file_groups(fake_rafiki, tmp_path):
    fake = fake_rafiki
    fake.presets = SEATS
    home, rdir = make_run(tmp_path, groups=GROUPS)
    cells = ["finder-liveness--1-alpha", "finder-liveness--2-beta",
             "finder-stale-state--1-alpha", "finder-stale-state--2-beta"]
    fake.answers = {f"review-01-{c}": FINDER_EMPTY for c in cells}
    scripted(fake, panel_1=PANEL_EMPTY_OK, panel_2=PANEL_EMPTY_OK)

    out = pr_review.stage_review(fake, home, args())

    assert out["children"] == 6
    finders = [s for s in fake.seat_spawns if s["preset"] == "review-pr:finder"]
    assert [s["labels"]["review/cell"] for s in finders] == cells
    assert [s["labels"]["review/lens"] for s in finders] == ["liveness", "liveness",
                                                             "stale-state", "stale-state"]
    # The scope names the group's paths and the lens's own text rides with it.
    assert "a.py, a_test.py" in finders[0]["instruction"]
    assert "LIVENESS lens body." in finders[0]["instruction"]
    assert "b.py" in finders[1]["instruction"]
    assert "STALE STATE lens body." in finders[2]["instruction"]
    for c in cells:
        assert (rdir / "review" / "01" / c / "task.md").exists()


def test_lean_fanout_runs_one_finder_per_triggered_lens_over_the_whole_change(fake_rafiki, tmp_path):
    # The defaults: the always-on liveness lens is the panel's to answer, and the
    # file groups gather proposed do not multiply the one triggered lens.
    fake = fake_rafiki
    fake.presets = SEATS
    home, _ = make_run(tmp_path, groups=GROUPS, fan={})
    scripted(fake, panel_1=PANEL_EMPTY_OK, panel_2=PANEL_EMPTY_OK)
    fake.answers["review-01-finder-stale-state"] = FINDER_EMPTY

    out = pr_review.stage_review(fake, home, args())

    assert out["children"] == 3
    finders = [s for s in fake.seat_spawns if s["preset"] == "review-pr:finder"]
    assert [s["labels"]["review/cell"] for s in finders] == ["finder-stale-state"]
    assert "the whole change" in finders[0]["instruction"]


def test_a_bad_fanout_setting_is_refused(fake_rafiki, tmp_path):
    fake_rafiki.presets = SEATS
    home, _ = make_run(tmp_path, fan={"finders": "some"})
    with pytest.raises(Fail, match="finders must be"):
        pr_review.stage_review(fake_rafiki, home, args())


def test_panel_gets_the_always_lenses_and_the_mode_rules(fake_rafiki, tmp_path):
    fake = fake_rafiki
    fake.presets = SEATS
    home, rdir = make_run(tmp_path, mode="design")
    scripted(fake, finder_liveness=FINDER_EMPTY, finder_stale_state=FINDER_EMPTY,
             panel_1=PANEL_DESIGN, panel_2=PANEL_EMPTY_OK)
    pr_review.stage_review(fake, home, args())

    panels = [s["instruction"] for s in fake.seat_spawns if s["preset"].startswith("review-pr:panel")]
    for text in panels:
        assert "MODE: design" in text and "- liveness" in text
        assert "stale-state" not in text   # only the always-on lenses are the checklist
        assert "Add every question the design must answer before implementation to `questions`." in text
    assert lines(rdir / "review" / "01" / "questions.jsonl") == [
        {"question": "What happens to in-flight reads on restart?", "raised_by": "review-pr:panel-1"}]


def test_delta_mode_panel_rejudges_prior(fake_rafiki, tmp_path):
    fake = fake_rafiki
    fake.presets = SEATS
    home, rdir = make_run(tmp_path, mode="delta")
    scripted(fake, finder_liveness=FINDER_EMPTY, finder_stale_state=FINDER_EMPTY,
             panel_1=PANEL_DELTA, panel_2=PANEL_EMPTY_OK)
    pr_review.stage_review(fake, home, args())

    panels = [s["instruction"] for s in fake.seat_spawns if s["preset"].startswith("review-pr:panel")]
    assert all("Re-judge each prior finding in packet/prior.json" in t for t in panels)
    assert lines(rdir / "review" / "01" / "prior.jsonl") == [
        {"id": "abc123", "status": "not-fixed", "reason": "a.py:7 still reads the stale field",
         "raised_by": "review-pr:panel-1"}]


def test_pr_mode_adds_no_panel_rules_and_writes_no_prior_or_questions(fake_rafiki, tmp_path):
    fake = fake_rafiki
    fake.presets = SEATS
    home, rdir = make_run(tmp_path, mode="pr")
    scripted(fake, finder_liveness=FINDER_EMPTY, finder_stale_state=FINDER_EMPTY,
             panel_1=PANEL_EMPTY_OK, panel_2=PANEL_EMPTY_OK)
    pr_review.stage_review(fake, home, args())

    panels = [s["instruction"] for s in fake.seat_spawns if s["preset"].startswith("review-pr:panel")]
    assert all("Re-judge each prior finding" not in t for t in panels)
    assert all("claims and proposed mechanisms" not in t for t in panels)
    adir = rdir / "review" / "01"
    assert not (adir / "questions.jsonl").exists() and not (adir / "prior.jsonl").exists()


def test_missing_lens_and_empty_reason_are_instruction_breaches(fake_rafiki, tmp_path):
    fake = fake_rafiki
    fake.presets = SEATS
    home, rdir = make_run(tmp_path)
    scripted(fake, finder_liveness=FINDER_EMPTY, finder_stale_state=FINDER_EMPTY,
             panel_1=PANEL_MISSING_LENS, panel_2=PANEL_EMPTY_REASON)
    pr_review.stage_review(fake, home, args())

    breaches = [e for e in friction(rdir) if e["kind"] == "instruction-breach"]
    assert sorted((e["cell"], e["note"]) for e in breaches) == [
        ("panel-1", "no checklist answer for lens liveness"),
        ("panel-2", "lens liveness: answered nothing with no reason")]
    assert {e["seat"] for e in breaches} == {"review-pr:panel-1", "review-pr:panel-2"}


def test_hallucinated_reference_is_friction_not_a_dropped_finding(fake_rafiki, tmp_path):
    fake = fake_rafiki
    fake.presets = SEATS
    home, rdir = make_run(tmp_path)
    scripted(fake, finder_liveness=FINDER_GHOST, finder_stale_state=FINDER_EMPTY,
             panel_1=PANEL_EMPTY_OK, panel_2=PANEL_EMPTY_OK)

    out = pr_review.stage_review(fake, home, args())

    hits = [e["note"] for e in friction(rdir) if e["kind"] == "hallucination"]
    assert hits == ["ghost.py:1 Ghost file: file is not in tree/",
                    "a.py:9999 Far line: line is out of range"]
    # Recorded, not dropped: refuting it is the verifiers' job.
    assert out["candidates"] == 2


def test_a_non_numeric_line_is_normalized_not_a_crash(fake_rafiki, tmp_path):
    fake = fake_rafiki
    fake.presets = SEATS
    home, rdir = make_run(tmp_path)
    bad_line = """```json
{"findings": [{"title": "Bad line", "category": "defect", "file": "a.py",
  "line": "a.py:3", "severity": "S2", "claim": "x", "evidence": "y", "repro": "z"}],
 "process_notes": []}
```"""
    scripted(fake, finder_liveness=bad_line, finder_stale_state=FINDER_EMPTY,
             panel_1=PANEL_EMPTY_OK, panel_2=PANEL_EMPTY_OK)

    out = pr_review.stage_review(fake, home, args())

    # The pool promises an int line (the reference check and the later
    # clustering both int() it), so the model's text is normalized, and the
    # record still pools with the anchor it does have. The coercion is friction:
    # a silently zeroed line would hide a broken answer.
    (row,) = lines(rdir / "review" / "01" / "candidates.jsonl")
    assert row["line"] == 0 and out["parse_failures"] == 0
    assert [(e["kind"], e["note"]) for e in friction(rdir)] == [
        ("contract-violation", "a.py:0 Bad line: line 'a.py:3' is not a number; pooled as 0"),
        ("hallucination", "a.py:0 Bad line: line is out of range")]


def test_rows_the_pool_cannot_carry_are_recorded(fake_rafiki, tmp_path):
    fake = fake_rafiki
    fake.presets = SEATS
    home, rdir = make_run(tmp_path)
    scripted(fake, finder_liveness=FINDER_JUNK, finder_stale_state=FINDER_EMPTY,
             panel_1=PANEL_JUNK, panel_2=PANEL_EMPTY_OK)

    pr_review.stage_review(fake, home, args())

    # Nothing is dropped quietly: the junk rows are named. The lens stays
    # answered because the row behind the junk one is well formed, so no breach
    # is invented for it either.
    assert [(e["cell"], e["kind"]) for e in friction(rdir)] == [
        ("finder-liveness", "contract-violation"), ("finder-liveness", "contract-violation"),
        ("finder-liveness", "hallucination"), ("panel-1", "contract-violation")]
    notes = [e["note"] for e in friction(rdir)]
    assert "findings[0]: str is not an object; dropped" in notes
    assert "checklist[0]: str is not an object; dropped" in notes
    assert [r for r in lines(rdir / "review" / "01" / "checklist.jsonl")
            if r["raised_by"] == "review-pr:panel-1"] == [
        {"lens": "liveness", "answer": "nothing", "reason": "a.py:1 raises first",
         "raised_by": "review-pr:panel-1"}]
    assert len(lines(rdir / "review" / "01" / "candidates.jsonl")) == 1


def test_a_glob_metacharacter_path_leaves_every_seat_s_packet(fake_rafiki, tmp_path):
    fake = fake_rafiki
    fake.presets = SEATS
    home, rdir = make_run(tmp_path, packet=HETERO_FILES)
    scripted(fake, finder_liveness=FINDER_EMPTY, finder_stale_state=FINDER_EMPTY,
             panel_1=PANEL_EMPTY_OK, panel_2=PANEL_EMPTY_OK)

    pr_review.stage_review(fake, home, args())

    # A fundi pre-fill globs a path containing *?[{ (and a glob matching nothing
    # is fatal for the child) while a claude seat reads it literally, so the
    # entry cannot mean one file in both modes: it is out of both lists, in the
    # order gather wrote. The drop is friction, naming the path.
    kept = ["packet/intent.md", "packet/diff.patch", "tree/a.py:1-10"]
    assert [s["packet_files"] for s in fake.seat_spawns] == [kept] * 4
    drops = [e for e in friction(rdir) if e["kind"] == "packet-drop"]
    assert [(e["seat"], e["note"].split(":")[0]) for e in drops] == [("packet", GLOB_FILE)]
    assert "pre-fill expands as a glob" in drops[0]["note"]


def test_a_spawn_failure_is_friction_and_its_lenses_are_unverified(fake_rafiki, tmp_path):
    fake = FailingFake("panel-2")
    fake.presets = SEATS
    home, rdir = make_run(tmp_path)
    scripted(fake, finder_liveness=FINDER_HIT, finder_stale_state=FINDER_EMPTY,
             panel_1=PANEL_HIT, panel_2=PANEL_EMPTY_OK)

    out = pr_review.stage_review(fake, home, args())

    assert out["children"] == 3
    assert [(e["cell"], e["kind"]) for e in friction(rdir) if e["kind"] == "spawn-error"] == [
        ("panel-2", "spawn-error")]
    # Its always-on lenses have no claim, and the stage says so per lens: an
    # absent panel seat must not read as a seat that answered everything.
    assert [(e["cell"], e["note"]) for e in friction(rdir)
            if e["kind"] == "instruction-breach"] == [
        ("panel-2", "no checklist answer for lens liveness: the seat produced no answer")]
    assert (rdir / "review" / "01" / "panel-2" / "spawn-error.txt").exists()
    assert lines(rdir / "review" / "01" / "checklist.jsonl") == [
        {"lens": "liveness", "answer": "findings", "reason": "the queue at a.py:20",
         "raised_by": "review-pr:panel-1"}]
    assert json.loads((rdir / "state.json").read_text())["status"] == "verifying"


def test_process_notes_and_delivery_are_recorded(fake_rafiki, tmp_path):
    fake = fake_rafiki
    fake.presets = SEATS
    home, rdir = make_run(tmp_path)
    scripted(fake, finder_liveness=FINDER_HIT, finder_stale_state=FINDER_EMPTY,
             panel_1=PANEL_HIT, panel_2=PANEL_EMPTY_OK)
    pr_review.stage_review(fake, home, args())

    notes = {(e["cell"], e["note"]) for e in friction(rdir) if e["kind"] == "self"}
    assert ("finder-liveness", "checked the callers of a.py") in notes
    assert ("panel-1", "the lock is at a.py:10") in notes
    adir = rdir / "review" / "01"
    assert json.loads((adir / "finder-liveness" / "child.json").read_text())["delivery"] == "prefill"
    assert json.loads((adir / "panel-1" / "child.json").read_text())["delivery"] == "read-list"
    # Every file whose bytes may be compared is sort_keys, child.json included.
    for name in ("finder-liveness", "panel-1"):
        m = json.loads((adir / name / "child.json").read_text())
        assert list(m) == sorted(m)
    assert json.loads((adir / "children.json").read_text()).keys() == {
        "finder-liveness", "finder-stale-state", "panel-1", "panel-2"}


def test_an_unparseable_answer_is_a_parse_error(fake_rafiki, tmp_path):
    fake = fake_rafiki
    fake.presets = SEATS
    home, rdir = make_run(tmp_path)
    scripted(fake, finder_liveness="no fenced block at all", finder_stale_state=FINDER_EMPTY,
             panel_1=PANEL_HIT, panel_2=PANEL_EMPTY_OK)

    out = pr_review.stage_review(fake, home, args())

    cdir = rdir / "review" / "01" / "finder-liveness"
    assert (cdir / "parse-error.txt").exists() and not (cdir / "result.json").exists()
    assert (cdir / "child.json").exists() and (cdir / "raw.md").read_text() == "no fenced block at all"
    assert out["parse_failures"] == 1 and out["candidates"] == 1
    assert [(e["cell"], e["kind"]) for e in friction(rdir)] == [
        ("finder-liveness", "parse-error"), ("panel-1", "self")]
    # Tagged with the attempt, so grading a rerun never counts this one.
    assert {(e["stage"], e["attempt"]) for e in friction(rdir)} == {("review", "01")}


def test_a_refused_spawn_is_retried_not_counted_as_a_result(tmp_path):
    fake = RefusingFake(refuse_when_live=2)
    fake.presets = SEATS
    home, rdir = make_run(tmp_path)
    scripted(fake, finder_liveness=FINDER_HIT, finder_stale_state=FINDER_EMPTY,
             panel_1=PANEL_HIT, panel_2=PANEL_EMPTY_OK)

    out = pr_review.stage_review(fake, home, args())

    assert fake.refused                       # the requeue arm, not the hard-Fail one
    assert len(fake.seat_spawns) == 4          # the refusal spawned nothing
    assert out["children"] == 4 and out["parse_failures"] == 0
    assert len(lines(rdir / "review" / "01" / "candidates.jsonl")) == 2


def test_a_refusal_with_nothing_of_ours_running_fails(tmp_path):
    fake = RefusingFake(refuse_when_live=0)
    fake.presets = SEATS
    home, rdir = make_run(tmp_path)

    with pytest.raises(Fail, match="nothing of ours running"):
        pr_review.stage_review(fake, home, args())

    assert fake.seat_spawns == []
    # A refused stage leaves the run re-runnable: the status it claimed goes
    # back to confirmed.
    assert json.loads((rdir / "state.json").read_text())["status"] == "confirmed"


def test_a_second_review_is_refused_while_one_holds_the_claim(fake_rafiki, tmp_path):
    fake = fake_rafiki
    fake.presets = SEATS
    home, rdir = make_run(tmp_path)
    (rdir / "review.lock").write_text(json.dumps(
        {"attempt": "01", "pid": 4242, "started": "2026-01-01T00:00:00+00:00"}, sort_keys=True) + "\n")

    # Two invocations that both read `confirmed` cannot both claim the run: the
    # second one is refused before it creates an attempt or spawns anything, and
    # the holder's claim is left alone.
    with pytest.raises(Fail, match="review is already running"):
        pr_review.stage_review(fake, home, args())

    assert fake.seat_spawns == []
    assert not (rdir / "review").exists()
    assert json.loads((rdir / "state.json").read_text())["status"] == "confirmed"
    assert (rdir / "review.lock").exists()


def test_the_status_is_read_after_the_claim(fake_rafiki, tmp_path, monkeypatch):
    """A review that finished at `verifying` between this invocation's start and
    its claim must be seen: the status is read while holding the claim, so the
    second invocation refuses instead of running the review a second time."""
    fake = fake_rafiki
    fake.presets = SEATS
    home, rdir = make_run(tmp_path)  # confirmed
    real_claim = pr_review.claim_review

    def another_review_finishes(rdir_):
        (rdir_ / "state.json").write_text(json.dumps({"status": "verifying"}, sort_keys=True) + "\n")
        real_claim(rdir_)

    monkeypatch.setattr(pr_review, "claim_review", another_review_finishes)

    with pytest.raises(Fail, match="run is verifying; confirm it first"):
        pr_review.stage_review(fake, home, args())

    assert fake.seat_spawns == []                 # no second review of the same run
    assert not (rdir / "review").exists()
    assert not (rdir / "review.lock").exists()    # the refusal gave the claim back
    assert json.loads((rdir / "state.json").read_text())["status"] == "verifying"


def test_a_clean_finish_releases_the_claim(tmp_path):
    home, rdir = make_run(tmp_path)
    fake = WatchingFake(rdir)
    fake.presets = SEATS
    scripted(fake, finder_liveness=FINDER_HIT, finder_stale_state=FINDER_EMPTY,
             panel_1=PANEL_HIT, panel_2=PANEL_EMPTY_OK)

    out = pr_review.stage_review(fake, home, args())

    assert out["children"] == 4
    # The claim is held for as long as the stage runs, and every child was stopped
    # while the run still said the review was running.
    assert {s for s, _ in fake.seen} == {"reviewing"}
    assert all(held for _, held in fake.seen)
    assert not (rdir / "review.lock").exists()
    assert json.loads((rdir / "state.json").read_text())["status"] == "verifying"


def test_a_refused_review_stops_its_live_children_before_the_status_moves(tmp_path):
    home, rdir = make_run(tmp_path)
    fake = WatchingFake(rdir, fail_child_info_after=2)  # the daemon drops once two children agree
    fake.presets = SEATS
    scripted(fake, finder_liveness=FINDER_HIT, finder_stale_state=FINDER_EMPTY,
             panel_1=PANEL_HIT, panel_2=PANEL_EMPTY_OK)

    with pytest.raises(Fail, match="daemon gone"):
        pr_review.stage_review(fake, home, args())

    # Stop first, status second: every stop saw `reviewing` and the claim held.
    assert fake.seen and {s for s, _ in fake.seen} == {"reviewing"}
    assert all(held for _, held in fake.seen)
    assert set(fake.stops) == {"c_1", "c_2", "c_3", "c_4"}
    assert json.loads((rdir / "state.json").read_text())["status"] == "confirmed"
    assert not (rdir / "review.lock").exists()


def test_an_unexpected_exception_leaves_a_defined_status_and_records_it(tmp_path):
    home, rdir = make_run(tmp_path)
    fake = CrashingFake()
    fake.presets = SEATS
    scripted(fake, finder_liveness=FINDER_HIT, finder_stale_state=FINDER_EMPTY,
             panel_1=PANEL_HIT, panel_2=PANEL_EMPTY_OK)

    with pytest.raises(OSError, match="daemon went away"):
        pr_review.stage_review(fake, home, args())

    # Not a Fail, so the run records it: a rerun can tell what happened from
    # friction, and the status is one it can act on rather than a stranded
    # `reviewing`.
    assert [(e["seat"], e["kind"], e["note"]) for e in friction(rdir)] == [
        ("review-pr", "stage-error", "OSError: daemon went away")]
    assert json.loads((rdir / "state.json").read_text())["status"] == "confirmed"
    assert not (rdir / "review.lock").exists()
    assert set(fake.stops) == {"c_1", "c_2", "c_3", "c_4"}


def test_a_dead_daemon_still_records_why_and_gives_the_claim_back(tmp_path):
    home, rdir = make_run(tmp_path)
    fake = DeadDaemonFake()
    fake.presets = SEATS
    scripted(fake, finder_liveness=FINDER_HIT, finder_stale_state=FINDER_EMPTY,
             panel_1=PANEL_HIT, panel_2=PANEL_EMPTY_OK)

    with pytest.raises(Fail, match="stop c_1: daemon unreachable"):
        pr_review.stage_review(fake, home, args())

    # The daemon that raised is the daemon that cannot stop a child, so the unwind
    # must not give up at the first refused stop: the run gets the reason, a record
    # naming every child that may still be live, its status back and its claim back.
    # c_1 is the stop collect_child made; the other four are the unwind's own.
    assert sorted(fake.stops) == ["c_1", "c_1", "c_2", "c_3", "c_4"]
    rec = friction(rdir)
    assert (rec[0]["seat"], rec[0]["kind"], rec[0]["note"]) == (
        "review-pr", "stage-error", "Fail: stop c_1: daemon unreachable")
    assert sorted((e["kind"], e["note"]) for e in rec[1:]) == [
        ("stop-failed", "finder-liveness: c_1: Fail: stop c_1: daemon unreachable"),
        ("stop-failed", "finder-stale-state: c_2: Fail: stop c_2: daemon unreachable"),
        ("stop-failed", "panel-1: c_3: Fail: stop c_3: daemon unreachable"),
        ("stop-failed", "panel-2: c_4: Fail: stop c_4: daemon unreachable")]
    # Never stranded at `reviewing` with the lock held and no trace of why.
    assert json.loads((rdir / "state.json").read_text())["status"] == "confirmed"
    assert not (rdir / "review.lock").exists()


def test_a_missing_line_is_friction(fake_rafiki, tmp_path):
    fake = fake_rafiki
    fake.presets = SEATS
    home, rdir = make_run(tmp_path)
    scripted(fake, finder_liveness=FINDER_NO_LINE, finder_stale_state=FINDER_EMPTY,
             panel_1=PANEL_EMPTY_OK, panel_2=PANEL_EMPTY_OK)

    pr_review.stage_review(fake, home, args())

    # A line the schema promised and the model left out is pooled as 0 either
    # way, so the friction is the only record that the anchor was missing.
    assert [(e["kind"], e["note"]) for e in friction(rdir)] == [
        ("contract-violation", "a.py:0 No anchor: no line given; pooled as 0"),
        ("hallucination", "a.py:0 No anchor: line is out of range")]


def test_a_run_that_is_not_confirmed_refuses(fake_rafiki, tmp_path):
    fake = fake_rafiki
    fake.presets = SEATS
    home, rdir = make_run(tmp_path, status="gathering")

    with pytest.raises(Fail, match="run is gathering; confirm it first"):
        pr_review.stage_review(fake, home, args())

    assert fake.seat_spawns == []
    assert not (rdir / "review").exists()
    assert json.loads((rdir / "state.json").read_text())["status"] == "gathering"


def test_a_misshapen_file_group_is_a_fail_not_a_crash(fake_rafiki, tmp_path):
    fake = fake_rafiki
    fake.presets = SEATS
    home, rdir = make_run(tmp_path, groups=["a.py", "b.py"])

    # g.get on a list element would be an AttributeError halfway through the
    # spawns, stranding the run in reviewing; the file is named instead.
    with pytest.raises(Fail, match="file_groups must be a list of objects"):
        pr_review.stage_review(fake, home, args())

    assert fake.seat_spawns == []
    assert json.loads((rdir / "state.json").read_text())["status"] == "confirmed"
