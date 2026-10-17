# tests: the run report and the friction rollup - what a review shows, what it
# refuses to drop, and the one thing it must not do (write into the run)
"""The report is the surface a session reads, so these tests pin what it shows:
confirmed findings ordered by severity, every side of a contested one, an
incomplete cluster as its own verdict rather than a dismissal, and a refuted
finding absent (it stays in the run for calibration). They also pin the two
rules that are easy to get wrong: a cost that was not reported is not $0, and
rendering a run never touches it.
"""

import hashlib
import json
import shutil

import pytest

from review_swarm import pr_report
from review_swarm.common import Fail
from review_swarm.runs import Home

RUN_ID = "20260101-000000"

# Ids the run records. The report orders and labels by the finding's own
# severity, which it reads back through these ids from candidates.jsonl and
# claims.jsonl, so the fixture uses ids, not titles, to tie the two together.
M1, M2, M3, M4, M5 = ("a" * 12, "b" * 12, "c" * 12, "d" * 12, "e" * 12)

CANDIDATES = [
    {"id": M1, "title": "leaks a socket", "file": "src/a.go", "line": 10, "severity": "S2",
     "claim": "the socket is never closed", "raised_by": "review-pr:panel-2", "source": "panel"},
    {"id": M2, "title": "off by one", "file": "src/b.go", "line": 30, "severity": "S1",
     "claim": "the slice drops the last element", "raised_by": "review-pr:panel-3", "source": "panel"},
    {"id": M5, "title": "refuted thing", "file": "src/d.go", "line": 3, "severity": "S2",
     "claim": "this is not real", "raised_by": "review-pr:finder", "source": "finder:lens-a"},
]

# A refuted checked-nothing claim lives only here, so its severity comes from
# this list of findings, not from candidates.jsonl. An INCOMPLETE claim is not a
# finding at all and is not in verdicts.jsonl either: the report is the one place
# it can appear, and it must.
CLAIMS = [
    {"key": "claim:00:lens-x", "lens": "lens-x", "raised_by": "review-pr:panel-2",
     "reason": "the guard rejects it", "verdict": "refuted",
     "votes": [{"seat": "review-pr:verify-1", "holds": False,
                "reasoning": "no guard exists on that path"}],
     "findings": [{"id": M3, "title": "unchecked claim", "file": "src/c.go", "line": 5,
                   "severity": "S3", "claim": "nothing is checked", "source": "claim:lens-x",
                   "raised_by": "review-pr:verify-1"}]},
    {"key": "claim:01:lens-y", "lens": "lens-y", "raised_by": "review-pr:panel-3",
     "reason": "the retry counter is clamped", "verdict": "incomplete",
     "votes": [{"seat": "review-pr:verify-1", "holds": True,
                "reasoning": "the clamp is at foo:9"},
               {"seat": "review-pr:verify-3", "holds": None, "reasoning": ""}],
     "findings": []},
]


def member(mid, verdict, title, file, line, claim, *, raised_by=("review-pr:panel-2",),
           source="panel", seats=()):
    return {"id": mid, "verdict": verdict, "seats": list(seats), "title": title, "file": file,
            "line": line, "claim": claim, "raised_by": list(raised_by), "source": source}


def votes_for(mid, *pairs):
    return [{"id": mid, "seat": seat, "refuted": refuted, "reasoning": reason,
             "severity_adjustment": None} for seat, refuted, reason in pairs]


