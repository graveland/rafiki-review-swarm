# tests: compare for PR runs - the configuration label, recall against the gold
# set, precision over the confirmed rows, spend and friction
"""What these tests pin down is what a calibration number is allowed to mean: the
configuration label moves when a lens or a resolved preset moves and not when the
packet merely differs; recall counts only the gold bugs that pin the run's own
head and only the issues the run actually showed, under the member of a bucket
that matched rather than its headline claim; a cost nothing reported stays null
instead of becoming a free run; and an unmeasured recall is null, never zero.
The runs here are hand-built directories: no stage, no daemon, no model.
"""

import json

import pytest

from review_swarm import analysis
from review_swarm.common import Fail
from review_swarm.runs import Home

PROJECT = "mgo"
# A full sha whose prefix is the short commit the gold rows carry, as a run
# records the resolved head and a gold row the PR revision.
HEAD = "c2503dda" + "0" * 32
OTHER = "46906483" + "1" * 32

PANEL = ["review-pr:panel-1", "review-pr:panel-2"]
GATHER = "review-pr:gather"


def gold_entry(bug, commit=HEAD[:8], grade="real", locations=("pkg/a.go:100",), titles=("a title",)):
    return {"bug": bug, "grade": grade, "description": "what goes wrong",
            "locations": list(locations), "found_by": ["human:Verolop"],
            "titles": list(titles), "ref": "mgo-720", "commit": commit,
            "graded_by": "model:first-pass"}


def manifest(lens_shas=("aaa", "bbb"), versions=None, kind=None, total_tokens=10):
    versions = versions or {GATHER: 1, "review-pr:panel-1": 3, "review-pr:panel-2": 1}
    kind = kind or {GATHER: "fundi", "review-pr:panel-1": "claude", "review-pr:panel-2": "fundi"}
    return {"entries": [{"path": "packet/intent.md", "bytes": 4, "tokens": 1}],
            "total_tokens": total_tokens, "mode": "pr", "base": "b" * 40, "head": HEAD,
            "lenses_manifest_sha256": "f" * 64,
            "lenses": [{"name": f"lens-{i}", "sha256": s, "always": False}
                       for i, s in enumerate(lens_shas)],
            "seats": {name: {"version": versions[name], "kind": kind[name], "model": f"m-{name}",
                             "max_cost": 1, "context_window": None}
                      for name in sorted(versions)}}


def member(file, line, verdict, title="a finding", mid=None):
    return {"id": mid or f"{file}:{line}:{title}", "verdict": verdict, "seats": ["review-pr:verify-2"],
            "title": title, "file": file, "line": line, "claim": f"{title}: what goes wrong",
            "raised_by": ["review-pr:panel-2"], "source": "panel"}


def verdict_rows(rows):
    return [{"key": key, "verdict": verdict, "members": members, "votes": [],
             "title": members[0]["title"], "file": members[0]["file"], "line": members[0]["line"],
             "claim": members[0]["claim"], "raised_by": members[0]["raised_by"], "source": ["panel"]}
            for key, verdict, members in rows]


def make_home(tmp_path, gold=()):
    home = Home(tmp_path)
    pdir = home.projects / PROJECT
    (pdir / "gold").mkdir(parents=True)
    (pdir / "project.json").write_text(json.dumps({"name": PROJECT, "target": "~/nowhere"}) + "\n")
    if gold:
        with (pdir / "gold" / "mgo-720.jsonl").open("w") as fh:
            for e in gold:
                fh.write(json.dumps(e, sort_keys=True) + "\n")
    return home


