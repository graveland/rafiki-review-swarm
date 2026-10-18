# tests: what the operator is told about fit at confirm time, and the design-mode
# variant the two PR prompts carry.
"""The contract these tests pin down: the confirm path and `pr status` say in
words that fit is UNCHECKED when no seat reports a context window, and why, so a
null window is never read as a claim of fit; a seat whose reported window cannot
hold the packet is still named with its way out; both PR prompts carry a design
variant (context is the code the doc names) while keeping the pr-mode wording and
the `{name}` placeholders render_prompt fills; and none of these files carries an
em-dash."""
import json
from pathlib import Path
from types import SimpleNamespace

from review_swarm import cli, pr_state
from review_swarm.common import PACKAGE, render_prompt
from review_swarm.runs import Home

MY_FILES = [Path(cli.__file__), PACKAGE / "prompts" / "task-pr-gather.md",
            PACKAGE / "prompts" / "task-pr-find.md", PACKAGE / "skills" / "review" / "SKILL.md"]


def estimate(not_reported=(), blocking=(), total=4000):
    """An estimate shaped like pr_gather.estimate's: the seats it could not check
    carry `context_window: null` and a `not_reported` row, and the seats it could
    carry a window and, when the packet is over it, a `blocking` row."""
    seats = {
        "review-pr:finder": {"kind": "fundi", "model": "openrouter/z-ai/glm-5.3-flash",
                             "context_window": None},
        "review-pr:panel-1": {"kind": "claude", "model": "claude-opus-5-5", "context_window": None},
        "review-pr:panel-2": {"kind": "fundi", "model": "openrouter/z-ai/glm-5.3-flash",
                              "context_window": 100},
    }
    for b in blocking:
        seats[b["seat"]]["context_window"] = 100
    return {"total_tokens": total, "seats": seats, "blocking": list(blocking),
            "not_reported": list(not_reported)}


def make_run(tmp_path, est, status="awaiting-confirm"):
    """A run directory with the pieces `pr confirm` and `pr status` read, and
    nothing else: no packet, no seat, no daemon."""
    home = Home(tmp_path)
    rdir = home.runs / "demo" / "20260101-000000"
    rdir.mkdir(parents=True)
    (rdir / "run.json").write_text(json.dumps({
        "run_id": "20260101-000000", "project": {"name": "demo"}, "mode": "pr",
        "target": "/tmp/demo", "head": "0123456789abcdef"}) + "\n")
    pr_state.save_state(rdir, {"status": status,
                               "gather": {"estimate": est, "confirm_over_tokens": 150000}})
    return home, rdir


def test_fit_is_unchecked_is_said_plainly_with_the_reason_and_the_seats():
    lines = cli.fit_report(estimate(not_reported=[{"seat": "review-pr:finder",
                                                   "reason": "context window not reported"},
                                                  {"seat": "review-pr:panel-1",
                                                   "reason": "context window not reported"}]))
    (line,) = lines
    assert line.startswith("fit UNCHECKED:")
    assert "review-pr:finder, review-pr:panel-1" in line
    assert "their presets carry none" in line and "the SDK has no model catalog" in line
    # The whole point of the ruling: no silent claim of fit.
    assert "not a claim that the packet" in line and "fits any of those seats" in line
    assert "4000 tokens" in line
    assert "parse-error friction" in line


def test_a_seat_the_window_cannot_hold_keeps_its_blocking_line_and_its_way_out():
    lines = cli.fit_report(estimate(not_reported=[{"seat": "review-pr:finder",
                                                   "reason": "context window not reported"}],
                                    blocking=[{"seat": "review-pr:panel-2",
                                               "reason": "packet exceeds context"}]))
    assert lines[0].startswith("fit UNCHECKED:")
    (blocking,) = lines[1:]
    assert blocking.startswith("blocking: review-pr:panel-2 (openrouter/z-ai/glm-5.3-flash, fundi):")
    assert "packet exceeds context" in blocking
    assert "confirm runs it anyway" in blocking and "start a new run" in blocking


def test_a_fully_reported_estimate_says_fit_was_checked():
    assert cli.fit_report({"total_tokens": 10, "seats": {}, "blocking": [], "not_reported": []}) == [
        "fit checked: every seat's context window is reported and the packet is inside it."]


def test_fit_report_is_the_same_lines_for_the_same_estimate():
    est = estimate(not_reported=[{"seat": "review-pr:panel-1", "reason": "context window not reported"}])
    assert cli.fit_report(est) == cli.fit_report(json.loads(json.dumps(est, sort_keys=True)))


def test_confirm_prints_the_unchecked_line_and_still_confirms_the_run(tmp_path, capsys):
    est = estimate(not_reported=[{"seat": "review-pr:finder", "reason": "context window not reported"}])
    home, rdir = make_run(tmp_path, est)
    cli.pr_confirm(home, SimpleNamespace(run="20260101-000000"))
    out = capsys.readouterr().out
    assert json.dumps(est, indent=1, sort_keys=True) in out
    assert "fit UNCHECKED: no context window is reported for review-pr:finder" in out
    assert "run 20260101-000000: confirmed" in out
    assert json.loads((rdir / "state.json").read_text())["status"] == "confirmed"