VERDICTS = [
    # Confirmed by both verifiers.
    {"key": "src/a.go:0", "title": "leaks a socket", "file": "src/a.go", "line": 10,
     "claim": "the socket is never closed", "verdict": "confirmed",
     "raised_by": ["review-pr:panel-2"], "source": ["panel"],
     "votes": votes_for(M1, ("review-pr:verify-1", False, "no Close call anywhere"),
                        ("review-pr:verify-3", False, "the defer is on the wrong branch")),
     "members": [member(M1, "confirmed", "leaks a socket", "src/a.go", 10,
                        "the socket is never closed", seats=("review-pr:verify-1",
                                                             "review-pr:verify-3"))]},
    # Contested: the two verifiers disagree, and both sides must survive.
    {"key": "src/b.go:1", "title": "off by one", "file": "src/b.go", "line": 30,
     "claim": "the slice drops the last element", "verdict": "contested",
     "raised_by": ["review-pr:panel-3"], "source": ["panel"],
     "votes": votes_for(M2, ("review-pr:verify-1", True, "the loop stops one short on purpose"),
                        ("review-pr:verify-3", False, "the slice drops the last element")),
     "members": [member(M2, "contested", "off by one", "src/b.go", 30,
                        "the slice drops the last element", raised_by=("review-pr:panel-3",),
                        seats=("review-pr:verify-1", "review-pr:verify-3"))]},
    # Incomplete: one vote never carried a boolean, and one issue no fresh child
    # could check at all. Neither is a dismissal.
    {"key": "src/c.go:0", "title": "unchecked claim", "file": "src/c.go", "line": 5,
     "claim": "nothing is checked", "verdict": "incomplete",
     "raised_by": ["review-pr:verify-1"], "source": ["claim:lens-x"],
     "votes": votes_for(M3, ("review-pr:verify-2", None, "")),
     "members": [member(M3, "incomplete", "unchecked claim", "src/c.go", 5, "nothing is checked",
                        raised_by=("review-pr:verify-1",), source="claim:lens-x",
                        seats=("review-pr:verify-2",)),
                 member(M4, "incomplete", "every verifier raised this", "src/c.go", 7, "no check ran",
                        raised_by=("review-pr:panel-1", "review-pr:panel-2"), seats=())]},
    # Refuted: kept in the run, never shown.
    {"key": "src/d.go:0", "title": "refuted thing", "file": "src/d.go", "line": 3,
     "claim": "this is not real", "verdict": "refuted",
     "raised_by": ["review-pr:finder"], "source": ["finder:lens-a"],
     "votes": votes_for(M5, ("review-pr:verify-1", True, "the call is guarded"),
                        ("review-pr:verify-3", True, "no such path")),
     "members": [member(M5, "refuted", "refuted thing", "src/d.go", 3, "this is not real",
                        raised_by=("review-pr:finder",), source="finder:lens-a")]},
]

FRICTION = [
    {"seat": "review-pr:verify-3", "kind": "parse-error", "note": "no fenced json block",
     "at": "2026-01-01T00:00:03Z"},
    {"seat": "", "kind": "verifier-shortfall", "note": "src/c.go:0 ddd: 0 verifier seat(s)",
     "at": "2026-01-01T00:00:01Z"},
    {"seat": "review-pr:verify-1", "kind": "parse-error", "note": "line is not an int",
     "at": "2026-01-01T00:00:02Z"},
]

# One child per seat: finder has a reported cost, a not-reported one, and a claude
# panel seat that reports tokens instead of dollars.
CHILDREN = [
    ("review/01/finder-lens-a/child.json",
     {"preset": "review-pr:finder", "cost_usd": 0.25, "input_tokens": None, "output_tokens": None}),
    ("review/01/finder-lens-b/child.json",
     {"preset": "review-pr:finder", "cost_usd": None, "input_tokens": None, "output_tokens": None}),
    ("review/01/panel-1/child.json",
     {"preset": "review-pr:panel-1", "cost_usd": None, "input_tokens": 900, "output_tokens": 400}),
    ("verify/01/00-aaaa-verify-2/child.json",
     {"preset": "review-pr:verify-2", "cost_usd": None, "input_tokens": None, "output_tokens": None}),
    ("verify/01/00-aaaa-verify-3/child.json",
     {"preset": "review-pr:verify-3", "cost_usd": 0.01, "input_tokens": None, "output_tokens": None}),
]

SEATS = {"review-pr:finder": {"kind": "fundi"}, "review-pr:panel-1": {"kind": "claude"},
         "review-pr:verify-2": {"kind": "fundi"}, "review-pr:verify-3": {"kind": "fundi"}}


def wrj(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True) + "\n")