def make_run(home, run_id, *, head=HEAD, packet_manifest=None, verdicts=(), grades=(), children=(),
             friction=(), target=None, review_attempt="01"):
    """A run directory as the stages leave it: run.json, the packet manifest the
    configuration label is read from, the newest verify attempt's verdicts, and
    whatever child metrics and friction the test needs."""
    rdir = home.runs / PROJECT / run_id
    (rdir / "packet").mkdir(parents=True)
    (rdir / "run.json").write_text(json.dumps({
        "run_id": run_id, "kind": "pr", "mode": "pr", "project": {"name": PROJECT},
        "target": target or str(home.path / "target"), "base": "b" * 40, "head": head,
        "tree": str(rdir / "tree")}) + "\n")
    if packet_manifest is not None:
        (rdir / "packet" / "manifest.json").write_text(
            json.dumps(packet_manifest, indent=1, sort_keys=True) + "\n")
    if verdicts:
        vdir = rdir / "verify" / "01"
        vdir.mkdir(parents=True)
        with (vdir / "verdicts.jsonl").open("w") as fh:
            for row in verdicts:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
    if grades:
        with (rdir / "grades.jsonl").open("w") as fh:
            for g in grades:
                fh.write(json.dumps(g, sort_keys=True) + "\n")
    for stage, attempt, key, kid in children:
        cdir = rdir / stage / attempt / key
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "child.json").write_text(json.dumps(kid, sort_keys=True) + "\n")
    # A verify attempt, as the stage leaves it, has written verdicts and names the
    # review attempt it verified.
    for vdir in (rdir / "verify").glob("*") if (rdir / "verify").exists() else ():
        (vdir / "verdicts.jsonl").touch()
        if not (vdir / "state.json").exists():
            (vdir / "state.json").write_text(json.dumps({"review": review_attempt}) + "\n")
    if friction:
        with (rdir / "friction.jsonl").open("w") as fh:
            for e in friction:
                fh.write(json.dumps(e, sort_keys=True) + "\n")
    return rdir


def one_row(home, run_id):
    rows = analysis.compare_pr(home, [run_id])
    assert len(rows) == 1
    return rows[0]


# --- the configuration label ----------------------------------------------------


def test_the_configuration_label_is_the_same_for_the_same_lenses_and_seats(tmp_path):
    home = make_home(tmp_path)
    make_run(home, "20260101-000001", packet_manifest=manifest())
    # Same lens set and same resolved versions, different packet sizes and models:
    # the label is about the configuration, not about what the packet happened to
    # measure.
    other = manifest(total_tokens=999)
    for seat in other["seats"].values():
        seat["model"] = "a different model"
    make_run(home, "20260101-000002", packet_manifest=other)
    assert one_row(home, "20260101-000001")["config"] == one_row(home, "20260101-000002")["config"]


def test_the_configuration_label_changes_when_a_lens_sha_changes(tmp_path):
    home = make_home(tmp_path)
    make_run(home, "20260101-000001", packet_manifest=manifest(lens_shas=("aaa", "bbb")))
    make_run(home, "20260101-000002", packet_manifest=manifest(lens_shas=("aaa", "ccc")))
    assert one_row(home, "20260101-000001")["config"] != one_row(home, "20260101-000002")["config"]


def test_the_configuration_label_changes_when_a_resolved_preset_version_changes(tmp_path):
    home = make_home(tmp_path)
    make_run(home, "20260101-000001", packet_manifest=manifest())
    bumped = manifest(versions={GATHER: 1, "review-pr:panel-1": 4, "review-pr:panel-2": 1})
    make_run(home, "20260101-000002", packet_manifest=bumped)
    assert one_row(home, "20260101-000001")["config"] != one_row(home, "20260101-000002")["config"]


def test_a_run_that_never_gathered_has_no_configuration_label(tmp_path):
    home = make_home(tmp_path)
    make_run(home, "20260101-000001", packet_manifest=None)
    assert one_row(home, "20260101-000001")["config"] is None


# --- recall ---------------------------------------------------------------------


