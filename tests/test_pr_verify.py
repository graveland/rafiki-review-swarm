# review-swarm: PR cross-validation - clustering, verifier assignment and verdicts
"""The rules these tests pin down are the ones that decide what a review shows:
no issue is dropped, none is verified by the seat that raised it, none is called
confirmed while a vote refutes it, and a bucket never lets one issue's verdict
stand in for another's. The pure functions come first, then the stage over
FakeRafiki and a run directory built here.
"""

import json
import os
from types import SimpleNamespace

import pytest

from review_swarm import pr_review, pr_verify
from review_swarm.common import AtCap, Fail
from review_swarm.runs import Home

RUN_ID = "20260101-000000"
ATTEMPT = "01"
PANEL = ["review-pr:panel-1", "review-pr:panel-2", "review-pr:panel-3"]
VERIFIERS = {"review-pr:panel-1": "review-pr:verify-1",
             "review-pr:panel-2": "review-pr:verify-2",
             "review-pr:panel-3": "review-pr:verify-3"}
FAMILIES = {"review-pr:panel-1": "anthropic", "review-pr:panel-2": "z-ai",
            "review-pr:panel-3": "openai"}
# The kinds and models the checkout's review-pr seat files carry: the families
# the stage reads live must come out as FAMILIES.
PRESETS = {
    "review-pr:finder": {"kind": "fundi", "model": "openrouter/z-ai/glm-5.3-flash"},
    "review-pr:panel-1": {"kind": "claude", "model": "claude-opus-5-5"},
    "review-pr:panel-2": {"kind": "fundi", "model": "openrouter/z-ai/glm-5.3-flash"},
    "review-pr:panel-3": {"kind": "fundi", "model": "openrouter/openai/gpt-5.6-sol"},
    "review-pr:verify-1": {"kind": "claude", "model": "claude-opus-5-5"},
    "review-pr:verify-2": {"kind": "fundi", "model": "openrouter/z-ai/glm-5.3-flash"},
    "review-pr:verify-3": {"kind": "fundi", "model": "openrouter/openai/gpt-5.6-sol"},
}
PACKET = ["packet/intent.md", "packet/diff.patch", "packet/lenses.json", "tree/src/foo.go"]
LENS = "always-lens"
CLAIM = {"lens": LENS, "answer": "nothing", "reason": "the guard rejects the bad input",
         "raised_by": "review-pr:panel-2"}


def cand(cid, title, line=10, raised_by="review-pr:panel-2", source="panel", evidence="evidence",
         lens=""):
    return {"id": cid, "title": title, "file": "src/foo.go", "line": line, "severity": "S2",
            "claim": f"{title}: what goes wrong", "evidence": evidence, "repro": "call it twice",
            "raised_by": raised_by, "source": source, "concern": LENS, "lens": lens}


def make_run(tmp_path, candidates=(), checklist=(), status="verifying", pr=None):
    home = Home(tmp_path)
    rdir = home.runs / "demo" / RUN_ID
    (rdir / "project").mkdir(parents=True)
    (rdir / "packet").mkdir()
    (rdir / "review" / ATTEMPT).mkdir(parents=True)
    (rdir / "tree" / "src").mkdir(parents=True)
    (rdir / "tree" / "src" / "foo.go").write_text("package main\n" * 200)
    (rdir / "run.json").write_text(json.dumps({
        "run_id": RUN_ID, "kind": "pr", "mode": "pr", "project": {"name": "demo"},
        "tree": str(rdir / "tree"), "target": str(rdir / "tree"), "files_changed": ["src/foo.go"]}))
    (rdir / "state.json").write_text(json.dumps({"status": status}))
    (rdir / "project" / "pr.json").write_text(json.dumps(pr if pr is not None else {
        "confirm_over_tokens": 150000, "panel": PANEL, "verifiers": VERIFIERS, "finders": "all", "split_groups": True, "verify_claims": True,
        "finder": "review-pr:finder", "gather": "review-pr:gather", "lens_dir": None}))
    (rdir / "packet" / "files.txt").write_text("\n".join(PACKET) + "\n")
    (rdir / "packet" / "lenses.json").write_text(json.dumps({"lenses": [
        {"name": LENS, "category": "defect", "prompt": "Check the guard and the caller.", "always": True}]}))
    with (rdir / "review" / ATTEMPT / "candidates.jsonl").open("w") as fh:
        for c in candidates:
            fh.write(json.dumps(c, sort_keys=True) + "\n")
    with (rdir / "review" / ATTEMPT / "checklist.jsonl").open("w") as fh:
        for c in checklist:
            fh.write(json.dumps(c, sort_keys=True) + "\n")
    return home, rdir


def arm(fake, key, obj):
    """Script one job's final answer, keyed by the name the stage spawns it
    under."""
    fake.answers[pr_verify.child_name(RUN_ID, ATTEMPT, key)] = "```json\n" + json.dumps(obj) + "\n```"