def make_run(tmp_path, *, mode="pr", verdicts=VERDICTS, children=CHILDREN, friction=FRICTION,
             seats=SEATS, candidates=CANDIDATES, claims=CLAIMS, run_id=RUN_ID):
    home = Home(tmp_path)
    rdir = home.runs / "demo" / run_id
    (rdir / "project").mkdir(parents=True)
    (rdir / "packet").mkdir()
    (rdir / "review" / "01").mkdir(parents=True)
    (rdir / "verify" / "01").mkdir(parents=True)
    (rdir / "run.json").write_text(json.dumps({
        "run_id": run_id, "kind": "pr", "mode": mode, "project": {"name": "demo"}, "pr": 42,
        "base": "0" * 40, "head": "1" * 40, "tree": str(rdir / "tree"),
        "files_changed": ["src/a.go", "src/b.go"]}, sort_keys=True) + "\n")
    (rdir / "packet" / "manifest.json").write_text(json.dumps({"seats": seats}, sort_keys=True) + "\n")
    (rdir / "state.json").write_text(json.dumps({"status": "done"}, sort_keys=True) + "\n")
    wrj(rdir / "review" / "01" / "candidates.jsonl", candidates)
    wrj(rdir / "review" / "01" / "checklist.jsonl",
        [{"lens": "lens-x", "answer": "nothing", "reason": "the guard rejects it",
          "raised_by": "review-pr:panel-2"}])
    wrj(rdir / "verify" / "01" / "verdicts.jsonl", verdicts)
    wrj(rdir / "verify" / "01" / "claims.jsonl", claims)
    (rdir / "verify" / "01" / "state.json").write_text(json.dumps(
        {"review": "01", "source": "review/01/candidates.jsonl"}, sort_keys=True) + "\n")
    wrj(rdir / "friction.jsonl", friction)
    for rel, doc in children:
        p = rdir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc, sort_keys=True) + "\n")
    return home, rdir


def render(tmp_path, **kw):
    _, rdir = make_run(tmp_path, **kw)
    out = tmp_path / "report.md"
    pr_report.write_report(rdir, out)
    return rdir, out.read_text()


def section(text, heading):
    """The body of one `## heading` section, up to the next `## `."""
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if l == f"## {heading}")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start + 1:end])


# --- the report -----------------------------------------------------------------


def test_report_opens_with_the_run_identity_and_a_verdict_line(tmp_path):
    _, text = render(tmp_path)
    assert text.splitlines()[0] == "# Review: demo pr #42"
    # members, not just buckets: a bucket can hold several issues with different
    # verdicts, so the bucket count alone says nothing about what is confirmed
    assert ("Confirmed 1 member in 1 bucket, contested 1 member in 1 bucket, "
            "incomplete 2 members in 1 bucket; highest confirmed severity S2." in text)
    # a refuted member is kept in the run for calibration, and the report says so
    assert "Not shown: 1 refuted member (kept in the run for calibration)." in text


def test_sections_appear_in_the_order_the_session_reads_them(tmp_path):
    _, text = render(tmp_path)
    order = ["## Confirmed", "## Contested", "## Incomplete", "## Triage", "## Process",
             "## Cost", "## Disposition"]
    at = [text.index(h) for h in order]
    assert at == sorted(at)


def test_confirmed_findings_carry_severity_anchor_claim_and_a_vote_summary(tmp_path):
    _, text = render(tmp_path)
    body = section(text, "Confirmed")
    assert "### S2 leaks a socket (confirmed)" in body
    assert "- `src/a.go:10` the socket is never closed" in body
    assert "- raised by: review-pr:panel-2 (source panel)" in body
    assert "2 vote(s), 2 upheld, 0 refuted (review-pr:verify-1, review-pr:verify-3)" in body