def test_recall_counts_only_the_gold_bugs_pinned_to_the_run_head(tmp_path):
    gold = [gold_entry("head-bug", locations=("pkg/a.go:100",)),
            gold_entry("other-commit-bug", commit=OTHER[:8], locations=("pkg/b.go:40",)),
            gold_entry("head-nit", grade="nit", locations=("pkg/c.go:7",))]
    home = make_home(tmp_path, gold)
    make_run(home, "20260101-000001", packet_manifest=manifest(), verdicts=verdict_rows([
        ("pkg/a.go:3", "confirmed", [member("pkg/a.go", 101, "confirmed")]),
        ("pkg/b.go:1", "confirmed", [member("pkg/b.go", 41, "confirmed")]),
        ("pkg/c.go:0", "confirmed", [member("pkg/c.go", 8, "confirmed")]),
    ]))
    row = one_row(home, "20260101-000001")
    assert row["gold_entries"] == 2 and row["gold_real"] == 1
    assert row["gold_matched"] == ["head-bug", "head-nit"]
    # The nit is reported as matched but is not credited: recall is over the real
    # bugs the head pins, so a matched nit cannot push it past 1.
    assert row["recall"] == 1.0


def test_recall_matches_a_member_of_a_bucket_not_only_its_representative(tmp_path):
    home = make_home(tmp_path, [gold_entry("buried-bug", locations=("pkg/a.go:100",))])
    # The bucket's headline claim sits in the same file but its own member is what
    # points at the gold location.
    make_run(home, "20260101-000001", packet_manifest=manifest(), verdicts=verdict_rows([
        ("pkg/a.go:0", "contested", [member("pkg/a.go", 4, "confirmed", title="headline"),
                                     member("pkg/a.go", 103, "contested", title="buried")]),
    ]))
    row = one_row(home, "20260101-000001")
    assert row["gold_matched"] == ["buried-bug"] and row["recall"] == 1.0


def test_a_refuted_or_incomplete_member_never_matches_gold(tmp_path):
    home = make_home(tmp_path, [gold_entry("gone-bug", locations=("pkg/a.go:100",))])
    make_run(home, "20260101-000001", packet_manifest=manifest(), verdicts=verdict_rows([
        ("pkg/a.go:3", "refuted", [member("pkg/a.go", 101, "refuted")]),
        ("pkg/a.go:6", "incomplete", [member("pkg/a.go", 190, "incomplete")]),
    ]))
    row = one_row(home, "20260101-000001")
    assert row["gold_matched"] == [] and row["recall"] == 0.0


def test_a_missing_or_unmeasured_recall_is_null_never_zero(tmp_path):
    home = make_home(tmp_path, [gold_entry("head-bug", locations=("pkg/a.go:100",)),
                                gold_entry("elsewhere", commit=OTHER[:8])])
    # No verify attempt ran: nothing was shown, so nothing was measured.
    make_run(home, "20260101-000001", packet_manifest=manifest())
    row = one_row(home, "20260101-000001")
    assert row["gold_real"] == 1 and row["recall"] is None and row["precision"] is None
    # A head that pins no real gold bug has no denominator either.
    make_run(home, "20260101-000002", head="f" * 40, packet_manifest=manifest(),
             verdicts=verdict_rows([
                 ("pkg/b.go:0", "confirmed", [member("pkg/b.go", 3, "confirmed")])]))
    assert one_row(home, "20260101-000002")["recall"] is None


# --- precision ------------------------------------------------------------------


def test_precision_is_the_confirmed_rows_matching_gold_or_graded_real(tmp_path):
    home = make_home(tmp_path, [gold_entry("head-bug", locations=("pkg/a.go:100",))])
    make_run(home, "20260101-000001", packet_manifest=manifest(),
             verdicts=verdict_rows([
                 ("pkg/a.go:3", "confirmed", [member("pkg/a.go", 105, "confirmed")]),
                 ("pkg/b.go:1", "confirmed", [member("pkg/b.go", 40, "confirmed", title="graded")]),
                 ("pkg/c.go:1", "confirmed", [member("pkg/c.go", 40, "confirmed", title="not graded")]),
                 ("pkg/d.go:1", "refuted", [member("pkg/d.go", 40, "refuted")]),
                 ("pkg/e.go:1", "contested", [member("pkg/e.go", 40, "contested")]),
             ]),
             grades=[{"issue": "pkg/b.go:1", "grade": "real"},
                     {"issue": "pkg/c.go:1", "grade": ""}])
    row = one_row(home, "20260101-000001")
    # Three confirmed rows, two of them accounted for: gold matched, and a sheet
    # grade of real. A refuted or contested row is not in the denominator.
    assert row["confirmed_rows"] == 3 and row["precision"] == round(2 / 3, 4)


