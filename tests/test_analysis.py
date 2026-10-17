# tests: the reference checks over a finding, and the run's grading sheet, which is
# the one sanctioned edit to a finished run (the human disposition the delta reads)
"""What these tests pin down: a finding under a dot-directory is not a
hallucination (the prefix is stripped, not its characters), and a human
disposition is written into the run's grading sheet beside the calibration grade,
never in place of it, with both readers taking their own field and the sheet kept
byte-stable. No stage, no daemon, no model.
"""
import json

import pytest

from review_swarm import analysis
from review_swarm.common import Fail


def tree(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("on: push\njobs:\n  build:\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("a = 1\nb = 2\n")
    return tmp_path


def test_a_dot_directory_file_is_not_a_hallucination(tmp_path):
    """`lstrip("./")` strips leading characters, not the prefix, so `.github/...`
    became `github/...` and every finding under a dot-directory was reported as a
    hallucination against the seat that raised it."""
    t = tree(tmp_path)
    for path in (".github/workflows/ci.yml", "./.github/workflows/ci.yml"):
        assert analysis.hallucination_checks(t, {"file": path, "line": 1}) == {"line_out_of_range": False}


def test_a_missing_file_is_still_a_hallucination(tmp_path):
    assert analysis.hallucination_checks(tree(tmp_path), {"file": "src/gone.py", "line": 1}) == {
        "file_missing": True}


def test_set_disposition_creates_the_sheet_and_records_the_outcome(tmp_path):
    rec = analysis.set_disposition(tmp_path, "src/a.py:0", "need-info", "waiting on the PVC decision")
    assert rec == {"issue": "src/a.py:0", "disposition": "need-info", "note": "waiting on the PVC decision"}
    assert (tmp_path / "grades.jsonl").read_text() == json.dumps(rec, sort_keys=True) + "\n"
    assert analysis.dispositions_by_issue(tmp_path) == {"src/a.py:0": "need-info"}


def test_set_disposition_updates_in_place_and_keeps_the_calibration_grade(tmp_path):
    (tmp_path / "grades.jsonl").write_text(json.dumps({"issue": "src/a.py:0", "grade": "real"}) + "\n")
    analysis.set_disposition(tmp_path, "src/a.py:0", "agree")
    assert json.loads((tmp_path / "grades.jsonl").read_text()) == {
        "issue": "src/a.py:0", "grade": "real", "disposition": "agree"}
    # A grade is not a disposition: the sheet keeps both, and each reader takes its own.
    assert analysis.grades_by_issue(tmp_path) == {"src/a.py:0": "real"}
    assert analysis.dispositions_by_issue(tmp_path) == {"src/a.py:0": "agree"}


def test_set_disposition_sorts_the_sheet_for_byte_stability(tmp_path):
    analysis.set_disposition(tmp_path, "src/b.py:0", "agree")
    analysis.set_disposition(tmp_path, "src/a.py:0", "disagree")
    lines = (tmp_path / "grades.jsonl").read_text().splitlines()
    assert [json.loads(l)["issue"] for l in lines] == ["src/a.py:0", "src/b.py:0"]


def test_set_disposition_rejects_an_unknown_outcome(tmp_path):
    with pytest.raises(Fail, match="not one of"):
        analysis.set_disposition(tmp_path, "src/a.py:0", "maybe")


def test_set_disposition_refuses_a_sheet_it_cannot_parse(tmp_path):
    (tmp_path / "grades.jsonl").write_text("{not json\n")
    with pytest.raises(Fail, match="grades.jsonl"):
        analysis.set_disposition(tmp_path, "src/a.py:0", "agree")
    # The sheet a human already wrote is left exactly as it was found.
    assert (tmp_path / "grades.jsonl").read_text() == "{not json\n"