def test_confirmed_findings_are_ordered_by_severity_then_file(tmp_path):
    verdicts = [dict(row) for row in VERDICTS]
    # A second confirmed finding at S3 in a file that sorts first: severity wins.
    verdicts.append({**VERDICTS[0], "key": "src/aa.go:0", "title": "less severe", "file": "src/aa.go",
                     "line": 1, "votes": votes_for("f" * 12, ("review-pr:verify-2", False, "clean")),
                     "members": [member("f" * 12, "confirmed", "less severe", "src/aa.go", 1, "minor")]})
    _, rdir = make_run(tmp_path, verdicts=verdicts)
    wrj(rdir / "review" / "01" / "candidates.jsonl",
        CANDIDATES + [{"id": "f" * 12, "title": "less severe", "file": "src/aa.go", "line": 1,
                       "severity": "S3", "claim": "minor", "raised_by": "review-pr:panel-2",
                       "source": "panel"}])
    out = tmp_path / "report.md"
    pr_report.write_report(rdir, out)
    body = section(out.read_text(), "Confirmed")
    assert body.index("### S2 leaks a socket") < body.index("### S3 less severe")


def test_a_confirmed_finding_reports_each_verifiers_severity_adjustment(tmp_path):
    # F3: a confirmed finding is summarised, but the design says adjustments are
    # reported, not averaged away, so each verifier's own adjustment is shown.
    verdicts = [dict(row) for row in VERDICTS]
    votes = votes_for(M1, ("review-pr:verify-1", False, "no Close call anywhere"),
                      ("review-pr:verify-3", False, "the defer is on the wrong branch"))
    votes[0]["severity_adjustment"] = "downgrade"
    votes[1]["severity_adjustment"] = "none"
    verdicts[0] = {**verdicts[0], "votes": votes}
    _, rdir = make_run(tmp_path, verdicts=verdicts)
    out = tmp_path / "report.md"
    pr_report.write_report(rdir, out)
    body = section(out.read_text(), "Confirmed")
    assert ("- severity adjustments: review-pr:verify-1: downgrade, "
            "review-pr:verify-3: none") in body


def test_a_missing_adjustment_is_not_read_as_the_ruling_none(tmp_path):
    # The fixture's confirmed votes carry no severity_adjustment: that is "not
    # reported", never "none", which is a verifier's explicit ruling that the
    # claimed tier stands.
    _, text = render(tmp_path)
    body = section(text, "Confirmed")
    assert ("- severity adjustments: review-pr:verify-1: not reported, "
            "review-pr:verify-3: not reported") in body


def test_severity_adjustments_are_reported_in_seat_order(tmp_path):
    # Two readings of one run are the same bytes: the per-seat adjustments sort
    # by seat, not by the order the votes happen to sit in the row.
    votes = votes_for(M1, ("review-pr:verify-3", False, "upheld"),
                      ("review-pr:verify-1", False, "upheld"))
    votes[0]["severity_adjustment"] = "upgrade"
    votes[1]["severity_adjustment"] = "downgrade"
    assert pr_report.severity_adjustments(votes) == \
        "review-pr:verify-1: downgrade, review-pr:verify-3: upgrade"


def test_a_contested_finding_shows_every_vote_and_both_sides(tmp_path):
    _, text = render(tmp_path)
    body = section(text, "Contested")
    assert "### S1 off by one (contested)" in body
    assert "- review-pr:verify-1: refuted - the loop stops one short on purpose" in body
    assert "- review-pr:verify-3: upheld - the slice drops the last element" in body


def test_an_incomplete_cluster_is_shown_with_whatever_votes_exist(tmp_path):
    _, text = render(tmp_path)
    body = section(text, "Incomplete")
    # the claim-derived finding's severity comes from claims.jsonl
    assert "### S3 unchecked claim (incomplete)" in body
    assert "- review-pr:verify-2: no verdict (the vote is missing or unparseable)" in body
    # an issue no fresh child could check is shown as unchecked, and its unknown
    # severity is a question mark, never a guess
    assert "### S? every verifier raised this (incomplete)" in body
    assert "- no votes: no fresh child could check this issue" in body