def test_a_mixed_grade_counts_as_real_and_a_nit_does_not(tmp_path):
    home = make_home(tmp_path)
    make_run(home, "20260101-000001", packet_manifest=manifest(),
             verdicts=verdict_rows([
                 ("pkg/a.go:1", "confirmed", [member("pkg/a.go", 1, "confirmed", title="mixed")]),
                 ("pkg/b.go:1", "confirmed", [member("pkg/b.go", 1, "confirmed", title="nit")]),
             ]),
             grades=[{"issue": "pkg/a.go:1", "grade": "mixed"},
                     {"issue": "pkg/b.go:1", "grade": "nit"}])
    assert one_row(home, "20260101-000001")["precision"] == 0.5


# --- spend and friction ---------------------------------------------------------


def child(preset, cost=None, tin=None, tout=None):
    return {"preset": preset, "cost_usd": cost, "input_tokens": tin, "output_tokens": tout,
            "model_turns": 3}


def test_a_child_with_no_cost_reported_is_not_counted_as_a_free_child(tmp_path):
    home = make_home(tmp_path)
    make_run(home, "20260101-000001", packet_manifest=manifest(),
             children=[("review", "01", "panel-2", child("review-pr:panel-2", cost=0.25)),
                       ("verify", "01", "00-x-verify-2", child("review-pr:panel-2", cost=None)),
                       # A metric that is not a number is not a reported cost either.
                       ("verify", "01", "01-y-verify-2", child("review-pr:panel-2", cost=True))])
    row = one_row(home, "20260101-000001")
    assert row["fundi_dollars"] == 0.25 and row["children"] == 3 and row["children_no_cost"] == 2


def test_a_preset_the_manifest_does_not_name_is_never_assumed_fundi(tmp_path):
    home = make_home(tmp_path)
    make_run(home, "20260101-000001", packet_manifest=manifest(),
             children=[("review", "01", "panel-1", child("review-pr:panel-1", tin=10, tout=2)),
                       ("review", "01", "panel-2", child("review-pr:panel-2", cost=0.25, tin=999)),
                       # The manifest names neither of these presets, so the run
                       # records no kind for them: neither cost nor tokens are
                       # booked, and the run report shows them in the same
                       # "kind the run does not record" table.
                       ("verify", "01", "00-x-verify-9", child("review-pr:verify-9", cost=9.5, tin=5)),
                       ("verify", "01", "01-y-verify-9", child("", cost=1.0, tin=500, tout=100))])
    row = one_row(home, "20260101-000001")
    assert row["fundi_dollars"] == 0.25
    assert row["claude_tokens"] == 12     # only the named claude seat is summed
    assert row["children"] == 4 and row["children_unknown_kind"] == 2
    assert row["children_no_cost"] == 0


def test_a_named_preset_with_no_kind_is_a_fundi_seat(tmp_path):
    home = make_home(tmp_path)
    bare = manifest()
    del bare["seats"]["review-pr:panel-2"]["kind"]
    make_run(home, "20260101-000001", packet_manifest=bare,
             children=[("review", "01", "panel-2", child("review-pr:panel-2", cost=0.5))])
    row = one_row(home, "20260101-000001")
    assert row["fundi_dollars"] == 0.5 and row["children_unknown_kind"] == 0


def test_a_run_that_never_gathered_books_no_child_into_a_kind(tmp_path):
    home = make_home(tmp_path)
    make_run(home, "20260101-000001", packet_manifest=None,
             children=[("review", "01", "panel-2", child("review-pr:panel-2", cost=0.25, tin=10))])
    row = one_row(home, "20260101-000001")
    assert row["config"] is None and row["fundi_dollars"] is None and row["claude_tokens"] is None
    assert row["children_unknown_kind"] == 1


def test_a_run_where_nothing_reported_a_cost_has_no_dollar_figure(tmp_path):
    home = make_home(tmp_path)
    make_run(home, "20260101-000001", packet_manifest=manifest(),
             children=[("review", "01", "panel-2", child("review-pr:panel-2", cost=None))])
    assert one_row(home, "20260101-000001")["fundi_dollars"] is None