def test_confirm_names_a_blocked_seat_without_dropping_it(tmp_path, capsys):
    est = estimate(not_reported=[{"seat": "review-pr:finder", "reason": "context window not reported"}],
                   blocking=[{"seat": "review-pr:panel-2", "reason": "packet exceeds context"}])
    home, _ = make_run(tmp_path, est)
    cli.pr_confirm(home, SimpleNamespace(run="20260101-000000"))
    out = capsys.readouterr().out
    assert "blocking: review-pr:panel-2 (openrouter/z-ai/glm-5.3-flash, fundi): packet exceeds context" in out
    assert "confirm runs it anyway" in out


def test_status_prints_the_same_fit_lines(tmp_path, capsys):
    est = estimate(not_reported=[{"seat": "review-pr:finder", "reason": "context window not reported"}])
    home, _ = make_run(tmp_path, est, status="confirmed")
    cli.pr_status(home, SimpleNamespace(run="20260101-000000"))
    out = capsys.readouterr().out
    assert "  fit UNCHECKED: no context window is reported for review-pr:finder" in out
    assert "blocking:" not in out


def test_the_finder_prompt_carries_a_design_variant_and_keeps_the_pr_wording():
    text = render_prompt("task-pr-find.md", mode="design", scope="the whole change",
                         lens_name="liveness", lens_prompt="Does it die?")
    assert "MODE: design" in text
    assert "DESIGN MODE VARIANT" in text and "MODE is design" in text
    # The design's own words for design-mode context.
    assert "the code the doc names" in text
    # The pr-mode wording survives untouched, and the placeholders still render.
    assert "One lens over the change, inside your scope and nothing else." in text
    assert "A finding needs a reachable path: file and line" in text
    assert "LENS - liveness: Does it die?" in text


def test_the_gather_prompt_carries_a_design_variant_and_keeps_the_pr_wording():
    text = render_prompt("task-pr-gather.md", intent="# Doc\n", changed_files="(no changed files)",
                         budget_tokens="40000")
    assert "DESIGN MODE VARIANT" in text and "the mode is design" in text
    assert "the code the doc names" in text
    assert "For the symbols this change touches (added, modified, or newly called)" in text
    assert "{budget_tokens}" not in text and "roughly 40000 tokens" in text
    assert "roughly {budget_tokens} tokens" not in text
    assert text.count("# Doc") == 1


def test_my_files_carry_no_em_dash():
    for f in MY_FILES:
        assert "\u2014" not in f.read_text(), f"{f} carries an em-dash"


def test_pr_dispose_records_a_disposition_in_the_runs_grading_sheet(tmp_path, capsys):
    """The F6 write side: `pr dispose` is the one verb that writes the sanctioned
    human annotation, the run's grading sheet, and it writes nothing else."""
    home, rdir = make_run(tmp_path, estimate(), status="done")
    cli.pr_dispose(home, SimpleNamespace(run="20260101-000000", issue_id="pkg/a.go:0",
                                        disposition="agree", note="the fix lands"))
    assert [json.loads(l) for l in (rdir / "grades.jsonl").read_text().splitlines()] == [
        {"issue": "pkg/a.go:0", "disposition": "agree", "note": "the fix lands"}]
    assert "20260101-000000: pkg/a.go:0 agree (the fix lands)" in capsys.readouterr().out


def test_pr_dispose_updates_the_record_and_keeps_its_calibration_grade(tmp_path):
    home, rdir = make_run(tmp_path, estimate(), status="done")
    (rdir / "grades.jsonl").write_text(json.dumps({"issue": "pkg/a.go:0", "grade": "real"}) + "\n")
    cli.pr_dispose(home, SimpleNamespace(run="20260101-000000", issue_id="pkg/a.go:0",
                                        disposition="disagree", note=None))
    assert [json.loads(l) for l in (rdir / "grades.jsonl").read_text().splitlines()] == [
        {"issue": "pkg/a.go:0", "grade": "real", "disposition": "disagree"}]


def test_swarm_main_parses_and_runs_pr_dispose_as_a_verb(tmp_path):
    home, rdir = make_run(tmp_path, estimate(), status="done")
    assert cli.swarm_main(["--home", str(home.path), "--data", str(home.data),
                           "pr", "dispose", "20260101-000000",
                           "pkg/a.go:0", "need-info", "--note", "more data needed"]) == 0
    assert json.loads((rdir / "grades.jsonl").read_text()) == {
        "issue": "pkg/a.go:0", "disposition": "need-info", "note": "more data needed"}


def test_every_spawning_stage_takes_parallel_with_the_stage_default():
    from review_swarm import stages
    ap = cli.stage_parser("review")
    for stage in ("find", "verify", "review", "verify-pr"):
        assert ap.parse_args(["--home", "/h", "--data", "/d", stage, "r1"]).parallel == stages.FIND_CAP
        assert ap.parse_args(["--home", "/h", "--data", "/d", stage, "r1", "--parallel", "40"]).parallel == 40