def test_an_incomplete_checked_nothing_claim_is_shown_under_incomplete(tmp_path):
    # F2: an always-on lens whose claim nobody resolved is not a finding, so it is
    # nowhere in verdicts.jsonl; the report is the only place it can appear.
    _, text = render(tmp_path)
    body = section(text, "Incomplete")
    assert "### claim: lens-y (incomplete)" in body
    assert "- raised by: review-pr:panel-3" in body
    assert "- reason: the retry counter is clamped" in body
    assert "- review-pr:verify-1: held (not refuted) - the clamp is at foo:9" in body
    assert "- review-pr:verify-3: no verdict (the vote is missing or unparseable)" in body
    # a refuted claim became a finding, so it is shown as a finding, not a claim
    assert "### claim: lens-x" not in text


def test_an_incomplete_claim_alone_is_not_reported_as_nothing_incomplete(tmp_path):
    claims = [c for c in CLAIMS if c["verdict"] == "incomplete"]
    _, text = render(tmp_path, verdicts=[], claims=claims)
    assert "### claim: lens-y (incomplete)" in section(text, "Incomplete")
    assert "(none: every cluster and every checked-nothing claim got a verdict)" not in text


def test_a_refuted_finding_is_not_shown(tmp_path):
    _, text = render(tmp_path)
    assert "refuted thing" not in text
    assert "## Refuted" not in text
    assert "src/d.go" not in text


# Ids for the mixed-verdict buckets: a bucket's own verdict is derived from its
# members, so a member's verdict is not the bucket's and the report must not
# confuse the two.
MC, MI, MT, MR = "7" * 12, "8" * 12, "9" * 12, "5" * 12

MIX_CANDIDATES = CANDIDATES + [
    {"id": MC, "title": "confirmed sibling", "file": "src/x.go", "line": 12, "severity": "S2",
     "claim": "it leaks", "raised_by": "review-pr:panel-2", "source": "panel"},
    {"id": MT, "title": "contested issue", "file": "src/y.go", "line": 5, "severity": "S1",
     "claim": "it is split", "raised_by": "review-pr:panel-3", "source": "panel"},
]

MIX_VERDICTS = [
    # The bucket is incomplete because one member is: the confirmed member is
    # still confirmed.
    {"key": "src/x.go:0", "title": "confirmed sibling", "file": "src/x.go", "line": 12,
     "claim": "it leaks", "verdict": "incomplete",
     "raised_by": ["review-pr:panel-2"], "source": ["panel"],
     "votes": votes_for(MC, ("review-pr:verify-1", False, "clean"))
     + votes_for(MI, ("review-pr:verify-2", None, "")),
     "members": [member(MC, "confirmed", "confirmed sibling", "src/x.go", 12, "it leaks",
                        seats=("review-pr:verify-1",)),
                 member(MI, "incomplete", "incomplete sibling", "src/x.go", 20, "maybe it leaks",
                        seats=("review-pr:verify-2",))]},
    # The bucket is contested because one member split: the refuted member in it
    # is still refuted, and never shown.
    {"key": "src/y.go:0", "title": "contested issue", "file": "src/y.go", "line": 5,
     "claim": "it is split", "verdict": "contested",
     "raised_by": ["review-pr:panel-3"], "source": ["panel"],
     "votes": votes_for(MT, ("review-pr:verify-1", True, "no"), ("review-pr:verify-3", False, "yes"))
     + votes_for(MR, ("review-pr:verify-1", True, "not real"),
                 ("review-pr:verify-3", True, "not real")),
     "members": [member(MT, "contested", "contested issue", "src/y.go", 5, "it is split",
                        raised_by=("review-pr:panel-3",),
                        seats=("review-pr:verify-1", "review-pr:verify-3")),
                 member(MR, "refuted", "refuted sibling", "src/y.go", 9, "not real")]},
]