def test_claude_kind_tokens_are_summed_over_what_was_reported_only(tmp_path):
    home = make_home(tmp_path)
    make_run(home, "20260101-000001", packet_manifest=manifest(),
             children=[("review", "01", "panel-1", child("review-pr:panel-1", tin=100, tout=20)),
                       ("verify", "01", "00-x-verify-1", child("review-pr:panel-1", tin=None, tout=7))])
    assert one_row(home, "20260101-000001")["claude_tokens"] == 127


def test_a_claude_run_that_reported_no_tokens_has_no_token_figure(tmp_path):
    home = make_home(tmp_path)
    make_run(home, "20260101-000001", packet_manifest=manifest(),
             children=[("review", "01", "panel-1", child("review-pr:panel-1", tin=None, tout=None))])
    assert one_row(home, "20260101-000001")["claude_tokens"] is None


def test_friction_is_counted_and_rows_are_sorted_by_run_id(tmp_path):
    home = make_home(tmp_path)
    make_run(home, "20260101-000002", packet_manifest=manifest(),
             friction=[{"stage": "gather", "kind": "spawn-error", "note": "one"}])
    make_run(home, "20260101-000001", packet_manifest=manifest())
    rows = analysis.compare_pr(home, ["20260101-000002", "20260101-000001"])
    assert [r["run"] for r in rows] == ["20260101-000001", "20260101-000002"]
    assert [r["friction"] for r in rows] == [0, 1]


# --- errors stay errors ---------------------------------------------------------


def test_a_verdict_row_without_members_is_a_fail_not_a_zero(tmp_path):
    home = make_home(tmp_path, [gold_entry("head-bug")])
    rdir = make_run(home, "20260101-000001", packet_manifest=manifest())
    (rdir / "verify" / "01").mkdir(parents=True)
    (rdir / "verify" / "01" / "verdicts.jsonl").write_text(json.dumps({"key": "pkg/a.go:3"}) + "\n")
    with pytest.raises(Fail):
        analysis.compare_pr(home, ["20260101-000001"])


def test_a_gold_line_that_does_not_parse_stops_the_comparison(tmp_path):
    home = make_home(tmp_path)
    (home.projects / PROJECT / "gold" / "mgo-720.jsonl").write_text("{not json}\n")
    make_run(home, "20260101-000001", packet_manifest=manifest())
    with pytest.raises(Fail):
        analysis.compare_pr(home, ["20260101-000001"])


def test_an_unknown_run_is_a_fail(tmp_path):
    home = make_home(tmp_path)
    with pytest.raises(Fail):
        analysis.compare_pr(home, ["20260101-999999"])


def test_only_the_graded_attempts_spend_and_friction_count(tmp_path):
    # A rerun leaves the abandoned review attempt 01 in the run. The grade comes
    # from verify 01, which verified review 02, so 01's child and its friction
    # belong to no grade; an untagged operator note is counted apart.
    home = make_home(tmp_path)
    make_run(home, "20260101-000001", packet_manifest=manifest(), review_attempt="02",
             children=[("review", "01", "panel-2", child("review-pr:panel-2", cost=9.0)),
                       ("review", "02", "panel-2", child("review-pr:panel-2", cost=0.25)),
                       ("verify", "01", "00-x-verify-2", child("review-pr:panel-2", cost=0.5))],
             friction=[{"stage": "gather", "kind": "self", "note": "g"},
                       {"stage": "review", "attempt": "01", "kind": "parse-error", "note": "old"},
                       {"stage": "review", "attempt": "02", "kind": "parse-error", "note": "new"},
                       {"stage": "verify-pr", "attempt": "01", "kind": "self", "note": "v"},
                       {"kind": "operator", "note": "untagged"}])
    row = one_row(home, "20260101-000001")
    assert row["review"] == "02" and row["verify"] == "01"
    assert row["fundi_dollars"] == 0.75 and row["children"] == 2
    assert row["friction"] == 3 and row["friction_untagged"] == 1