def jsonl(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def friction_log(rdir):
    """The run's friction, empty when nothing was recorded (no file)."""
    f = rdir / "friction.jsonl"
    return jsonl(f) if f.exists() else []


def verify_answer(refuted, adjustment="none", notes=()):
    return {"refuted": refuted, "reasoning": f"the code says refuted={refuted}",
            "severity_adjustment": adjustment, "process_notes": list(notes)}


def run_stage(fake, home, **kw):
    return pr_verify.stage_verify_pr(fake, home, SimpleNamespace(**{"run": RUN_ID, "model": None, "timeout": 30, "parallel": 24, **kw}))


def vkey(index, member_id, seat):
    return pr_verify.job_key("verify", index, member_id, seat)


def fid(f):
    """The id the stage gives a pooled finding. normalize recomputes it from the
    anchor and title, so a test that keys an answer by finding must use the same
    formula rather than a hand-written id."""
    return pr_review.finding_id(f)


# --- pure functions -------------------------------------------------------------


def test_cluster_groups_by_file_and_thirty_line_bucket():
    a = cand("a1", "one", line=10)
    b = cand("a2", "two", line=29)     # same 30-line bucket as line 10
    c = cand("b1", "three", line=30)   # the next bucket
    d = cand("c1", "other file", line=5)
    d["file"] = "src/bar.go"
    clusters = pr_verify.cluster([b, d, a, c])
    assert [(cl["key"], [m["id"] for m in cl["members"]]) for cl in clusters] == [
        ("src/bar.go:0", ["c1"]), ("src/foo.go:0", ["a1", "a2"]), ("src/foo.go:1", ["b1"])]


def test_cluster_is_deterministic_and_picks_the_longest_evidence():
    short = cand("a1", "one", line=10, evidence="short")
    long = cand("a2", "two", line=12, evidence="a much longer piece of evidence")
    finder = cand("a3", "three", line=20, raised_by="review-pr:finder", source="finder:lens-x")
    first = pr_verify.cluster([short, long, finder])
    second = pr_verify.cluster([finder, long, short])
    assert first == second
    assert first[0]["representative"]["id"] == "a2"
    assert first[0]["raised_by"] == ["review-pr:finder", "review-pr:panel-2"]
    assert first[0]["source"] == ["finder:lens-x", "panel"]


def test_member_units_keep_every_distinct_issue_in_a_bucket():
    # One issue raised twice is one unit; two issues sharing a bucket are two,
    # each with its own claim and its own raisers.
    cl = pr_verify.cluster([cand("aaaa", "first", line=3, evidence="x" * 40),
                            cand("aaaa", "first", line=3, evidence="short", raised_by="review-pr:finder"),
                            cand("bbbb", "second", line=25)])
    units = pr_verify.member_units(cl[0])
    assert [u["id"] for u in units] == ["aaaa", "bbbb"]
    assert units[0]["member"]["evidence"] == "x" * 40
    assert units[0]["raised_by"] == ["review-pr:finder", "review-pr:panel-2"]
    assert units[1]["member"]["title"] == "second"


def test_panel_raised_issue_is_verified_by_the_other_panel_seats_verifiers():
    cl = {"key": "src/foo.go:0", "raised_by": ["review-pr:panel-2"]}
    assert pr_verify.assign_verifiers(cl, PANEL, VERIFIERS, FAMILIES) == [
        "review-pr:verify-1", "review-pr:verify-3"]


def test_finder_raised_issue_gets_two_verifiers_of_different_families():
    cl = {"key": "src/foo.go:0", "raised_by": ["review-pr:finder"]}
    assert pr_verify.assign_verifiers(cl, PANEL, VERIFIERS, FAMILIES) == [
        "review-pr:verify-1", "review-pr:verify-2"]


def test_finder_raised_issue_skips_a_second_seat_of_the_same_family():
    # panel-1 and panel-2 served by one family: the second pick is panel-3.
    cl = {"key": "src/foo.go:0", "raised_by": ["review-pr:finder"]}
    families = {**FAMILIES, "review-pr:panel-2": "anthropic"}
    assert pr_verify.assign_verifiers(cl, PANEL, VERIFIERS, families) == [
        "review-pr:verify-1", "review-pr:verify-3"]


def test_finder_raised_issue_spreads_families_when_one_is_left():
    # F6: one family among the seats left, so two seats of it check the issue and
    # the shortfall is recorded rather than passed off as two opinions.
    cl = {"key": "src/foo.go:0", "raised_by": ["review-pr:finder"]}
    panel = ["review-pr:panel-1", "review-pr:panel-2"]
    families = {"review-pr:panel-1": "anthropic", "review-pr:panel-2": "anthropic"}
    shortfalls = []
    assert pr_verify.assign_verifiers(cl, panel, VERIFIERS, families, shortfalls=shortfalls) == [
        "review-pr:verify-1", "review-pr:verify-2"]
    assert len(shortfalls) == 1 and "one family" in shortfalls[0]


def test_a_single_outside_seat_does_not_fail_the_family_search():
    # F6's ordering ruling: the finder's family is not a bar when it is the only
    # way to a second seat, so one outside seat plus two finder-family seats is a
    # pair (of different families) and is recorded, never a Fail.
    cl = {"key": "k", "raised_by": ["review-pr:finder"]}
    families = {"review-pr:panel-1": "anthropic", "review-pr:panel-2": "z-ai",
                "review-pr:panel-3": "z-ai"}
    shortfalls = []
    assert pr_verify.assign_verifiers(cl, PANEL, VERIFIERS, families, exclude_family="z-ai",
                                      shortfalls=shortfalls) == ["review-pr:verify-1",
                                                                "review-pr:verify-2"]
    assert len(shortfalls) == 1 and "outside the finder's family" in shortfalls[0]


def test_finder_raised_issue_with_one_seat_left_fails():
    # F6: fewer than two seats cannot yield two fresh children.
    cl = {"key": "src/foo.go:0", "raised_by": ["review-pr:finder"]}
    with pytest.raises(Fail) as e:
        pr_verify.assign_verifiers(cl, ["review-pr:panel-1"], VERIFIERS, FAMILIES)
    assert "two fresh children" in str(e.value)


def test_finder_raised_issue_avoids_the_finders_own_family():
    # F7: with the shipped presets, panel-2's verifier runs the finder's model, so
    # the pair is panel-1 + panel-3 instead.
    cl = {"key": "src/foo.go:0", "raised_by": ["review-pr:finder"]}
    assert pr_verify.assign_verifiers(cl, PANEL, VERIFIERS, FAMILIES, exclude_family="z-ai") == [
        "review-pr:verify-1", "review-pr:verify-3"]


def test_finder_raised_issue_uses_the_finders_family_when_nothing_else_is_left():
    cl = {"key": "src/foo.go:0", "raised_by": ["review-pr:finder"]}
    families = {s: "z-ai" for s in PANEL}
    shortfalls = []
    assert pr_verify.assign_verifiers(cl, PANEL, VERIFIERS, families, exclude_family="z-ai",
                                      shortfalls=shortfalls) == ["review-pr:verify-1", "review-pr:verify-2"]
    # the finder's family was reused AND the pair shares one family: both weakenings
    # are recorded
    assert len(shortfalls) == 2
    assert any("finder's own family" in s for s in shortfalls)
    assert any("one family" in s for s in shortfalls)


def test_a_seat_never_verifies_what_it_raised():
    # A claim the verify-2 seat refuted comes back raised_by verify-2; its panel
    # twin is out too, so the child that raised the finding cannot verify it.
    cl = {"key": "src/foo.go:0", "raised_by": ["review-pr:verify-2"]}
    assert pr_verify.assign_verifiers(cl, PANEL, VERIFIERS, FAMILIES) == [
        "review-pr:verify-1", "review-pr:verify-3"]


def test_a_raiser_stays_out_of_the_fallback_list():
    # The review's probe cases: the seat that raised the issue (verify-2 here) is
    # out of both lists, and the fallback keeps the verifier twins of raising panel
    # seats, which are the only fresh children left (the brief's rule, and the same
    # list it asks for when every panel seat raised the issue).
    a = pr_verify.assign_verifiers({"key": "src/foo.go:0", "raised_by": ["review-pr:panel-1",
                                                                         "review-pr:panel-3",
                                                                         "review-pr:verify-2"]},
                                   PANEL, VERIFIERS, FAMILIES)
    assert a == ["review-pr:verify-1", "review-pr:verify-3"]
    b = pr_verify.assign_verifiers({"key": "src/foo.go:0",
                                    "raised_by": [*PANEL, "review-pr:verify-2"]}, PANEL, VERIFIERS, FAMILIES)
    assert b == ["review-pr:verify-1", "review-pr:verify-3"]
    # a raiser is out even when it is the only verifier seat left
    c = pr_verify.assign_verifiers({"key": "src/foo.go:0", "raised_by": ["review-pr:panel-1",
                                                                          "review-pr:panel-2",
                                                                          "review-pr:verify-1",
                                                                          "review-pr:verify-2"]},
                                   ["review-pr:panel-1", "review-pr:panel-2"], VERIFIERS, FAMILIES)
    assert c == ["review-pr:verify-3"]


@pytest.mark.parametrize("raised_by,panel,verifiers", [
    # Nothing legitimate remains: every verifier seat raised the issue itself. The
    # caller records the issue as incomplete rather than failing the run (N1).
    (["review-pr:verify-1", "review-pr:verify-2", "review-pr:verify-3"], PANEL, VERIFIERS),
    (["review-pr:verify-1"], ["review-pr:panel-1"], {"review-pr:panel-1": "review-pr:verify-1"}),
])
def test_an_issue_every_verifier_seat_raised_gets_no_verifier(raised_by, panel, verifiers):
    assert pr_verify.assign_verifiers({"key": "src/foo.go:0", "raised_by": raised_by},
                                     panel, verifiers, FAMILIES) == []


def test_all_three_panel_seats_raising_it_still_gets_verifiers():
    cl = {"key": "src/foo.go:0", "raised_by": list(PANEL)}
    assert pr_verify.assign_verifiers(cl, PANEL, VERIFIERS, FAMILIES) == [
        "review-pr:verify-1", "review-pr:verify-2", "review-pr:verify-3"]


def test_an_issue_is_never_left_unverified_without_a_verifier_mapping():
    # A config with no verifier at all cannot check anything, so it is a Fail.
    cl = {"key": "src/foo.go:0", "raised_by": ["review-pr:panel-1"]}
    with pytest.raises(Fail) as e:
        pr_verify.assign_verifiers(cl, PANEL, {}, FAMILIES)
    assert "src/foo.go:0" in str(e.value)


@pytest.mark.parametrize("votes,expected", [
    ([], "incomplete"),
    ([{"refuted": False}, {"refuted": False}], "confirmed"),
    ([{"refuted": False}, {"refuted": True}], "contested"),
    ([{"refuted": True}, {"refuted": False}], "contested"),
    ([{"refuted": True}, {"refuted": True}], "refuted"),
    ([{"refuted": True}, {"reasoning": "no verdict"}], "incomplete"),
    ([{"refuted": "false"}], "incomplete"),
    ([{"refuted": None}], "incomplete"),
    ([{"refuted": False}], "confirmed"),
    ([{"refuted": True}], "refuted"),
])
def test_verdict_table(votes, expected):
    assert pr_verify.verdict(votes) == expected


@pytest.mark.parametrize("members,expected", [
    (["confirmed", "confirmed"], "confirmed"),
    (["refuted", "refuted"], "refuted"),
    (["confirmed", "refuted"], "contested"),
    (["contested", "refuted"], "contested"),
    (["confirmed", "incomplete"], "incomplete"),
    (["refuted", "incomplete"], "incomplete"),
])
def test_bucket_verdict_from_its_issues(members, expected):
    assert pr_verify.cluster_verdict(members) == expected


def test_minority_side_only_exists_on_an_uneven_split():
    assert pr_verify.minority_side([{"refuted": True}, {"refuted": True}, {"refuted": False}]) == {False}
    assert pr_verify.minority_side([{"refuted": True}, {"refuted": False}]) == set()
    assert pr_verify.minority_side([{"refuted": True}, {"refuted": None}]) == set()
    assert pr_verify.vote_tally([{"refuted": True}, {"refuted": None}, {"refuted": False}]) == {True: 1, False: 1}


@pytest.mark.parametrize("kind,model,expected", [
    ("claude", "claude-opus-5-5", "anthropic"),
    ("fundi", "openrouter/z-ai/glm-5.3-flash", "z-ai"),
    ("fundi", "openrouter/openai/gpt-5.6-sol", "openai"),
    ("fundi", "openrouter/~deepseek/deepseek-flash-latest", "~deepseek"),
    ("fundi", None, ""),
])
def test_seat_family(kind, model, expected):
    assert pr_verify.seat_family(kind, model) == expected


@pytest.mark.parametrize("answer,kind,canonical", [
    ("nothing", "claim", True),
    ("  Nothing ", "claim", True),
    ("findings", "findings", True),
    ("Findings", "findings", True),
    ("checked: nothing, because the guard rejects it", "claim", False),
    ("", "claim", False),
    (None, "claim", False),
    ("findings, see above", "claim", False),
])
def test_claim_answer_treats_everything_but_findings_as_a_claim(answer, kind, canonical):
    assert pr_verify.claim_answer(answer) == (kind, canonical)


def test_claim_reason_falls_back_to_the_answer_text():
    assert pr_verify.claim_reason({"reason": " the guard ", "answer": "nothing"}) == "the guard"
    assert pr_verify.claim_reason({"answer": "checked: nothing, because the guard runs"}) == \
        "checked: nothing, because the guard runs"
    assert pr_verify.claim_reason({"answer": "nothing"}) == ""
    assert pr_verify.claim_reason({"answer": None}) == ""


def test_backed_lenses_needs_the_raisers_own_finding_naming_that_lens():
    # F5: the pair (seat, lens) is what backs a "findings" answer, so one seat's
    # finding cannot stand for another seat's answer, and a finding that names no
    # lens backs nothing at all.
    assert pr_verify.backed_lenses([cand("a1", "x", lens=LENS)]) == {("review-pr:panel-2", LENS)}
    assert pr_verify.backed_lenses(
        [cand("a1", "x", raised_by="review-pr:panel-3", lens=LENS)]) == {("review-pr:panel-3", LENS)}
    assert pr_verify.backed_lenses([cand("a1", "x")]) == set()
    assert pr_verify.backed_lenses([{**cand("a1", "x"), "lens": "  "}]) == set()


def test_refutation_synthesizes_the_missing_grounding():
    found, note = pr_verify.refutation(LENS, "review-pr:verify-1", "the reason names no code", None)
    assert (found["category"], found["severity"], found["file"], found["line"]) == ("quality", "S3", "", 0)
    assert found["claim"] == found["evidence"] == "the reason names no code"
    assert found["source"] == f"claim:{LENS}" and found["concern"] == LENS
    assert found["raised_by"] == "review-pr:verify-1" and found["id"]
    assert note and "no finding object" in note

    # A finding object is used as given, with the raiser and source stamped on.
    given, note2 = pr_verify.refutation(LENS, "review-pr:verify-2", "r",
                                        {"title": "t", "file": "src/foo.go", "line": 12})
    assert given["title"] == "t" and given["line"] == 12 and given["raised_by"] == "review-pr:verify-2"
    assert note2 is None


def test_normalize_gives_a_finding_an_int_line_and_its_own_id():
    f = pr_verify.normalize({"title": "t", "file": "src/foo.go", "line": "src/foo.go:12"})
    assert f["line"] == 0 and f["id"] == pr_review.finding_id(f)
    # a model-written id is never kept: it could tie two distinct issues together
    stolen = pr_verify.normalize({"title": "t", "file": "src/foo.go", "line": 3, "id": "other"})
    assert stolen["id"] == pr_review.finding_id({"title": "t", "file": "src/foo.go", "line": 3})


def test_normalize_names_every_line_it_coerces():
    # W8 residual: the line sets the bucket AND part of the id, so a coercion with
    # no trace is a finding that can move anchor silently.
    notes = []
    f = pr_verify.normalize({"title": "t", "file": "src/foo.go", "line": None}, notes)
    assert f["line"] == 0 and len(notes) == 1 and f["id"] in notes[0] and "line None" in notes[0]
    # an absent field is the same coercion
    pr_verify.normalize({"title": "t", "file": "src/foo.go"}, notes)
    assert len(notes) == 2 and "line None" in notes[1]
    # text int_line rescues is still a coercion: the raw value is not what the
    # bucket and the id were computed from
    pr_verify.normalize({"title": "t", "file": "src/foo.go", "line": "12"}, notes)
    assert len(notes) == 3 and "line '12'" in notes[2]
    # an int line is not a coercion, so nothing is recorded for it
    assert pr_verify.normalize({"title": "t", "file": "src/foo.go", "line": 12}, notes)["line"] == 12
    assert len(notes) == 3


# --- the stage ------------------------------------------------------------------


def panel_raiser_run(tmp_path, fake, **kw):
    """One candidate raised by the panel, verified by verify-1 and verify-3."""
    c = cand("a1", "leaks a socket", **kw)
    home, rdir = make_run(tmp_path, candidates=[c])
    fake.presets = PRESETS
    mid = fid(c)
    return home, rdir, [vkey(0, mid, "review-pr:verify-1"), vkey(1, mid, "review-pr:verify-3")]


def test_stage_writes_a_refuted_bucket_and_sets_done(tmp_path, fake_rafiki):
    home, rdir, keys = panel_raiser_run(tmp_path, fake_rafiki)
    for key in keys:
        arm(fake_rafiki, key, verify_answer(True))
    summary = run_stage(fake_rafiki, home)

    (row,) = jsonl(rdir / "verify" / ATTEMPT / "verdicts.jsonl")
    assert row["verdict"] == "refuted"
    assert row["key"] == "src/foo.go:0"
    assert [v["seat"] for v in row["votes"]] == ["review-pr:verify-1", "review-pr:verify-3"]
    assert [m["id"] for m in row["members"]] == [fid(cand("a1", "leaks a socket"))]
    assert [m["verdict"] for m in row["members"]] == ["refuted"]
    assert row["raised_by"] == ["review-pr:panel-2"]
    assert row["source"] == ["panel"]
    assert row["title"] == "leaks a socket" and row["line"] == 10
    assert summary["refuted"] == 1 and summary["clusters"] == 1 and summary["members"] == 1
    assert json.loads((rdir / "state.json").read_text())["status"] == "done"
    # one sort_keys row, byte for byte: the run's bytes are compared and hashed
    (line,) = [l for l in (rdir / "verify" / ATTEMPT / "verdicts.jsonl").read_text().splitlines() if l]
    assert line == json.dumps(json.loads(line), sort_keys=True)


def test_stage_confirms_when_every_verifier_upholds(tmp_path, fake_rafiki):
    home, rdir, keys = panel_raiser_run(tmp_path, fake_rafiki)
    for key in keys:
        arm(fake_rafiki, key, verify_answer(False, adjustment="downgrade", notes=["read two files"]))
    run_stage(fake_rafiki, home)

    (row,) = jsonl(rdir / "verify" / ATTEMPT / "verdicts.jsonl")
    assert row["verdict"] == "confirmed"
    # every vote's severity adjustment is reported and none is averaged away
    assert [v["severity_adjustment"] for v in row["votes"]] == ["downgrade", "downgrade"]
    friction = friction_log(rdir)
    assert {f["kind"] for f in friction} == {"self"}
    assert [f["note"] for f in friction] == ["read two files", "read two files"]


def test_each_distinct_issue_in_a_bucket_gets_its_own_votes(tmp_path, fake_rafiki):
    # F4: two issues 15 lines apart share a bucket; the first must not answer for
    # the second, and a refuted second must not be hidden by the first.
    a = cand("aaaa", "long one", line=10, evidence="x" * 40)
    b = cand("bbbb", "short one", line=25)
    home, rdir = make_run(tmp_path, candidates=[a, b])
    fake_rafiki.presets = PRESETS
    for pos, uid in enumerate(sorted([fid(a), fid(b)])):
        for i, seat in enumerate(("review-pr:verify-1", "review-pr:verify-3")):
            arm(fake_rafiki, vkey(2 * pos + i, uid, seat), verify_answer(uid != fid(a)))
    out = run_stage(fake_rafiki, home)

    (row,) = jsonl(rdir / "verify" / ATTEMPT / "verdicts.jsonl")
    assert row["verdict"] == "contested"
    # The summary counts issues by their own verdict, like the report: the
    # contested bucket holds one confirmed and one refuted issue.
    assert (out["confirmed"], out["refuted"], out["contested"]) == (1, 1, 0)
    assert out["buckets"]["contested"] == 1
    assert [m["id"] for m in row["members"]] == sorted([fid(a), fid(b)])
    assert {m["id"]: m["verdict"] for m in row["members"]} == {fid(a): "confirmed", fid(b): "refuted"}
    assert [v["id"] for v in row["votes"]] == [uid for uid in sorted([fid(a), fid(b)]) for _ in range(2)]
    # the second issue is put to verifiers in its own right
    assert "short one" in " ".join(s["instruction"] for s in fake_rafiki.seat_spawns)


def test_stage_keeps_a_contested_issue_and_marks_the_minority_side(tmp_path, fake_rafiki):
    # One issue every panel seat raised, so all three verifier seats check it (the
    # fallback): a 2-1 split has a minority side, an even split would not.
    c = cand("a1", "one", line=10)
    home, rdir = make_run(tmp_path, candidates=[{**c, "raised_by": p} for p in PANEL])
    fake_rafiki.presets = PRESETS
    for i, (seat, refuted) in enumerate((("review-pr:verify-1", True), ("review-pr:verify-2", True),
                                         ("review-pr:verify-3", False))):
        arm(fake_rafiki, vkey(i, fid(c), seat), verify_answer(refuted))
    run_stage(fake_rafiki, home)

    (row,) = jsonl(rdir / "verify" / ATTEMPT / "verdicts.jsonl")
    assert row["verdict"] == "contested"
    assert len(row["votes"]) == 3
    assert [m["id"] for m in row["members"]] == [fid(c)]
    losses = [f for f in friction_log(rdir) if f["kind"] == "contested-loss"]
    assert [f["seat"] for f in losses] == ["review-pr:verify-3"]
    assert "against 2 vote(s)" in losses[0]["note"]


def test_an_unparseable_vote_makes_the_issue_incomplete(tmp_path, fake_rafiki):
    home, rdir, keys = panel_raiser_run(tmp_path, fake_rafiki)
    arm(fake_rafiki, keys[0], verify_answer(True))
    fake_rafiki.answers[pr_verify.child_name(RUN_ID, ATTEMPT, keys[1])] = "no json block at all"
    run_stage(fake_rafiki, home)

    (row,) = jsonl(rdir / "verify" / ATTEMPT / "verdicts.jsonl")
    assert row["verdict"] == "incomplete"
    assert [v["refuted"] for v in row["votes"]] == [True, None]
    assert any(f["kind"] == "parse-error" and f["seat"] == "review-pr:verify-3"
               for f in friction_log(rdir))


def test_verifier_tasks_never_name_a_seat_or_a_model(tmp_path, fake_rafiki):
    home, rdir, keys = panel_raiser_run(tmp_path, fake_rafiki)
    for key in keys:
        arm(fake_rafiki, key, verify_answer(True))
    run_stage(fake_rafiki, home)

    assert len(fake_rafiki.seat_spawns) == 2
    for spawn in fake_rafiki.seat_spawns:
        text = spawn["instruction"]
        for leak in ("review-pr", "panel-1", "panel-2", "panel-3", "verify-1", "verify-2",
                     "verify-3", "claude-opus-5-5", "glm-5.3-flash", "gpt-5.6-sol"):
            assert leak not in text, leak
        # the packet is the shared files.txt list, in order, unchanged
        assert spawn["packet_files"] == PACKET
        # its paths are run-relative, so the seat runs in the run directory
        assert spawn["cwd"] == str(rdir)
    assert fake_rafiki.seat_spawns[0]["preset"] == "review-pr:verify-1"


def test_finder_raised_issue_is_verified_by_other_families(tmp_path, fake_rafiki):
    # F7 end to end: the finder runs glm-5.3-flash, so verify-2 (the same model)
    # is not half of its cross-check.
    c = cand("a1", "finder issue", raised_by="review-pr:finder", source="finder:always-lens")
    home, rdir = make_run(tmp_path, candidates=[c])
    fake_rafiki.presets = PRESETS
    for i, seat in enumerate(("review-pr:verify-1", "review-pr:verify-3")):
        arm(fake_rafiki, vkey(i, fid(c), seat), verify_answer(False))
    run_stage(fake_rafiki, home)

    assert [s["preset"] for s in fake_rafiki.seat_spawns] == ["review-pr:verify-1", "review-pr:verify-3"]
    (row,) = jsonl(rdir / "verify" / ATTEMPT / "verdicts.jsonl")
    assert row["verdict"] == "confirmed"


def test_a_refuted_nothing_claim_becomes_a_verified_finding(tmp_path, fake_rafiki):
    home, rdir = make_run(tmp_path, checklist=[CLAIM])
    fake_rafiki.presets = PRESETS
    finding = {"title": "the input reaches the parser unguarded", "category": "defect",
               "file": "src/foo.go", "line": 12, "severity": "S3", "claim": "bad input is parsed",
               "evidence": "foo.go:12 parses it", "repro": "n/a"}
    arm(fake_rafiki, "claim-00-verify-1",
        {"holds": False, "reasoning": "no guard exists on that path", "finding": finding,
         "process_notes": ["claim checked"]})
    arm(fake_rafiki, "claim-00-verify-3", {"holds": True, "reasoning": "the guard is at foo.go:9",
                                           "finding": None})
    # the refutation is raised by verify-1, so panel-1 and verify-1 are out
    mid = pr_review.finding_id(finding)
    for i, seat in enumerate(("review-pr:verify-2", "review-pr:verify-3")):
        arm(fake_rafiki, vkey(i, mid, seat), verify_answer(False))
    summary = run_stage(fake_rafiki, home)

    (claim,) = jsonl(rdir / "verify" / ATTEMPT / "claims.jsonl")
    assert claim["verdict"] == "refuted"
    assert claim["lens"] == LENS and claim["raised_by"] == "review-pr:panel-2"
    assert [v["holds"] for v in claim["votes"]] == [False, True]
    assert [f["source"] for f in claim["findings"]] == [f"claim:{LENS}"]
    assert summary["claim_findings"] == 1
    assert [f["kind"] for f in friction_log(rdir)] == ["self"]

    (row,) = jsonl(rdir / "verify" / ATTEMPT / "verdicts.jsonl")
    assert row["verdict"] == "confirmed"
    assert row["raised_by"] == ["review-pr:verify-1"]
    assert row["source"] == [f"claim:{LENS}"]
    assert [v["seat"] for v in row["votes"]] == ["review-pr:verify-2", "review-pr:verify-3"]
    # a claim job is told whose lens and reason, never which seat said so or on
    # which model it runs
    for spawn in fake_rafiki.seat_spawns:
        for leak in ("review-pr", "panel-2", "verify-1", "verify-2", "verify-3", "glm-5.3-flash"):
            assert leak not in spawn["instruction"], leak


def test_a_claim_refuted_without_a_finding_still_becomes_one(tmp_path, fake_rafiki):
    # F2: holds=false with no finding object is a refutation the stage owes a
    # finding for, and it is checked by a seat that did not raise it.
    home, rdir = make_run(tmp_path, checklist=[CLAIM])
    fake_rafiki.presets = PRESETS
    for key in ("claim-00-verify-1", "claim-00-verify-3"):
        arm(fake_rafiki, key, {"holds": False, "reasoning": "the reason names no code", "finding": None})
    synth = pr_verify.refutation(LENS, "review-pr:verify-1", "the reason names no code", None)[0]
    # both synthesized refutations share one id, so the issue has two raisers and
    # only verify-2 (panel-2's twin) may check it
    arm(fake_rafiki, vkey(0, synth["id"], "review-pr:verify-2"), verify_answer(False))
    run_stage(fake_rafiki, home)

    (claim,) = jsonl(rdir / "verify" / ATTEMPT / "claims.jsonl")
    assert claim["verdict"] == "refuted"
    assert len(claim["findings"]) == 2
    assert {(f["category"], f["severity"], f["file"]) for f in claim["findings"]} == {("quality", "S3", "")}
    assert [f["claim"] for f in claim["findings"]] == ["the reason names no code"] * 2
    kinds = [f["kind"] for f in friction_log(rdir)]
    assert kinds.count("instruction-breach") == 2
    assert "hallucination" not in kinds  # a synthesized finding has no location to check

    (row,) = jsonl(rdir / "verify" / ATTEMPT / "verdicts.jsonl")
    assert row["key"] == ":0"
    assert row["verdict"] == "confirmed"
    assert row["raised_by"] == ["review-pr:verify-1", "review-pr:verify-3"]
    assert [v["seat"] for v in row["votes"]] == ["review-pr:verify-2"]


def test_a_claim_that_holds_leaves_no_finding(tmp_path, fake_rafiki):
    home, rdir = make_run(tmp_path, checklist=[CLAIM])
    fake_rafiki.presets = PRESETS
    for key in ("claim-00-verify-1", "claim-00-verify-3"):
        arm(fake_rafiki, key, {"holds": True, "reasoning": "the guard is on that path", "finding": None})
    run_stage(fake_rafiki, home)

    (claim,) = jsonl(rdir / "verify" / ATTEMPT / "claims.jsonl")
    assert claim["verdict"] == "holds" and claim["findings"] == []
    assert jsonl(rdir / "verify" / ATTEMPT / "verdicts.jsonl") == []


def test_every_checklist_answer_but_findings_is_attacked(tmp_path, fake_rafiki):
    # F3: the prompt offers both the literal "nothing" and its prose form, and a
    # seat may write neither. All of them are claims; the odd spellings are also
    # recorded. A "findings" answer is not a claim, but it is not trusted either:
    # with nothing behind it, this one is a breach (see the F5 tests below).
    checklist = [
        {"lens": "lens-a", "answer": "checked: nothing, because the guard rejects it",
         "raised_by": "review-pr:panel-2"},
        {"lens": "lens-b", "answer": "Nothing", "reason": "clean", "raised_by": "review-pr:panel-3"},
        {"lens": "lens-c", "answer": None, "raised_by": "review-pr:panel-2"},
        {"lens": "lens-d", "answer": "findings", "raised_by": "review-pr:panel-2"},
    ]
    home, rdir = make_run(tmp_path, checklist=checklist)
    fake_rafiki.presets = PRESETS
    for key in ("claim-00-verify-1", "claim-00-verify-3", "claim-01-verify-1", "claim-01-verify-2",
                "claim-02-verify-1", "claim-02-verify-3"):
        arm(fake_rafiki, key, {"holds": True, "reasoning": "grounded in the code", "finding": None})
    run_stage(fake_rafiki, home)

    rows = jsonl(rdir / "verify" / ATTEMPT / "claims.jsonl")
    assert [r["lens"] for r in rows] == ["lens-a", "lens-b", "lens-c"]
    assert [r["verdict"] for r in rows] == ["holds", "holds", "holds"]
    # the prose form is the reason when the seat left `reason` empty
    assert rows[0]["reason"] == "checked: nothing, because the guard rejects it"
    breaches = [f for f in friction_log(rdir) if f["kind"] == "instruction-breach"]
    assert [f["note"].split(":")[0] for f in breaches] == ["lens lens-a", "lens lens-c", "lens lens-d"]
    assert "None" in breaches[1]["note"]


def test_a_findings_answer_with_no_backing_finding_is_an_instruction_breach(tmp_path, fake_rafiki):
    # F5: the dodge the ruling closes. A seat can answer "findings" for every
    # always-on lens, raise nothing, and sail past every claim attack, because
    # "findings" was never a claim. With no candidate of its own naming the lens,
    # the answer is an instruction breach naming the seat and the lens, and no
    # finding is invented to stand for the answer the seat never grounded.
    home, rdir = make_run(tmp_path, checklist=[
        {"lens": LENS, "answer": "findings", "reason": "the finding I raised above",
         "raised_by": "review-pr:panel-2"}])
    fake_rafiki.presets = PRESETS
    summary = run_stage(fake_rafiki, home)

    assert fake_rafiki.seat_spawns == []
    assert jsonl(rdir / "verify" / ATTEMPT / "claims.jsonl") == []
    assert jsonl(rdir / "verify" / ATTEMPT / "verdicts.jsonl") == []
    assert summary["claim_findings"] == 0 and summary["claims"] == 0
    (breach,) = [f for f in friction_log(rdir) if f["kind"] == "instruction-breach"]
    assert breach["seat"] == "review-pr:panel-2"
    assert LENS in breach["note"] and "no finding raised for it" in breach["note"]


def test_a_findings_answer_backed_by_its_own_finding_is_not_attacked(tmp_path, fake_rafiki):
    # F5: the honest answer. The seat raised the finding it names for that lens, so
    # the answer is neither a claim nor a breach; the finding itself is still
    # verified like any other, because only the answer is trusted, not the issue.
    c = cand("a1", "raised for the lens", lens=LENS)
    home, rdir = make_run(tmp_path, candidates=[c], checklist=[
        {"lens": LENS, "answer": "findings", "reason": "the finding I raised above",
         "raised_by": "review-pr:panel-2"}])
    fake_rafiki.presets = PRESETS
    for i, seat in enumerate(("review-pr:verify-1", "review-pr:verify-3")):
        arm(fake_rafiki, vkey(i, fid(c), seat), verify_answer(False))
    summary = run_stage(fake_rafiki, home)

    assert jsonl(rdir / "verify" / ATTEMPT / "claims.jsonl") == []
    assert summary["claims"] == 0 and summary["claim_findings"] == 0
    assert [f for f in friction_log(rdir) if f["kind"] == "instruction-breach"] == []
    # the finding's own two verifiers ran, and no child was spent attacking the answer
    assert [s["preset"] for s in fake_rafiki.seat_spawns] == ["review-pr:verify-1", "review-pr:verify-3"]
    (row,) = jsonl(rdir / "verify" / ATTEMPT / "verdicts.jsonl")
    assert row["verdict"] == "confirmed"


def test_a_refused_spawn_is_requeued_not_counted(tmp_path, fake_rafiki):
    home, rdir, keys = panel_raiser_run(tmp_path, fake_rafiki)
    for key in keys:
        arm(fake_rafiki, key, verify_answer(False))
    # The cap refuses the second spawn while the first child is still live: it is
    # requeued and retried once that child settles, never counted as a result.
    spawn_seat, calls = fake_rafiki.spawn_seat, []

    def flaky(name, **kw):
        calls.append(name)
        if len(calls) == 2:
            raise AtCap("live agent(s) already running")
        return spawn_seat(name, **kw)

    fake_rafiki.spawn_seat = flaky
    run_stage(fake_rafiki, home)

    (row,) = jsonl(rdir / "verify" / ATTEMPT / "verdicts.jsonl")
    assert row["verdict"] == "confirmed" and len(row["votes"]) == 2
    assert len(calls) == 3  # one refusal, then the retry
    assert not [f for f in friction_log(rdir) if f["kind"] == "spawn-error"]


def test_a_failed_spawn_leaves_the_issue_incomplete(tmp_path, fake_rafiki):
    # A verifier that never ran has not upheld anything: the issue stays
    # incomplete rather than passing on the one vote that did arrive.
    home, rdir, keys = panel_raiser_run(tmp_path, fake_rafiki)
    for key in keys:
        arm(fake_rafiki, key, verify_answer(False))
    spawn_seat, calls = fake_rafiki.spawn_seat, []

    def flaky(name, **kw):
        calls.append(name)
        if len(calls) == 2:
            raise Fail("preset review-pr:verify-3 not found")
        return spawn_seat(name, **kw)

    fake_rafiki.spawn_seat = flaky
    summary = run_stage(fake_rafiki, home)

    (row,) = jsonl(rdir / "verify" / ATTEMPT / "verdicts.jsonl")
    assert row["verdict"] == "incomplete"
    assert [v["refuted"] for v in row["votes"]] == [False, None]
    assert summary["incomplete"] == 1
    assert [f["kind"] for f in friction_log(rdir)] == ["spawn-error"]
    assert (rdir / "verify" / ATTEMPT / keys[1] / "spawn-error.txt").exists()


def test_a_panel_seat_with_no_verifier_is_refused_before_any_job(tmp_path, fake_rafiki):
    # F9: the assignment IS the cross-check, so a gap in pr.json fails up front
    # rather than after the claim jobs have been paid for.
    home, _ = make_run(tmp_path, checklist=[CLAIM], pr={
        "panel": PANEL, "verifiers": {k: v for k, v in VERIFIERS.items() if k != "review-pr:panel-2"},
        "finder": "review-pr:finder", "finders": "all", "split_groups": True, "verify_claims": True})
    fake_rafiki.presets = PRESETS
    with pytest.raises(Fail) as e:
        run_stage(fake_rafiki, home)
    assert "review-pr:panel-2" in str(e.value)
    assert fake_rafiki.seat_spawns == []


def test_a_missing_verifier_preset_is_refused_before_any_job(tmp_path, fake_rafiki):
    home, _ = make_run(tmp_path, checklist=[CLAIM], pr={
        "panel": PANEL, "verifiers": {**VERIFIERS, "review-pr:panel-2": "review-pr:verify-9"},
        "finder": "review-pr:finder", "finders": "all", "split_groups": True, "verify_claims": True})
    fake_rafiki.presets = PRESETS
    with pytest.raises(Fail) as e:
        run_stage(fake_rafiki, home)
    assert "review-pr:verify-9" in str(e.value)
    assert fake_rafiki.seat_spawns == []


def test_the_families_come_from_the_verifier_presets(fake_rafiki):
    # F10: the verifier preset is what runs, so retuning it must move the family.
    fake_rafiki.presets = {**PRESETS, "review-pr:verify-2": {"kind": "fundi",
                                                             "model": "openrouter/openai/gpt-5.6-sol"}}
    assert pr_verify.verifier_families(fake_rafiki, PANEL, VERIFIERS) == {
        **FAMILIES, "review-pr:panel-2": "openai"}


def test_a_run_that_is_not_being_verified_is_refused(tmp_path, fake_rafiki):
    home, rdir = make_run(tmp_path, status="confirmed")
    fake_rafiki.presets = PRESETS
    with pytest.raises(Fail) as e:
        run_stage(fake_rafiki, home)
    assert "confirmed" in str(e.value)
    assert fake_rafiki.seat_spawns == []
    # the refusal gives the claim back: a run that was never verified must not be
    # left behind a lock
    assert not (rdir / "verify.lock").exists()


def test_the_newest_review_attempt_is_the_one_verified(tmp_path, fake_rafiki):
    home, rdir = make_run(tmp_path)
    (rdir / "review" / "02").mkdir()
    with (rdir / "review" / "02" / "candidates.jsonl").open("w") as fh:
        fh.write(json.dumps(cand("a1", "newer", line=10), sort_keys=True) + "\n")
    with (rdir / "review" / "02" / "checklist.jsonl").open("w"):
        pass
    fake_rafiki.presets = PRESETS
    c = cand("a1", "newer", line=10)
    for i, seat in enumerate(("review-pr:verify-1", "review-pr:verify-3")):
        arm(fake_rafiki, vkey(i, fid(c), seat), verify_answer(False))
    run_stage(fake_rafiki, home)

    (row,) = jsonl(rdir / "verify" / ATTEMPT / "verdicts.jsonl")
    assert row["title"] == "newer"
    assert json.loads((rdir / "verify" / ATTEMPT / "state.json").read_text())["review"] == "02"


def test_a_malformed_pool_file_names_its_path(tmp_path, fake_rafiki):
    home, rdir = make_run(tmp_path)
    (rdir / "review" / ATTEMPT / "candidates.jsonl").write_text("{not json\n")
    fake_rafiki.presets = PRESETS
    with pytest.raises(Fail) as e:
        run_stage(fake_rafiki, home)
    assert str(rdir / "review" / ATTEMPT / "candidates.jsonl") in str(e.value)
    assert fake_rafiki.seat_spawns == []


def test_a_model_written_id_never_merges_two_issues(tmp_path, fake_rafiki):
    # F4 probe: a claim refutation that copies a pooled finding's id (or numbers
    # itself "F1") must not swallow it. The id is always recomputed, so both
    # issues get their own unit, their own seats and their own votes.
    pooled = cand("abc", "real bug A", line=12, raised_by="review-pr:panel-1")
    home, rdir = make_run(tmp_path, candidates=[pooled], checklist=[CLAIM])
    fake_rafiki.presets = PRESETS
    stolen = {"id": "abc", "title": "different bug B", "category": "defect", "file": "src/foo.go",
              "line": 14, "severity": "S2", "claim": "b is unguarded", "evidence": "b" * 20,
              "repro": "n/a"}
    arm(fake_rafiki, "claim-00-verify-1",
        {"holds": False, "reasoning": "no guard on that path", "finding": stolen})
    arm(fake_rafiki, "claim-00-verify-3", {"holds": True, "reasoning": "guarded at foo.go:9",
                                           "finding": None})
    ids = sorted([pr_review.finding_id(pooled), pr_review.finding_id(stolen)])
    assert "abc" not in ids  # the model's id is not an id here
    for pos, uid in enumerate(ids):
        for i, seat in enumerate(("review-pr:verify-2", "review-pr:verify-3")):
            arm(fake_rafiki, vkey(2 * pos + i, uid, seat), verify_answer(False))
    run_stage(fake_rafiki, home)

    (row,) = jsonl(rdir / "verify" / ATTEMPT / "verdicts.jsonl")
    assert [m["id"] for m in row["members"]] == ids
    assert {m["title"] for m in row["members"]} == {"real bug A", "different bug B"}
    # both issues were put to verifiers, so neither was swallowed by the other's id
    tasks = " ".join(s["instruction"] for s in fake_rafiki.seat_spawns)
    assert "real bug A" in tasks and "different bug B" in tasks


def test_a_lens_every_panel_seat_claimed_nothing_for_stays_incomplete(tmp_path, fake_rafiki):
    # N1: three refutations of one lens share an id (they are one hole per
    # claimant), so no verifier seat is left to check the issue. The stage records
    # it and carries on: an unproven row is shown, and a dead stage would lose
    # every other verdict in the run.
    home, rdir = make_run(tmp_path, checklist=[
        {"lens": LENS, "answer": "nothing", "reason": "no code touches this", "raised_by": p}
        for p in PANEL])
    fake_rafiki.presets = PRESETS
    for i in range(len(PANEL)):
        for seat in ("review-pr:verify-1", "review-pr:verify-2", "review-pr:verify-3"):
            arm(fake_rafiki, pr_verify.job_key("claim", i, LENS, seat),
                {"holds": False, "reasoning": "the reason names no code", "finding": None})
    summary = run_stage(fake_rafiki, home)

    claims = jsonl(rdir / "verify" / ATTEMPT / "claims.jsonl")
    assert [c["verdict"] for c in claims] == ["refuted"] * 3
    (row,) = jsonl(rdir / "verify" / ATTEMPT / "verdicts.jsonl")
    assert row["verdict"] == "incomplete"
    assert len(row["members"]) == 1 and row["members"][0]["verdict"] == "incomplete"
    assert row["votes"] == [] and row["members"][0]["seats"] == []
    assert summary["incomplete"] == 1 and summary["claim_findings"] == 6
    assert json.loads((rdir / "state.json").read_text())["status"] == "done"
    short = [f for f in friction_log(rdir) if f["kind"] == "verifier-shortfall"]
    assert len(short) == 1 and row["members"][0]["id"] in short[0]["note"]
    assert "stays incomplete" in short[0]["note"]


def test_a_missing_checklist_names_its_path(tmp_path, fake_rafiki):
    # W11: review always writes the checklist; reading its absence as "the panel
    # claimed nothing" would skip every claim job silently.
    home, rdir = make_run(tmp_path)
    (rdir / "review" / ATTEMPT / "checklist.jsonl").unlink()
    fake_rafiki.presets = PRESETS
    with pytest.raises(Fail) as e:
        run_stage(fake_rafiki, home)
    assert str(rdir / "review" / ATTEMPT / "checklist.jsonl") in str(e.value)
    assert fake_rafiki.seat_spawns == []


def test_a_claim_vote_without_a_boolean_holds_leaves_a_trace(tmp_path, fake_rafiki):
    home, rdir = make_run(tmp_path, checklist=[CLAIM])
    fake_rafiki.presets = PRESETS
    for key in ("claim-00-verify-1", "claim-00-verify-3"):
        arm(fake_rafiki, key, {"holds": "false", "reasoning": "a refutation, spelled as a string"})
    run_stage(fake_rafiki, home)

    (claim,) = jsonl(rdir / "verify" / ATTEMPT / "claims.jsonl")
    assert claim["verdict"] == "incomplete" and claim["findings"] == []
    breaches = [f for f in friction_log(rdir) if f["kind"] == "instruction-breach"]
    assert [f["seat"] for f in breaches] == ["review-pr:verify-1", "review-pr:verify-3"]
    assert "holds='false'" in breaches[0]["note"]


def test_a_verifier_vote_without_a_boolean_refuted_leaves_a_trace(tmp_path, fake_rafiki):
    home, rdir, keys = panel_raiser_run(tmp_path, fake_rafiki)
    arm(fake_rafiki, keys[0], verify_answer(False))
    arm(fake_rafiki, keys[1], {"refuted": "true", "reasoning": "a refutation, spelled as a string"})
    run_stage(fake_rafiki, home)

    (row,) = jsonl(rdir / "verify" / ATTEMPT / "verdicts.jsonl")
    assert row["verdict"] == "incomplete"
    breaches = [f for f in friction_log(rdir) if f["kind"] == "instruction-breach"]
    assert len(breaches) == 1 and "refuted='true'" in breaches[0]["note"]


def test_a_non_object_checklist_row_leaves_a_trace(tmp_path, fake_rafiki):
    home, rdir = make_run(tmp_path, checklist=["not an object", CLAIM])
    fake_rafiki.presets = PRESETS
    for key in ("claim-00-verify-1", "claim-00-verify-3"):
        arm(fake_rafiki, key, {"holds": True, "reasoning": "grounded in the code", "finding": None})
    run_stage(fake_rafiki, home)

    (claim,) = jsonl(rdir / "verify" / ATTEMPT / "claims.jsonl")
    assert claim["lens"] == LENS
    breaches = [f for f in friction_log(rdir) if f["kind"] == "instruction-breach"]
    assert len(breaches) == 1 and "checklist row 1 is not an object" in breaches[0]["note"]


def test_two_panel_seats_sharing_a_verifier_are_refused(tmp_path, fake_rafiki):
    # A shared verifier seat silently narrows a check to one child, so the mapping
    # is validated with the rest of the config, before any job is paid for.
    home, _ = make_run(tmp_path, checklist=[CLAIM], pr={
        "panel": PANEL, "verifiers": {p: "review-pr:verify-1" for p in PANEL},
        "finder": "review-pr:finder", "finders": "all", "split_groups": True, "verify_claims": True})
    fake_rafiki.presets = PRESETS
    with pytest.raises(Fail) as e:
        run_stage(fake_rafiki, home)
    assert "review-pr:verify-1" in str(e.value)
    assert fake_rafiki.seat_spawns == []


def test_the_claim_is_held_while_the_verify_children_run(tmp_path, fake_rafiki):
    # W7 probe: a spy on spawn_seat sees the marker, so a second invocation that
    # starts while these children are live is refused instead of spawning a full
    # second attempt over them.
    home, rdir, keys = panel_raiser_run(tmp_path, fake_rafiki)
    for key in keys:
        arm(fake_rafiki, key, verify_answer(False))
    spawn_seat, seen = fake_rafiki.spawn_seat, []

    def spy(name, **kw):
        seen.append(json.loads((rdir / "state.json").read_text()).get("verify_in_progress"))
        return spawn_seat(name, **kw)

    fake_rafiki.spawn_seat = spy
    run_stage(fake_rafiki, home)
    assert len(seen) == 2 and all(m and m.get("pid") for m in seen)


def test_a_second_verify_is_refused_while_one_is_in_progress(tmp_path, fake_rafiki):
    # The claim is the O_EXCL file, so the refusal names its holder and leaves
    # the live invocation's claim in place (clearing it would admit a third).
    home, rdir = make_run(tmp_path)
    fake_rafiki.presets = PRESETS
    (rdir / "verify.lock").write_text(json.dumps(
        {"stage": "verify", "at": "2026-01-01T00:00:00+00:00", "pid": 4242}, sort_keys=True) + "\n")
    with pytest.raises(Fail) as e:
        run_stage(fake_rafiki, home)
    assert "already in progress" in str(e.value) and "4242" in str(e.value)
    assert fake_rafiki.seat_spawns == []
    # the claim is the live invocation's: refusing must not release it for the other
    assert json.loads((rdir / "verify.lock").read_text())["pid"] == 4242


def test_the_claim_is_a_file_taken_atomically(tmp_path):
    # W7 residual: the mirror is not the claim. The file is created with O_EXCL,
    # so a second start cannot pass the guard by reading the same status; the
    # state is read only under the claim, so the marker it writes is never a
    # stale read written back.
    _home, rdir = make_run(tmp_path)
    claim, state = pr_verify.claim_verify(rdir, {"run_id": RUN_ID})

    assert claim.exists() and claim == rdir / "verify.lock"
    assert state["verify_in_progress"]["pid"] == os.getpid()
    with pytest.raises(Fail) as e:
        pr_verify.claim_verify(rdir, {"run_id": RUN_ID})
    assert "already in progress" in str(e.value)

    pr_verify.release_verify(rdir, claim)
    assert not (rdir / "verify.lock").exists()


def test_a_stale_mirror_does_not_refuse_a_run_whose_claim_is_gone(tmp_path):
    # The state marker is only the visible mirror: a crash whose lock is already
    # gone must not refuse the rerun forever, so the file is the one authority.
    _home, rdir = make_run(tmp_path)
    state = json.loads((rdir / "state.json").read_text())
    state["verify_in_progress"] = {"at": "2026-01-01T00:00:00+00:00", "pid": 4242}
    (rdir / "state.json").write_text(json.dumps(state))

    claim, fresh = pr_verify.claim_verify(rdir, {"run_id": RUN_ID})
    assert claim.exists() and fresh["verify_in_progress"]["pid"] == os.getpid()
    pr_verify.release_verify(rdir, claim)


def test_a_finished_verify_releases_its_claim(tmp_path, fake_rafiki):
    home, rdir, keys = panel_raiser_run(tmp_path, fake_rafiki)
    for key in keys:
        arm(fake_rafiki, key, verify_answer(False))
    run_stage(fake_rafiki, home)

    state = json.loads((rdir / "state.json").read_text())
    assert state["status"] == "done" and "verify_in_progress" not in state
    assert not (rdir / "verify.lock").exists()


def test_the_unwind_writes_the_record_and_releases_the_claim_with_the_daemon_gone(tmp_path, fake_rafiki):
    # W7 residual: when the daemon is gone the unwind is all that is left, and it
    # must still write the error record and still give the run back rather than
    # strand it behind a lock held for a stage that is dead.
    home, rdir, _keys = panel_raiser_run(tmp_path, fake_rafiki)

    def dead(*_a, **_kw):
        raise OSError("daemon unreachable")

    fake_rafiki.spawn_seat = dead
    with pytest.raises(OSError) as e:
        run_stage(fake_rafiki, home)
    assert "daemon unreachable" in str(e.value)

    state = json.loads((rdir / "state.json").read_text())
    assert state["status"] == "verifying" and "verify_in_progress" not in state
    assert state["verify_error"]["type"] == "OSError" and "daemon unreachable" in state["verify_error"]["error"]
    failed = [f for f in friction_log(rdir) if f["kind"] == "verify-failed"]
    assert len(failed) == 1 and "daemon unreachable" in failed[0]["note"]
    assert not (rdir / "verify.lock").exists()


def test_a_failed_verify_leaves_a_re_verifiable_run_with_an_error_record(tmp_path, fake_rafiki):
    # W7: a stage that dies after taking the run must not strand `verifying` with
    # nothing said. The status is defined, the cause is recorded, and the marker is
    # released so the rerun the record exists for is actually admitted.
    home, rdir, keys = panel_raiser_run(tmp_path, fake_rafiki)
    (rdir / "review" / ATTEMPT / "checklist.jsonl").unlink()
    fake_rafiki.presets = PRESETS
    with pytest.raises(Fail) as e:
        run_stage(fake_rafiki, home)
    assert "checklist.jsonl" in str(e.value)

    state = json.loads((rdir / "state.json").read_text())
    assert state["status"] == "verifying"
    assert "verify_in_progress" not in state
    assert state["verify_error"]["type"] == "Fail" and "checklist.jsonl" in state["verify_error"]["error"]
    assert state["verify_error"]["at"]
    failed = [f for f in friction_log(rdir) if f["kind"] == "verify-failed"]
    assert len(failed) == 1 and "checklist.jsonl" in failed[0]["note"]
    # the claim goes with the failure, or the rerun below would be refused
    assert not (rdir / "verify.lock").exists()

    # the rerun: admitted, and the stale error is not carried over. The failed
    # attempt died before it made its directory, so this rerun is attempt 01.
    (rdir / "review" / ATTEMPT / "checklist.jsonl").write_text("")
    for key in keys:
        arm(fake_rafiki, key, verify_answer(False))
    run_stage(fake_rafiki, home)
    state = json.loads((rdir / "state.json").read_text())
    assert state["status"] == "done"
    assert "verify_in_progress" not in state and "verify_error" not in state
    (row,) = jsonl(rdir / "verify" / ATTEMPT / "verdicts.jsonl")
    assert row["verdict"] == "confirmed"


def test_a_unit_left_with_one_verifier_records_the_shortfall(tmp_path, fake_rafiki):
    # N1 residual: two verifier seats refuting one lens synthesize one id, so the
    # only child left to check that issue is verify-2, and the claimant's twin
    # (verify-1) is out. One child is a weaker cross-check than two, so it is
    # recorded rather than passed off as a full check.
    home, rdir = make_run(tmp_path, checklist=[CLAIM])
    fake_rafiki.presets = PRESETS
    for key in ("claim-00-verify-1", "claim-00-verify-3"):
        arm(fake_rafiki, key, {"holds": False, "reasoning": "the reason names no code", "finding": None})
    synth = pr_verify.refutation(LENS, "review-pr:verify-1", "the reason names no code", None)[0]
    arm(fake_rafiki, vkey(0, synth["id"], "review-pr:verify-2"), verify_answer(False))
    run_stage(fake_rafiki, home)

    (row,) = jsonl(rdir / "verify" / ATTEMPT / "verdicts.jsonl")
    assert [m["id"] for m in row["members"]] == [synth["id"]]
    assert row["members"][0]["seats"] == ["review-pr:verify-2"]
    short = [f for f in friction_log(rdir) if f["kind"] == "verifier-shortfall"]
    assert len(short) == 1
    assert synth["id"] in short[0]["note"] and "1 verifier seat(s)" in short[0]["note"]


def test_a_verifier_finding_with_no_line_leaves_a_coercion_trace(tmp_path, fake_rafiki):
    # W8 residual: a verifier-supplied claim finding reaches the pool unnormalized,
    # so it is where an absent line is still visible and the coercion must be named.
    home, rdir = make_run(tmp_path, checklist=[CLAIM])
    fake_rafiki.presets = PRESETS
    finding = {"title": "the input reaches the parser unguarded", "category": "defect",
               "file": "src/foo.go", "line": None, "severity": "S3", "claim": "bad input is parsed",
               "evidence": "foo.go parses it with no guard", "repro": "n/a"}
    arm(fake_rafiki, "claim-00-verify-1",
        {"holds": False, "reasoning": "no guard exists on that path", "finding": finding})
    arm(fake_rafiki, "claim-00-verify-3", {"holds": True, "reasoning": "the guard is at foo.go:9",
                                           "finding": None})
    mid = pr_review.finding_id({**finding, "line": 0})
    for i, seat in enumerate(("review-pr:verify-2", "review-pr:verify-3")):
        arm(fake_rafiki, vkey(i, mid, seat), verify_answer(False))
    run_stage(fake_rafiki, home)

    coerced = [f for f in friction_log(rdir) if f["kind"] == "line-coerced"]
    assert len(coerced) == 1
    assert coerced[0]["seat"] == "review-pr:verify-1"
    assert mid in coerced[0]["note"] and "line None" in coerced[0]["note"]
    (row,) = jsonl(rdir / "verify" / ATTEMPT / "verdicts.jsonl")
    assert row["verdict"] == "confirmed" and row["members"][0]["line"] == 0


def test_a_finished_run_is_not_flipped_back_by_a_reporting_failure(tmp_path):
    # The guard in leave_verify_failed, on its own: after the stage's final save only
    # reporting can raise (a closed stdout, say), and flipping a done run back to
    # "verifying" would send the next operator to re-verify verdicts that exist.
    _home, rdir = make_run(tmp_path)
    state = {"status": "done", "verify": {"attempt": ATTEMPT}}
    pr_verify.leave_verify_failed(rdir, state, BrokenPipeError("stdout closed"))

    assert state == {"status": "done", "verify": {"attempt": ATTEMPT}}
    assert json.loads((rdir / "state.json").read_text())["status"] == "verifying"
    (failed,) = [f for f in friction_log(rdir) if f["kind"] == "verify-failed"]
    assert "finished" in failed["note"] and "BrokenPipeError" in failed["note"]


def test_lean_fanout_reports_a_nothing_claim_without_verifying_it(tmp_path, fake_rafiki):
    # The default: "checked: nothing" stays the seat's answer in checklist.jsonl
    # and no verifier is spawned for it; a "findings" answer with nothing behind
    # it is still a breach, since that check costs no child.
    lean = {"confirm_over_tokens": 150000, "panel": PANEL, "verifiers": VERIFIERS,
            "finder": "review-pr:finder", "gather": "review-pr:gather", "lens_dir": None}
    unbacked = {"lens": "lens-d", "answer": "findings", "raised_by": "review-pr:panel-2"}
    home, rdir = make_run(tmp_path, checklist=[CLAIM, unbacked], pr=lean)
    fake_rafiki.presets = PRESETS
    run_stage(fake_rafiki, home)

    assert fake_rafiki.seat_spawns == []
    assert jsonl(rdir / "verify" / ATTEMPT / "claims.jsonl") == []
    breaches = [f for f in jsonl(rdir / "friction.jsonl") if f["kind"] == "instruction-breach"]
    assert [f["seat"] for f in breaches] == ["review-pr:panel-2"]
    assert "lens-d" in breaches[0]["note"]


def test_every_verifier_task_carries_the_project_s_tools(tmp_path, fake_rafiki):
    home, rdir, keys = panel_raiser_run(tmp_path, fake_rafiki)
    (rdir / "project" / "tools.md").write_text("Use mcp__codescan__code_symbol on tree code.demo.\n")
    for key in keys:
        arm(fake_rafiki, key, verify_answer(True))
    run_stage(fake_rafiki, home)
    tasks = [s["instruction"] for s in fake_rafiki.seat_spawns]
    assert tasks and all("Use mcp__codescan__code_symbol on tree code.demo." in t for t in tasks)