def test_a_member_is_shown_by_its_own_verdict_not_its_buckets(tmp_path):
    _, text = render(tmp_path, verdicts=MIX_VERDICTS, candidates=MIX_CANDIDATES)
    confirmed = section(text, "Confirmed")
    assert "### S2 confirmed sibling (confirmed)" in confirmed
    assert "incomplete sibling" not in confirmed
    incomplete = section(text, "Incomplete")
    assert "### S? incomplete sibling (incomplete)" in incomplete
    assert "confirmed sibling" not in incomplete
    # it is triaged too, even though its bucket is incomplete
    assert "| S2 `src/x.go:12` confirmed sibling |  |  |" in section(text, "Triage")
    # and the verdict line counts members, so its bucket being incomplete does not
    # hide it from the confirmed count or the severity line
    assert ("Confirmed 1 member in 1 bucket, contested 1 member in 1 bucket, "
            "incomplete 1 member in 1 bucket; highest confirmed severity S2." in text)


def test_a_refuted_member_in_a_contested_bucket_is_not_shown_or_triaged(tmp_path):
    _, text = render(tmp_path, verdicts=MIX_VERDICTS, candidates=MIX_CANDIDATES)
    assert "refuted sibling" not in text
    assert "### S1 contested issue (contested)" in section(text, "Contested")
    assert "| S1 `src/y.go:5` contested issue |  |  |" in section(text, "Triage")
    assert "Not shown: 1 refuted member (kept in the run for calibration)." in text


def test_a_buckets_titles_only_its_own_members_votes(tmp_path):
    # A bucket with two issues would otherwise hand both sets of votes to the
    # first one: each member's votes are read by id.
    verdicts = [dict(row) for row in VERDICTS]
    verdicts[0] = {**verdicts[0],
                   "votes": votes_for(M1, ("review-pr:verify-1", False, "upheld it"),
                                      ("review-pr:verify-3", False, "upheld it"))
                   + votes_for("9" * 12, ("review-pr:verify-1", True, "refuted the other"))}
    _, text = render(tmp_path, verdicts=verdicts)
    body = section(text, "Confirmed")
    assert "2 vote(s), 2 upheld, 0 refuted" in body
    assert "refuted the other" not in body


def test_the_triage_table_has_one_empty_row_per_confirmed_and_contested_finding(tmp_path):
    _, text = render(tmp_path)
    body = section(text, "Triage")
    assert "| finding | disposition | deciding lines |" in body
    assert "| S2 `src/a.go:10` leaks a socket |  |  |" in body
    assert "| S1 `src/b.go:30` off by one |  |  |" in body
    assert "unchecked claim" not in body       # incomplete is not for triage
    assert "refuted thing" not in body


def test_process_groups_friction_by_kind_then_seat(tmp_path):
    _, text = render(tmp_path)
    body = section(text, "Process")
    assert body.index("### parse-error") < body.index("### verifier-shortfall")
    assert body.index("- review-pr:verify-1: line is not an int") \
        < body.index("- review-pr:verify-3: no fenced json block")
    assert "- (stage): src/c.go:0 ddd: 0 verifier seat(s)" in body


def test_cost_splits_fundi_dollars_from_claude_tokens(tmp_path):
    _, text = render(tmp_path)
    body = section(text, "Cost")
    fundi = body[:body.index("Claude-kind seats")]
    assert "| review-pr:finder | 2 | $0.2500 | 1 |" in fundi
    assert "| review-pr:verify-3 | 1 | $0.0100 | 0 |" in fundi
    claude = body[body.index("Claude-kind seats"):]
    assert "| review-pr:panel-1 | 1 | 900 | 400 | 0 |" in claude


def test_an_unreported_cost_is_not_reported_as_zero(tmp_path):
    _, text = render(tmp_path)
    body = section(text, "Cost")
    # verify-2 reported no cost at all: unknown, never $0
    assert "| review-pr:verify-2 | 1 | not reported | 1 |" in body
    assert "$0.0000" not in body


def test_a_seat_kind_the_run_does_not_record_is_reported_apart(tmp_path):
    children = CHILDREN + [
        ("verify/01/00-bbbb-verify-9/child.json",
         {"preset": "review-pr:verify-9", "cost_usd": 0.5,
          "input_tokens": 10, "output_tokens": 5}),
        ("verify/01/00-cccc/child.json",
         {"preset": "", "cost_usd": 0.25, "input_tokens": None, "output_tokens": None}),
    ]
    _, text = render(tmp_path, children=children)
    body = section(text, "Cost")
    assert "Seats whose kind the run does not record" in body
    assert "| review-pr:verify-9 | 1 | $0.5000 | 10 in / 5 out |" in body
    assert "| (no seat) | 1 | $0.2500 | not reported |" in body
    # F8 ruling: an unnamed preset is unknown, never fundi, so neither child is
    # booked into the dollars-by-fundi table
    fundi = body[:body.index("Seats whose kind")]
    assert "review-pr:verify-9" not in fundi
    assert "(no seat)" not in fundi


def test_the_report_is_deterministic(tmp_path):
    _, rdir = make_run(tmp_path)
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    pr_report.write_report(rdir, a)
    pr_report.write_report(rdir, b)
    assert a.read_bytes() == b.read_bytes()


def test_writing_the_report_does_not_modify_the_run(tmp_path):
    _, rdir = make_run(tmp_path)

    def snapshot():
        return {str(p.relative_to(rdir)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                for p in sorted(rdir.rglob("*")) if p.is_file()}

    before = snapshot()
    pr_report.write_report(rdir, tmp_path / "report.md")
    assert snapshot() == before


def test_the_report_refuses_to_be_written_into_the_run(tmp_path):
    _, rdir = make_run(tmp_path)
    with pytest.raises(Fail) as e:
        pr_report.write_report(rdir, rdir / "report.md")
    assert "never edited after it finishes" in str(e.value)
    assert not (rdir / "report.md").exists()


def test_the_inside_run_guard_compares_file_identity(tmp_path):
    _, rdir = make_run(tmp_path)
    assert pr_report.inside_run(rdir, rdir) is True
    assert pr_report.inside_run(rdir / "report.md", rdir) is True
    assert pr_report.inside_run(rdir / "verify" / "01" / "r.md", rdir) is True
    assert pr_report.inside_run(tmp_path / "reports" / "r.md", rdir) is False
    # `..` is folded before the walk, even through a component that does not exist
    # yet: the kernel cannot traverse the missing part to reach its `..`
    assert pr_report.inside_run(tmp_path / "nope" / ".." / "runs" / "demo" / RUN_ID / "r.md",
                                rdir) is True


def test_the_inside_run_guard_collapses_dotdot_through_a_missing_component(tmp_path):
    # Regression from the identity guard: without normalising first, the walk stops
    # at `T/nope`, and the mkdir that follows creates it, after which that path IS
    # the run.
    _, rdir = make_run(tmp_path)
    missing = tmp_path / "nope"
    assert not missing.exists()
    out = missing / ".." / "runs" / "demo" / RUN_ID / "report.md"
    with pytest.raises(Fail) as e:
        pr_report.write_report(rdir, out)
    assert "never edited after it finishes" in str(e.value)
    assert not (rdir / "report.md").exists()
    assert not missing.exists()   # refused before any parent was created


def test_the_inside_run_guard_is_not_fooled_by_a_case_variant(tmp_path):
    # F5: on a case-insensitive filesystem (APFS) a case variant of the run's path
    # IS the run, and resolve() does not canonicalise the case of a path that does
    # not exist yet, so a string comparison writes into the run.
    _, rdir = make_run(tmp_path)
    variant = tmp_path / "RUNS"
    if not variant.exists():
        pytest.skip("case-sensitive filesystem: a case variant is a different path")
    out = variant / "demo" / RUN_ID / "report.md"
    with pytest.raises(Fail) as e:
        pr_report.write_report(rdir, out)
    assert "never edited after it finishes" in str(e.value)
    assert not (rdir / "report.md").exists()


def test_a_run_that_was_never_verified_is_refused(tmp_path):
    _, rdir = make_run(tmp_path)
    shutil.rmtree(rdir / "verify")
    with pytest.raises(Fail) as e:
        pr_report.write_report(rdir, tmp_path / "report.md")
    assert "no verify attempt" in str(e.value)


def test_a_design_run_renders_its_questions(tmp_path):
    _home, rdir = make_run(tmp_path, mode="design")
    wrj(rdir / "review" / "01" / "questions.jsonl",
        [{"question": "who owns the lock?", "raised_by": "review-pr:panel-2"}])
    out = tmp_path / "report.md"
    pr_report.write_report(rdir, out)
    text = out.read_text()
    body = section(text, "Questions")
    assert "- who owns the lock? (raised by review-pr:panel-2)" in body
    assert "## Prior findings" not in text
    assert text.splitlines()[0] == "# Review: demo design (no doc)"


def test_a_delta_run_renders_the_prior_re_judgements(tmp_path):
    _home, rdir = make_run(tmp_path, mode="delta")
    wrj(rdir / "review" / "01" / "prior.jsonl",
        [{"id": M1, "status": "fixed", "reason": "the Close is now deferred",
          "raised_by": "review-pr:panel-3"}])
    out = tmp_path / "report.md"
    pr_report.write_report(rdir, out)
    text = out.read_text()
    body = section(text, "Prior findings")
    assert f"- `{M1}`: fixed - the Close is now deferred (raised by review-pr:panel-3)" in body
    assert "## Questions" not in text


# --- friction rollup ------------------------------------------------------------


def test_friction_rollup_counts_a_run_and_names_it(tmp_path):
    home, _ = make_run(tmp_path)
    rows = pr_report.friction_rollup(home, run=RUN_ID)
    assert rows == [
        {"seat": "", "kind": "verifier-shortfall", "count": 1, "runs": [RUN_ID]},
        {"seat": "review-pr:verify-1", "kind": "parse-error", "count": 1, "runs": [RUN_ID]},
        {"seat": "review-pr:verify-3", "kind": "parse-error", "count": 1, "runs": [RUN_ID]},
    ]


def test_friction_rollup_covers_every_pr_run_of_a_project(tmp_path):
    home = Home(tmp_path)
    made = []
    for run_id, friction in (("20260101-000001", FRICTION),
                             ("20260101-000002", [{"seat": "review-pr:verify-3", "kind": "parse-error",
                                                   "note": "again", "at": "2026-01-02T00:00:00Z"}])):
        _, _rdir = make_run(tmp_path, run_id=run_id, friction=friction)
        made.append(run_id)
    # a run of another kind is not a PR run, and is left out rather than folded in
    other = home.runs / "demo" / "20260101-000003"
    other.mkdir(parents=True)
    (other / "run.json").write_text(json.dumps({"run_id": other.name, "kind": "find"}) + "\n")
    wrj(other / "friction.jsonl", [{"seat": "review-pr:verify-3", "kind": "parse-error", "note": "x",
                                    "at": "2026-01-03T00:00:00Z"}])

    rows = pr_report.friction_rollup(home, project="demo")
    assert [(r["seat"], r["kind"], r["count"], r["runs"]) for r in rows] == [
        ("", "verifier-shortfall", 1, ["20260101-000001"]),
        ("review-pr:verify-1", "parse-error", 1, ["20260101-000001"]),
        ("review-pr:verify-3", "parse-error", 2, ["20260101-000001", "20260101-000002"]),
    ]


def test_friction_rollup_orders_by_seat_then_kind(tmp_path):
    friction = [{"seat": "b", "kind": "k2", "note": "1", "at": "t"},
                {"seat": "b", "kind": "k1", "note": "2", "at": "t"},
                {"seat": "a", "kind": "k2", "note": "3", "at": "t"},
                {"seat": "a", "kind": "k1", "note": "4", "at": "t"}]
    home, _ = make_run(tmp_path, friction=friction)
    rows = pr_report.friction_rollup(home, run=RUN_ID)
    assert [(r["seat"], r["kind"]) for r in rows] == [("a", "k1"), ("a", "k2"), ("b", "k1"), ("b", "k2")]


@pytest.mark.parametrize("kw", [{}, {"run": RUN_ID, "project": "demo"}])
def test_friction_rollup_needs_exactly_one_target(tmp_path, kw):
    home, _ = make_run(tmp_path)
    with pytest.raises(Fail) as e:
        pr_report.friction_rollup(home, **kw)
    assert "exactly one of run= or project=" in str(e.value)
