# tests: the gather stage - the packet's fixed order, the estimate, the confirm stop
"""The contract these tests pin down: files.txt is the byte-identical prefix the
review seats share and is never reordered, two builds of the same inputs are
byte-identical, the estimate splits fundi dollars from claude-kind volume, a
packet that does not fit a seat's window blocks that seat, a gather seat that
fails or answers badly leaves friction and an empty (never cut) packet, and the
run stops at awaiting-confirm when the operator has to decide. The repository
under review is a real two-commit git repo; gh is never called."""
import hashlib
import json
import subprocess
from types import SimpleNamespace

import pytest

from review_swarm import analysis, pr_gather, pr_runs, pr_state
from review_swarm.common import Fail
from review_swarm.runs import Home
from review_swarm.stages import child_metrics

DAEMON = {"target": "live", "version": "test"}
GLM = "openrouter/z-ai/glm-5.3-flash"
GPT = "openrouter/openai/gpt-5.6-sol"
GATHER_MODEL = "openrouter/~deepseek/deepseek-flash-latest"

# The wide fan-out: these tests pin the machinery each setting widens; the lean
# defaults have their own tests.
PR_JSON = {
    "confirm_over_tokens": 150000,
    "finders": "all", "split_groups": True, "verify_claims": True,
    "panel": ["review-pr:panel-1", "review-pr:panel-2", "review-pr:panel-3"],
    "verifiers": {"review-pr:panel-1": "review-pr:verify-1", "review-pr:panel-2": "review-pr:verify-2",
                  "review-pr:panel-3": "review-pr:verify-3"},
    "finder": "review-pr:finder",
    "gather": "review-pr:gather",
    "lens_dir": None,
}

# Every seat carries its window in its preset here, because nothing else reports
# one (the shipped presets do not and the pinned SDK has no catalog): a seat with
# no window is informational and never blocks, which is what the
# test_an_unreported_window_* and test_the_estimate_is_usable_* tests pin.
SEATS = {
    "review-pr:gather": {"kind": "fundi", "model": GATHER_MODEL, "max_cost": 0.5, "context_window": 128000},
    "review-pr:finder": {"kind": "fundi", "model": GLM, "max_cost": 0.3, "context_window": 200000},
    "review-pr:panel-1": {"kind": "claude", "model": "claude-opus-5-5", "max_cost": 3, "context_window": 200000},
    "review-pr:panel-2": {"kind": "fundi", "model": GLM, "max_cost": 1, "context_window": 200000},
    "review-pr:panel-3": {"kind": "fundi", "model": GPT, "max_cost": 2, "context_window": 400000},
    "review-pr:verify-1": {"kind": "claude", "model": "claude-opus-5-5", "max_cost": 0.5, "context_window": 200000},
    "review-pr:verify-2": {"kind": "fundi", "model": GLM, "max_cost": 0.2, "context_window": 200000},
    "review-pr:verify-3": {"kind": "fundi", "model": GPT, "max_cost": 0.4, "context_window": 400000},
}

GATHER_ANSWER = """I picked the ranges that decide whether the change is safe.

```json
{"context": [{"path": "src/a.py", "start": 1, "end": 2, "why": "the changed line"},
             {"path": "README.md", "why": "how the change is reached"}],
 "file_groups": [{"name": "core", "paths": ["src/a.py"]}],
 "process_notes": ["the packet disagrees with src/a.py line numbers"]}
```
"""

def fake_gh(*args):
    """Review comments on the pulls API, one issue comment on the issues API, so
    both sections and their rendering have something to show."""
    if "/pulls/" in args[1]:
        return [{"path": "src/a.py", "line": 2, "user": {"login": "reviewer"},
                 "body": "this line changes behaviour"},
                {"path": "src/a.py", "line": 1, "user": {"login": "author"}, "body": "why not"}]
    return [{"user": {"login": "drive-by"}, "body": "any update?"}]

# The exact packet prefix, in the order the seats depend on.
FILES_TXT = ["packet/intent.md", "packet/diff.patch", "packet/threads.md", "packet/prior.json",
             "packet/lenses.json", "tree/src/a.py", "tree/src/b.py",
             "tree/src/a.py:1-2", "tree/README.md"]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def two_commit_repo(root, remote=None):
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "T")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("a = 1\n")
    (root / "README.md").write_text("hi\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "one")
    first = git(root, "rev-parse", "HEAD").strip()
    (root / "src" / "a.py").write_text("a = 2\n")
    (root / "src" / "b.py").write_text("b = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "two")
    if remote:
        git(root, "remote", "add", "origin", remote)
    return first, git(root, "rev-parse", "HEAD").strip()


def write_lenses(d):
    """One always-on lens and one triggered by a changed path, so the table order
    and the `always` flag both have something to distinguish."""
    d.mkdir(parents=True)
    (d / "always-lens.md").write_text(
        "---\nname: always-lens\ncategory: defect\napplies_to: [design, pr, delta]\n"
        "triggers: always\ncatches: [known-bug]\n---\n\nAlways applies.\n")
    (d / "paths-lens.md").write_text(
        "---\nname: paths-lens\ncategory: quality\napplies_to: [pr, delta]\n"
        "triggers:\n  paths: [\"src/b.py\"]\n---\n\nOnly when b.py moves.\n")
    return d


def make_home(tmp_path, target, pr_json=None):
    home = Home(tmp_path / "home")
    pdir = home.projects / "demo"
    (pdir / "gold").mkdir(parents=True)
    (pdir / "project.json").write_text(json.dumps({"name": "demo", "target": str(target)}) + "\n")
    (pdir / "pr.json").write_text(json.dumps(PR_JSON if pr_json is None else pr_json) + "\n")
    (pdir / "gold" / "bugs.jsonl").write_text(
        json.dumps({"bug": "known-bug", "grade": "real", "locations": ["src/a.py:2"]}) + "\n")
    return home


def since_run(home, first, *, rows=None, status="done", project="demo"):
    """A finished run the delta compares against, with the verdicts prior.json
    is built from. It is `done` and this project's, which is what `since_run_dir`
    requires of a prior: an unfinished or foreign run is refused (the
    test_a_since_run_* tests pin that), never silently emptied."""
    sdir = home.runs / "demo" / "20240101-000000"
    (sdir / "verify" / "01").mkdir(parents=True)
    (sdir / "run.json").write_text(json.dumps(
        {"run_id": "20240101-000000", "head": first, "project": {"name": project}}) + "\n")
    pr_state.save_state(sdir, {"status": status, "run_id": "20240101-000000"})
    if rows is None:
        rows = [{"id": "abc", "title": "stale read", "file": "src/a.py", "line": 2, "claim": "reads old a",
                 "verdict": "confirmed", "extra": "not carried"},
                {"id": "def", "title": "maybe", "file": "src/b.py", "line": 1, "claim": "races", "verdict": "contested"},
                {"id": "ghi", "title": "dead", "file": "src/a.py", "line": 1, "claim": "unused", "verdict": "refuted"},
                # The verify stage's own rows key a cluster `key`, not `id`.
                {"key": "jkl", "title": "unset", "file": "src/a.py", "line": 1, "claim": "no owner",
                 "verdict": "confirmed"}]
    (sdir / "verify" / "01" / "verdicts.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows))
    return "20240101-000000"


@pytest.fixture
def run_dir(tmp_path, fake_rafiki, monkeypatch):
    """A delta run ready to gather: real repo, real frozen trees, fake rafiki."""
    target = tmp_path / "repo"
    first, head = two_commit_repo(target, remote="https://github.com/o/r.git")
    home = make_home(tmp_path, target)
    lens_generic = write_lenses(tmp_path / "lenses")
    monkeypatch.setattr(pr_runs, "gh_pr_view",
                        lambda pr, repo: {"baseRefOid": first, "headRefOid": head,
                                          "title": "Add b", "body": "Why b exists."})
    monkeypatch.setattr(pr_gather, "gh_json", fake_gh)
    rdir, run = pr_runs.pr_run_new(home, "demo", "delta", pr=7, since_run=since_run(home, first),
                                  daemon=DAEMON)
    fake_rafiki.presets.update(SEATS)
    fake_rafiki.answers["gather-"] = GATHER_ANSWER
    return SimpleNamespace(rdir=rdir, run=run, home=home, rk=fake_rafiki,
                           lens_generic=lens_generic, first=first, head=head)


def build(run_dir):
    return pr_gather.build_packet(run_dir.rk, run_dir.home, run_dir.rdir,
                                  lens_generic=run_dir.lens_generic)


def test_files_txt_is_exactly_the_specified_order(run_dir):
    build(run_dir)
    assert (run_dir.rdir / "packet" / "files.txt").read_text() == "\n".join(FILES_TXT) + "\n"


def test_the_home_lens_layer_reaches_the_packet(run_dir):
    d = run_dir.home.path / "lenses"
    d.mkdir()
    (d / "house.md").write_text("---\nname: house\ncategory: quality\napplies_to: [pr, delta]\n"
                                "triggers: always\n---\n\nHouse rule.\n")
    build(run_dir)
    manifest = json.loads((run_dir.rdir / "project" / "lenses.manifest.json").read_text())
    assert "house" in [lens["name"] for lens in manifest["lenses"]]


def test_two_builds_of_the_same_inputs_are_byte_identical(run_dir):
    build(run_dir)
    first = [(run_dir.rdir / "packet" / n).read_bytes() for n in ("files.txt", "manifest.json")]
    build(run_dir)
    second = [(run_dir.rdir / "packet" / n).read_bytes() for n in ("files.txt", "manifest.json")]
    assert first == second


def test_the_gather_seat_gets_the_intent_the_diff_and_the_changed_files_last(run_dir):
    build(run_dir)
    (spawn,) = run_dir.rk.seat_spawns
    assert spawn["preset"] == "review-pr:gather"
    assert spawn["cwd"] == str(run_dir.rdir)
    assert spawn["packet_files"] == ["packet/intent.md", "packet/diff.patch",
                                     "tree/src/a.py", "tree/src/b.py"]
    # The seat's instruction is rendered, not a raw template, and it carries the
    # PR's own words as the intent.
    assert "# Add b" in spawn["instruction"]
    assert "{budget_tokens}" not in spawn["instruction"]
    assert spawn["instruction"].rstrip().endswith("```")


def test_the_manifest_sizes_every_entry_and_records_the_seats(run_dir):
    manifest = build(run_dir)
    packet = run_dir.rdir / "packet"
    assert [e["path"] for e in manifest["entries"]] == FILES_TXT
    assert manifest["total_tokens"] == sum(e["tokens"] for e in manifest["entries"])
    for e in manifest["entries"]:
        assert e["tokens"] == e["bytes"] // 4
    assert manifest["base"] == run_dir.first and manifest["head"] == run_dir.head
    assert manifest["lenses_manifest_sha256"] == hashlib.sha256(
        (run_dir.rdir / "project" / "lenses.manifest.json").read_bytes()).hexdigest()
    assert sorted(manifest["seats"]) == sorted(SEATS)
    for name, spec in SEATS.items():
        assert manifest["seats"][name]["kind"] == spec["kind"]
        assert manifest["seats"][name]["model"] == spec["model"]
        assert manifest["seats"][name]["max_cost"] == spec["max_cost"]
    assert [l["name"] for l in manifest["lenses"]] == ["always-lens", "paths-lens"]
    # Written sorted, so its bytes are reproducible.
    assert (packet / "manifest.json").read_text() == json.dumps(manifest, indent=1, sort_keys=True) + "\n"


def test_lenses_json_lists_the_selected_lenses_and_which_are_always_on(run_dir):
    build(run_dir)
    doc = json.loads((run_dir.rdir / "packet" / "lenses.json").read_text())
    assert [l["name"] for l in doc["lenses"]] == ["always-lens", "paths-lens"]
    assert [l["always"] for l in doc["lenses"]] == [True, False]
    assert doc["lenses"][0]["prompt"] == "Always applies."


def test_prior_json_carries_the_confirmed_and_contested_verdicts(run_dir):
    build(run_dir)
    doc = json.loads((run_dir.rdir / "packet" / "prior.json").read_text())
    # A row the since run never graded has a null disposition: not reported, and
    # not the same as a disposition of "wrong".
    assert doc == {"verdicts": [
        {"id": "jkl", "title": "unset", "file": "src/a.py", "line": 1, "claim": "no owner",
         "verdict": "confirmed", "disposition": None},
        {"id": "abc", "title": "stale read", "file": "src/a.py", "line": 2, "claim": "reads old a",
         "verdict": "confirmed", "disposition": None},
        {"id": "def", "title": "maybe", "file": "src/b.py", "line": 1, "claim": "races",
         "verdict": "contested", "disposition": None}]}


def test_prior_json_selects_each_issues_own_verdict_and_carries_its_disposition(run_dir):
    """A bucket is one row but holds several issues: prior never takes the bucket's
    verdict for a member, and carries the human disposition recorded for the
    issue's own bucket (keyed as the report and prior.json key it)."""
    sdir = run_dir.home.runs / "demo" / "20240101-000000"
    (sdir / "verify" / "01" / "verdicts.jsonl").write_text(json.dumps({
        "key": "src/a.py:0", "verdict": "contested", "members": [
            {"id": "m1", "verdict": "confirmed", "title": "real one",
             "file": "src/a.py", "line": 3, "claim": "c1"},
            {"id": "m2", "verdict": "refuted", "title": "not real",
             "file": "src/a.py", "line": 4, "claim": "c2"}]}) + "\n")
    analysis.set_disposition(sdir, "src/a.py:0", "agree", "the fix lands")
    build(run_dir)
    assert json.loads((run_dir.rdir / "packet" / "prior.json").read_text()) == {"verdicts": [
        {"id": "m1", "title": "real one", "file": "src/a.py", "line": 3, "claim": "c1",
         "verdict": "confirmed", "disposition": "agree"}]}


def test_a_recorded_disposition_reaches_a_delta_prior(run_dir):
    """The F6 write side: a human disposes an issue in the since run's grading
    sheet, and a delta prior built from that run carries it. The disposition is
    per bucket, so the two members that share a bucket share their disposition."""
    sdir = run_dir.home.runs / "demo" / "20240101-000000"
    analysis.set_disposition(sdir, "src/a.py:0", "need-info", "waiting on the PVC decision")
    build(run_dir)
    doc = json.loads((run_dir.rdir / "packet" / "prior.json").read_text())
    by_id = {v["id"]: v["disposition"] for v in doc["verdicts"]}
    assert by_id == {"jkl": "need-info", "abc": "need-info", "def": None}


def test_a_calibration_grade_is_not_carried_as_a_disposition(run_dir):
    """A grade says whether the bot was right, a disposition what was decided: a
    graded but undisposed issue is not reported to a delta as decided."""
    sdir = run_dir.home.runs / "demo" / "20240101-000000"
    (sdir / "grades.jsonl").write_text(json.dumps({"issue": "src/a.py:0", "grade": "real"}) + "\n")
    build(run_dir)
    doc = json.loads((run_dir.rdir / "packet" / "prior.json").read_text())
    assert [v["disposition"] for v in doc["verdicts"]] == [None, None, None]


def test_prior_json_reads_only_the_newest_verify_attempt(run_dir):
    """An earlier attempt's verdicts were superseded by the newest one, which is
    the attempt the report reads: merging both would carry a finding twice under
    two outcomes."""
    sdir = run_dir.home.runs / "demo" / "20240101-000000"
    (sdir / "verify" / "02").mkdir()
    (sdir / "verify" / "02" / "verdicts.jsonl").write_text(json.dumps({
        "key": "src/a.py:0", "verdict": "confirmed", "members": [
            {"id": "newest", "verdict": "confirmed", "title": "new", "file": "src/a.py",
             "line": 2, "claim": "c"}]}) + "\n")
    build(run_dir)
    doc = json.loads((run_dir.rdir / "packet" / "prior.json").read_text())
    assert [v["id"] for v in doc["verdicts"]] == ["newest"]


def test_a_missing_since_run_fails_loudly(run_dir):
    doc = json.loads((run_dir.rdir / "run.json").read_text())
    doc["since_run"] = "20990101-000000"
    (run_dir.rdir / "run.json").write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    with pytest.raises(Fail, match="not found"):
        build(run_dir)


def test_a_since_run_that_is_not_done_fails_loudly(run_dir):
    pr_state.save_state(run_dir.home.runs / "demo" / "20240101-000000",
                        {"status": "gathering", "run_id": "20240101-000000"})
    with pytest.raises(Fail, match="not 'done'"):
        build(run_dir)


def test_a_since_run_of_another_project_fails_loudly(run_dir):
    sdir = run_dir.home.runs / "demo" / "20240101-000000"
    doc = json.loads((sdir / "run.json").read_text())
    doc["project"] = {"name": "other"}
    (sdir / "run.json").write_text(json.dumps(doc) + "\n")
    with pytest.raises(Fail, match="not demo"):
        build(run_dir)


def test_a_delta_without_a_since_run_fails(run_dir):
    doc = json.loads((run_dir.rdir / "run.json").read_text())
    del doc["since_run"]
    (run_dir.rdir / "run.json").write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    with pytest.raises(Fail, match="no since_run"):
        build(run_dir)


def test_prior_json_is_never_a_silent_empty_from_a_run_with_no_verdicts(run_dir):
    sdir = run_dir.home.runs / "demo" / "20240101-000000"
    (sdir / "verify" / "01" / "verdicts.jsonl").unlink()
    with pytest.raises(Fail, match="no prior to carry"):
        build(run_dir)


def test_every_page_of_the_comments_apis_reaches_threads_md(run_dir, monkeypatch):
    """A PR with many review comments fills more than the first page: the APIs are
    paged, and `--slurp`'s one-array-per-page answer is flattened, so threads.md
    is the whole thread and never its first 30 rows."""
    calls = []

    def paged(*args):
        calls.append(args)
        if "/pulls/" in args[1]:
            return [[{"path": "src/a.py", "line": 1, "user": {"login": "p1"}, "body": "page one"}],
                    [{"path": "src/a.py", "line": 2, "user": {"login": "p2"}, "body": "page two"}]]
        return [[{"user": {"login": "i1"}, "body": "issue page"}]]

    monkeypatch.setattr(pr_gather, "gh_json", paged)
    build(run_dir)
    assert calls and all("--paginate" in c and "--slurp" in c for c in calls)
    text = (run_dir.rdir / "packet" / "threads.md").read_text()
    assert "## src/a.py:1 p1\npage one" in text
    assert "## src/a.py:2 p2\npage two" in text
    assert "## (issue comment) i1\nissue page" in text


def test_a_dot_directory_context_range_is_not_mangled(run_dir):
    """`lstrip("./")` strips leading characters, not the prefix, so
    `.github/x.yml` became `github/x.yml` and was dropped as `not in the frozen
    tree`. Both call sites now strip the prefix (and only the prefix), so a
    range under a dot-directory is kept."""
    (run_dir.rdir / "tree" / ".github" / "workflows").mkdir(parents=True)
    (run_dir.rdir / "tree" / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    (run_dir.rdir / "tree" / ".github" / "CODEOWNERS").write_text("* @o\n")
    run_dir.rk.answers["gather-"] = (
        '```json\n{"context": [{"path": ".github/workflows/ci.yml", "start": 1, "end": 1},'
        ' {"path": "./.github/CODEOWNERS", "start": 1, "end": 1}], "file_groups": []}\n```\n')
    build(run_dir)
    files = (run_dir.rdir / "packet" / "files.txt").read_text().splitlines()
    assert "tree/.github/workflows/ci.yml:1-1" in files
    assert "tree/.github/CODEOWNERS:1-1" in files
    assert not [f for f in files if f.startswith("tree/github/")]


def test_a_failed_in_progress_save_releases_the_claim(run_dir, monkeypatch):
    """The save that mirrors the claim into state.json is inside the release path:
    a failed save gives the claim back and still reports the error, so the run is
    never left claimed by the invocation that failed."""
    def boom(rdir, state):
        raise Fail("save state: no space left on device")

    monkeypatch.setattr(pr_gather.pr_state, "save_state", boom)
    with pytest.raises(Fail, match="no space left"):
        pr_gather.claim_gather(run_dir.rdir, run_dir.run)
    assert not (run_dir.rdir / pr_gather.CLAIM).exists()


def test_threads_are_rendered_as_blocks_in_a_stable_order(run_dir):
    build(run_dir)
    assert (run_dir.rdir / "packet" / "threads.md").read_text() == (
        "## src/a.py:1 author\nwhy not\n\n## src/a.py:2 reviewer\nthis line changes behaviour\n\n"
        "## (issue comment) drive-by\nany update?\n")


def test_an_intent_is_the_pr_title_and_body(run_dir):
    build(run_dir)
    assert (run_dir.rdir / "packet" / "intent.md").read_text() == "# Add b\n\nWhy b exists.\n"


def test_the_diff_is_the_pinned_range(run_dir):
    build(run_dir)
    diff = (run_dir.rdir / "packet" / "diff.patch").read_text()
    assert "src/a.py" in diff and "src/b.py" in diff and diff.startswith("diff --git")


def test_gather_process_notes_are_recorded_as_friction(run_dir):
    build(run_dir)
    lines = [json.loads(l) for l in (run_dir.rdir / "friction.jsonl").read_text().splitlines()]
    assert [l for l in lines if l["kind"] == "self"] == [
        {"seat": "review-pr:gather", "kind": "self",
         "note": "the packet disagrees with src/a.py line numbers", "at": lines[0]["at"]}]


def test_estimate_splits_fundi_dollars_from_claude_volume(run_dir):
    manifest = build(run_dir)
    est = pr_gather.estimate(run_dir.rk, run_dir.rdir, manifest)
    # gather 0.5 + finder 0.3 x 2 lenses + panel-2 1 + panel-3 2 + verifiers
    # 2 x 0.2 (the cheapest fundi verify seat) x (2 lenses + 3 panel seats)
    # + the claim jobs: one always-on lens x 3 panel seats x 2 checks, priced by
    # the verifier seat that runs each (verify-2 0.2 x 2, verify-3 0.4 x 2).
    assert est["fundi"] == {"usd_upper_bound": 7.3, "label": "upper bound", "uncapped": [], "by_seat": {
        "review-pr:gather": 0.5, "review-pr:finder": 0.6, "review-pr:panel-2": 1.0,
        "review-pr:panel-3": 2.0, "review-pr:verify-2": 2.4, "review-pr:verify-3": 0.8}}
    # claude: panel-1 and verify-1, each reading the packet, plus the two claim
    # jobs that land on verify-1 (panel-1 is claude-kind).
    assert est["claude"] == {"input_tokens": 4 * manifest["total_tokens"], "seat_count": 2,
                             "seats": ["review-pr:panel-1", "review-pr:verify-1"]}
    assert est["blocking"] == []
    assert est["seats"]["review-pr:finder"]["context_window"] == 200_000

def test_the_claim_jobs_are_priced_per_always_on_lens_and_verifier_kind(run_dir):
    """N2: every panel seat answers every always-on lens, and each "nothing"
    answer is a claim checked by two fresh children, so the bound must cover
    n_always x panel x 2 more verifier spawns, priced by the verifier seat's kind
    (fundi dollars or claude packet volume)."""
    (run_dir.lens_generic / "paths-lens.md").write_text(
        "---\nname: paths-lens\ncategory: quality\napplies_to: [pr, delta]\n"
        "triggers: always\n---\n\nAlways applies too.\n")
    manifest = build(run_dir)
    est = pr_gather.estimate(run_dir.rk, run_dir.rdir, manifest)
    # Two always-on lenses: each fundi verifier seat gains 2 x 2 claim spawns on
    # top of the cluster term, and the claude verify seat re-reads for its own.
    assert est["fundi"]["by_seat"]["review-pr:verify-2"] == 2.0 + 4 * 0.2
    assert est["fundi"]["by_seat"]["review-pr:verify-3"] == 4 * 0.4
    assert est["claude"]["input_tokens"] == (2 + 4) * manifest["total_tokens"]


def test_under_the_threshold_the_run_is_confirmed(run_dir, fake_rafiki):
    summary = pr_gather.stage_gather(fake_rafiki, run_dir.home, SimpleNamespace(run=run_dir.run["run_id"]))
    assert summary["status"] == "confirmed"
    assert json.loads((run_dir.rdir / "state.json").read_text())["status"] == "confirmed"
    assert summary["estimate"]["blocking"] == []


def test_over_the_threshold_the_run_awaits_confirm_with_the_whole_packet(run_dir, fake_rafiki):
    # The threshold is per repo; a tiny one is the same thing as a big packet.
    (run_dir.rdir / "project" / "pr.json").write_text(json.dumps({**PR_JSON, "confirm_over_tokens": 1}) + "\n")
    summary = pr_gather.stage_gather(fake_rafiki, run_dir.home, SimpleNamespace(run=run_dir.run["run_id"]))
    assert summary["status"] == "awaiting-confirm"
    state = json.loads((run_dir.rdir / "state.json").read_text())
    assert state["status"] == "awaiting-confirm"
    assert state["gather"]["over_threshold"] is True and state["gather"]["confirm_over_tokens"] == 1
    assert state["gather"]["manifest"] == "packet/manifest.json"
    assert summary["total_tokens"] > 1
    # Nothing is ever cut: the packet is complete even though the run stopped.
    assert (run_dir.rdir / "packet" / "files.txt").read_text() == "\n".join(FILES_TXT) + "\n"


def test_a_seat_that_cannot_take_the_packet_blocks_and_awaits_confirm(run_dir, fake_rafiki):
    # The window now comes from the seat's own preset, so a seat tuned to a small
    # model is what makes the packet too big for it.
    fake_rafiki.presets["review-pr:panel-2"] = {**SEATS["review-pr:panel-2"], "context_window": 10}
    summary = pr_gather.stage_gather(fake_rafiki, run_dir.home, SimpleNamespace(run=run_dir.run["run_id"]))
    assert {"seat": "review-pr:panel-2", "reason": "packet exceeds context"} in summary["blocking"]
    assert summary["status"] == "awaiting-confirm"
    assert json.loads((run_dir.rdir / "state.json").read_text())["status"] == "awaiting-confirm"


def test_a_gh_failure_is_friction_and_the_packet_is_still_built(run_dir, monkeypatch):
    def boom(*args):
        raise Fail("gh api: 502")

    monkeypatch.setattr(pr_gather, "gh_json", boom)
    build(run_dir)
    assert (run_dir.rdir / "packet" / "threads.md").read_text() == ""
    kinds = [json.loads(l)["kind"] for l in (run_dir.rdir / "friction.jsonl").read_text().splitlines()]
    assert kinds.count("gh-error") == 2   # one per comments API
    assert (run_dir.rdir / "packet" / "files.txt").read_text() == "\n".join(FILES_TXT) + "\n"


def test_a_gather_parse_failure_is_friction_and_an_empty_context(run_dir):
    run_dir.rk.answers["gather-"] = "I looked at the change but wrote no json block."
    manifest = build(run_dir)
    assert json.loads((run_dir.rdir / "packet" / "context.json").read_text()) == {
        "context": [], "file_groups": []}
    # The packet still holds everything that does not depend on the gather seat.
    assert (run_dir.rdir / "packet" / "files.txt").read_text() == "\n".join(FILES_TXT[:7]) + "\n"
    assert [e["path"] for e in manifest["entries"]] == FILES_TXT[:7]
    entry = [json.loads(l) for l in (run_dir.rdir / "friction.jsonl").read_text().splitlines()]
    assert [e for e in entry if e["kind"] == "parse-error" and e["seat"] == "review-pr:gather"]


def test_a_gather_spawn_failure_is_friction_and_an_empty_context(run_dir):
    run_dir.rk.refuse_next = 1
    manifest = build(run_dir)
    assert json.loads((run_dir.rdir / "packet" / "context.json").read_text())["context"] == []
    assert manifest["total_tokens"] > 0
    kinds = [json.loads(l)["kind"] for l in (run_dir.rdir / "friction.jsonl").read_text().splitlines()]
    assert "spawn-error" in kinds


def test_a_range_the_tree_cannot_answer_is_left_out_of_the_packet(run_dir):
    # A deleted file cannot be pre-filled; its deletion is still in the diff.
    run_dir.rk.answers["gather-"] = ('```json\n{"context": [{"path": "src/gone.py", "start": 1, "end": 2},'
                                     ' {"path": "src/a.py", "start": 1, "end": 1}], "file_groups": []}\n```\n')
    build(run_dir)
    assert (run_dir.rdir / "packet" / "files.txt").read_text() == "\n".join(
        FILES_TXT[:7] + ["tree/src/a.py:1-1"]) + "\n"


def test_a_range_the_pre_fill_syntax_cannot_carry_becomes_the_whole_file(run_dir):
    run_dir.rk.answers["gather-"] = ('```json\n{"context": [{"path": "src/a.py", "start": 0, "end": 2}],'
                                     ' "file_groups": []}\n```\n')
    build(run_dir)
    assert (run_dir.rdir / "packet" / "files.txt").read_text() == "\n".join(
        FILES_TXT[:7] + ["tree/src/a.py"]) + "\n"


def test_the_sdk_catalog_is_read_when_the_sdk_has_one():
    # The table answers today; a future SDK catalog answers for a model the table
    # does not list, so a new seat model cannot silently pass the fit check.
    class C:
        @staticmethod
        def models():
            return {"some/new-model": {"context_window": 1234},
                    "other/new-model": SimpleNamespace(context_length=4321)}

    rk = SimpleNamespace(c=C())
    assert pr_gather.sdk_windows(rk) == {"some/new-model": 1234, "other/new-model": 4321}
    assert pr_gather.sdk_windows(SimpleNamespace(c=SimpleNamespace())) == {}


def test_a_preset_window_is_what_the_estimate_uses(run_dir):
    run_dir.rk.presets["review-pr:finder"] = {**SEATS["review-pr:finder"], "context_window": 50}
    manifest = build(run_dir)
    est = pr_gather.estimate(run_dir.rk, run_dir.rdir, manifest)
    assert manifest["seats"]["review-pr:finder"]["context_window"] == 50
    assert est["seats"]["review-pr:finder"]["context_window"] == 50
    # 50 tokens is far below the packet, so that seat alone blocks the run.
    assert {"seat": "review-pr:finder", "reason": "packet exceeds context"} in est["blocking"]


def test_the_sdk_catalog_is_used_when_no_preset_carries_a_window(run_dir):
    run_dir.rk.presets["review-pr:finder"] = {k: v for k, v in SEATS["review-pr:finder"].items()
                                              if k != "context_window"}
    run_dir.rk.models = lambda: {GLM: {"context_window": 2000}}
    manifest = build(run_dir)
    est = pr_gather.estimate(run_dir.rk, run_dir.rdir, manifest)
    assert manifest["seats"]["review-pr:finder"]["context_window"] is None
    assert est["seats"]["review-pr:finder"]["context_window"] == 2000
    assert [b for b in est["blocking"] if b["seat"] == "review-pr:finder"] == []


def test_an_unreported_window_is_null_friction_and_informational(run_dir, fake_rafiki):
    """Nothing reports this seat's window, so the estimate says `null`, records
    friction naming the seat and its model, and reports the seat as information.
    It must not assume 128K, and it must not block either: in production no preset
    carries a window and the SDK has no catalog, so blocking would stop every run
    before a single seat is spawned."""
    run_dir.rk.presets["review-pr:panel-3"] = {k: v for k, v in SEATS["review-pr:panel-3"].items()
                                               if k != "context_window"}
    summary = pr_gather.stage_gather(fake_rafiki, run_dir.home, SimpleNamespace(run=run_dir.run["run_id"]))
    est = summary["estimate"]
    assert est["seats"]["review-pr:panel-3"]["context_window"] is None
    assert {"seat": "review-pr:panel-3", "reason": "context window not reported"} in est["not_reported"]
    assert est["blocking"] == []      # an unreported window is not a claim about fit
    assert summary["status"] == "confirmed"
    notes = [json.loads(l) for l in (run_dir.rdir / "friction.jsonl").read_text().splitlines()]
    assert [n for n in notes if n["kind"] == "context-window"
            and n["seat"] == "review-pr:panel-3" and GPT in n["note"]]
    # Every other seat still reports its own window.
    assert est["seats"]["review-pr:panel-2"]["context_window"] == 200000


def test_the_estimate_is_usable_when_no_seat_reports_a_window(run_dir, fake_rafiki):
    """The production case: a real preset carries no window field and the SDK has
    no catalog verb, so every seat is unreported. The estimate is still priced and
    the run still proceeds, with every seat named as informational."""
    for name, spec in run_dir.rk.presets.items():
        run_dir.rk.presets[name] = {k: v for k, v in spec.items() if k != "context_window"}
    manifest = build(run_dir)
    est = pr_gather.estimate(run_dir.rk, run_dir.rdir, manifest)
    assert est["blocking"] == []
    assert sorted(b["seat"] for b in est["not_reported"]) == sorted(SEATS)
    assert all(est["seats"][n]["context_window"] is None for n in SEATS)
    assert est["fundi"]["usd_upper_bound"] == 7.3      # still priced
    assert est["claude"]["seat_count"] == 2
    summary = pr_gather.stage_gather(fake_rafiki, run_dir.home, SimpleNamespace(run=run_dir.run["run_id"]))
    assert summary["status"] == "confirmed" and summary["blocking"] == []
    assert summary["not_reported"] == est["not_reported"]


def test_the_estimate_says_plainly_that_fit_is_unchecked(run_dir, fake_rafiki, capsys):
    """The design's one hard limit cannot fire when no seat carries a context
    window, so the estimate and the gather summary say so in words, and why,
    rather than leaving the operator to infer it from a column of nulls. No
    window is invented in its place and the seat is still informational."""
    for name, spec in run_dir.rk.presets.items():
        run_dir.rk.presets[name] = {k: v for k, v in spec.items() if k != "context_window"}
    manifest = build(run_dir)
    est = pr_gather.estimate(run_dir.rk, run_dir.rdir, manifest)
    assert est["fit_checked"] is False
    assert "fit is NOT CHECKED" in est["fit_note"]
    assert "no seat carries a context window" in est["fit_note"]
    assert est["blocking"] == []
    summary = pr_gather.stage_gather(fake_rafiki, run_dir.home, SimpleNamespace(run=run_dir.run["run_id"]))
    assert summary["estimate"]["fit_checked"] is False
    assert "fit is NOT CHECKED" in capsys.readouterr().out


def test_the_estimate_says_fit_is_checked_when_every_seat_reports_a_window(run_dir):
    manifest = build(run_dir)
    est = pr_gather.estimate(run_dir.rk, run_dir.rdir, manifest)
    assert est["fit_checked"] is True
    assert "checked against every seat's context window" in est["fit_note"]


# --- fix round 1: the state guard, the config's gather seat, null caps, drops ---


def test_gather_refuses_a_run_it_may_not_rebuild(run_dir):
    """Packet bytes and status survive a refused gather: the packet is what the
    run's review and verify attempts were run against, so a finished run is not
    rebuilt in place."""
    build(run_dir)
    assert len(run_dir.rk.seat_spawns) == 1
    before = {p.name: p.read_bytes() for p in (run_dir.rdir / "packet").iterdir()}
    state = json.loads((run_dir.rdir / "state.json").read_text())
    args = SimpleNamespace(run=run_dir.run["run_id"])
    # Confirmed and awaiting-confirm are states the run left gather in; done and
    # aborted are finished. None of them may be gathered again.
    for status in ("confirmed", "awaiting-confirm", "reviewing", "verifying", "done", "aborted"):
        state["status"] = status
        pr_state.save_state(run_dir.rdir, state)
        with pytest.raises(Fail, match=status):
            pr_gather.stage_gather(run_dir.rk, run_dir.home, args)
        assert {p.name: p.read_bytes() for p in (run_dir.rdir / "packet").iterdir()} == before
        assert json.loads((run_dir.rdir / "state.json").read_text())["status"] == status
    assert len(run_dir.rk.seat_spawns) == 1


def test_the_gather_seat_comes_from_the_run_config(run_dir):
    (run_dir.rdir / "project" / "pr.json").write_text(
        json.dumps({**PR_JSON, "gather": "review-pr:gather-alt"}) + "\n")
    run_dir.rk.presets["review-pr:gather-alt"] = {"kind": "fundi", "model": GATHER_MODEL, "max_cost": 5}
    manifest = build(run_dir)
    spawn = run_dir.rk.seat_spawns[0]
    assert spawn["preset"] == "review-pr:gather-alt"
    assert spawn["labels"]["review/seat"] == "review-pr:gather-alt"
    assert sorted(manifest["seats"]) == sorted((set(SEATS) - {"review-pr:gather"})
                                               | {"review-pr:gather-alt"})
    est = pr_gather.estimate(run_dir.rk, run_dir.rdir, manifest)
    # The configured seat is priced as the gather seat, not dropped.
    assert est["fundi"]["by_seat"]["review-pr:gather-alt"] == 5.0
    assert "review-pr:gather" not in est["fundi"]["by_seat"]


def test_a_config_without_a_gather_seat_fails_naming_it(run_dir):
    (run_dir.rdir / "project" / "pr.json").write_text(
        json.dumps({k: v for k, v in PR_JSON.items() if k != "gather"}) + "\n")
    with pytest.raises(Fail, match="no gather seat"):
        build(run_dir)


def test_an_uncapped_fundi_seat_is_null_and_named_not_priced_at_zero(run_dir):
    run_dir.rk.presets["review-pr:panel-3"] = {"kind": "fundi", "model": GPT}   # no max_cost
    manifest = build(run_dir)
    assert manifest["seats"]["review-pr:panel-3"]["max_cost"] is None
    est = pr_gather.estimate(run_dir.rk, run_dir.rdir, manifest)
    assert est["fundi"]["usd_upper_bound"] is None
    assert est["fundi"]["uncapped"] == ["review-pr:panel-3"]
    assert "review-pr:panel-3" not in est["fundi"]["by_seat"]
    # The seats that do have a cap stay priced, so the partial sum is still read.
    assert est["fundi"]["by_seat"]["review-pr:gather"] == 0.5


def test_an_uncapped_verifier_seat_also_leaves_the_bound_null(run_dir):
    run_dir.rk.presets["review-pr:verify-3"] = {"kind": "fundi", "model": GPT}   # no max_cost
    manifest = build(run_dir)
    est = pr_gather.estimate(run_dir.rk, run_dir.rdir, manifest)
    assert est["fundi"]["uncapped"] == ["review-pr:verify-3"]
    assert est["fundi"]["usd_upper_bound"] is None
    # The cheapest capped fundi verify seat still prices the verifier term.
    assert est["fundi"]["by_seat"]["review-pr:verify-2"] == 2.4


def test_a_file_group_that_is_not_a_mapping_is_friction_and_no_groups(run_dir):
    run_dir.rk.answers["gather-"] = ('```json\n{"context": [], "file_groups": ["src/a.py",'
                                     ' {"name": "core", "paths": ["src/a.py"]}]}\n```\n')
    build(run_dir)
    # The seat has already been paid, so a malformed answer is not a Fail: the
    # answer is treated as having no groups, and friction names what arrived.
    assert json.loads((run_dir.rdir / "packet" / "context.json").read_text())["file_groups"] == []
    drops = [json.loads(l) for l in (run_dir.rdir / "friction.jsonl").read_text().splitlines()
             if json.loads(l)["kind"] == "packet-drop"]
    assert len(drops) == 1 and drops[0]["seat"] == "review-pr:gather"
    assert "file_groups[0] is not {name, paths}" in drops[0]["note"]
    assert "no file_groups" in drops[0]["note"]


def test_a_file_group_without_paths_is_friction_and_no_groups(run_dir):
    run_dir.rk.answers["gather-"] = '```json\n{"context": [], "file_groups": [{"name": "core"}]}\n```\n'
    build(run_dir)
    assert json.loads((run_dir.rdir / "packet" / "context.json").read_text())["file_groups"] == []
    notes = " ".join(json.loads(l)["note"] for l in
                     (run_dir.rdir / "friction.jsonl").read_text().splitlines()
                     if json.loads(l)["kind"] == "packet-drop")
    assert "file_groups[0] is not {name, paths}" in notes


def test_a_good_file_group_survives_into_the_packet(run_dir):
    build(run_dir)
    doc = json.loads((run_dir.rdir / "packet" / "context.json").read_text())
    assert doc["file_groups"] == [{"name": "core", "paths": ["src/a.py"]}]


def test_dropped_context_rows_are_friction_not_a_silent_drop(run_dir):
    run_dir.rk.answers["gather-"] = (
        '```json\n{"context": ["src/a.py", {"why": "no path"},'
        ' {"path": "src/nope.py", "start": 1, "end": 2}], "file_groups": []}\n```\n')
    build(run_dir)
    drops = [json.loads(l) for l in (run_dir.rdir / "friction.jsonl").read_text().splitlines()
             if json.loads(l)["kind"] == "packet-drop"]
    assert [d["seat"] for d in drops] == ["review-pr:gather"] * 3
    notes = " ".join(d["note"] for d in drops)
    assert "context[0] is not an object" in notes
    assert "context[1] has no path" in notes
    assert "not in the frozen tree" in notes
    # Nothing the tree can answer was cut, and nothing else was added.
    assert (run_dir.rdir / "packet" / "files.txt").read_text() == "\n".join(FILES_TXT[:7]) + "\n"


def test_a_packet_exclude_keeps_the_whole_file_out_but_not_its_range(run_dir):
    cfg = run_dir.rdir / "project" / "pr.json"
    cfg.write_text(json.dumps({**json.loads(cfg.read_text()), "packet_exclude": ["src/a*"]}) + "\n")
    build(run_dir)
    files = (run_dir.rdir / "packet" / "files.txt").read_text().splitlines()
    assert "tree/src/a.py" not in files
    assert "tree/src/b.py" in files
    # The gather seat's range into the excluded file is a deliberate ask, so it stays.
    assert "tree/src/a.py:1-2" in files
    assert "a = 2" in (run_dir.rdir / "packet" / "diff.patch").read_text()
    drops = [json.loads(l) for l in (run_dir.rdir / "friction.jsonl").read_text().splitlines()
             if json.loads(l)["kind"] == "packet-drop"]
    assert len(drops) == 1 and "src/a.py: matches pr.json packet_exclude 'src/a*'" in drops[0]["note"]


def test_a_packet_exclude_that_is_not_a_list_of_globs_fails(run_dir):
    cfg = run_dir.rdir / "project" / "pr.json"
    cfg.write_text(json.dumps({**json.loads(cfg.read_text()), "packet_exclude": "src/a.py"}) + "\n")
    with pytest.raises(Fail, match="packet_exclude must be a list of glob strings"):
        build(run_dir)


def test_the_run_snapshot_has_no_gold_answer_key(run_dir):
    """Every seat runs with cwd = the run directory, so the calibration answer
    key must not be in the snapshot; the lens `catches` validation reads it from
    the project instead, which is what lets always-lens's `known-bug` resolve."""
    assert not (run_dir.rdir / "project" / "gold").exists()
    assert (run_dir.rdir / "project" / "pr.json").exists()
    assert (run_dir.home.projects / "demo" / "gold" / "bugs.jsonl").exists()
    assert [l["name"] for l in build(run_dir)["lenses"]] == ["always-lens", "paths-lens"]


def test_a_comment_with_no_line_is_reported_and_not_written_as_line_zero(run_dir, monkeypatch):
    monkeypatch.setattr(pr_gather, "gh_json", lambda *args: [
        {"path": "src/a.py", "line": None, "original_line": None, "user": {"login": "author"},
         "body": "outdated"}])

    build(run_dir)
    # Both APIs returned the same lineless comment, and neither block claims a line.
    assert (run_dir.rdir / "packet" / "threads.md").read_text() == (
        "## src/a.py author\noutdated\n\n## src/a.py author\noutdated\n")
    notes = [json.loads(l)["note"] for l in (run_dir.rdir / "friction.jsonl").read_text().splitlines()
             if json.loads(l)["kind"] == "gh-error"]
    assert len(notes) == 2
    assert "reports no line" in notes[0]


def test_a_direct_packet_build_is_guarded_too(run_dir):
    state = json.loads((run_dir.rdir / "state.json").read_text())
    state["status"] = "done"
    pr_state.save_state(run_dir.rdir, state)
    with pytest.raises(Fail, match="done"):
        build(run_dir)
    assert not (run_dir.rdir / "packet").exists()


# --- fix round 2: glob paths, the in-progress claim, .review, null tokens ---


def test_a_glob_path_never_reaches_files_txt_or_the_gather_prefill(run_dir):
    """A path with a glob character is a glob to the daemon's pre-fill reader and
    a literal path to a seat that reads the list, so it must not be in the packet
    at all (the pre-fill would fail the child, or the two modes would read
    different files). It is recorded instead, and the gather seat still sees it in
    the change."""
    d = run_dir.rdir / "tree" / "app" / "[ref]"
    d.mkdir(parents=True)
    (d / "page.tsx").write_text("export default 1\n")
    run_doc = json.loads((run_dir.rdir / "run.json").read_text())
    run_doc["files_changed"] = sorted([*run_doc["files_changed"], "app/[ref]/page.tsx"])
    (run_dir.rdir / "run.json").write_text(json.dumps(run_doc, indent=2, sort_keys=True) + "\n")

    manifest = build(run_dir)
    files = (run_dir.rdir / "packet" / "files.txt").read_text().splitlines()
    assert files == FILES_TXT   # unchanged: the glob file never enters the packet
    assert [e["path"] for e in manifest["entries"]] == files
    (spawn,) = run_dir.rk.seat_spawns
    assert spawn["packet_files"] == ["packet/intent.md", "packet/diff.patch",
                                     "tree/src/a.py", "tree/src/b.py"]
    # The seat is told the change touched it; it can pick context, which is then
    # filtered the same way.
    assert "app/[ref]/page.tsx" in spawn["instruction"]
    notes = [json.loads(l)["note"] for l in
             (run_dir.rdir / "friction.jsonl").read_text().splitlines()
             if json.loads(l)["kind"] == "packet-drop"]
    assert any("app/[ref]/page.tsx" in n and "'*?[{' is a glob" in n for n in notes), notes


def test_a_glob_path_the_gather_seat_picked_is_excluded_and_recorded(run_dir):
    run_dir.rk.answers["gather-"] = (
        '```json\n{"context": [{"path": "src/a.py", "start": 1, "end": 1},'
        ' {"path": "src/[ref]/page.tsx", "start": 1, "end": 2}], "file_groups": []}\n```\n')
    build(run_dir)
    files = (run_dir.rdir / "packet" / "files.txt").read_text().splitlines()
    assert files == FILES_TXT[:7] + ["tree/src/a.py:1-1"]
    notes = " ".join(json.loads(l)["note"] for l in
                     (run_dir.rdir / "friction.jsonl").read_text().splitlines()
                     if json.loads(l)["kind"] == "packet-drop")
    assert "src/[ref]/page.tsx" in notes and "left out of the packet" in notes


def test_a_second_concurrent_gather_is_refused(run_dir):
    """Two gathers would both pass the status guard and both write packet/, so
    the claim file is exclusive and the loser is told who holds it."""
    lock = run_dir.rdir / pr_gather.CLAIM
    lock.write_text(json.dumps({"stage": "gather", "pid": 4242, "at": "2026-01-01T00:00:00+00:00"}) + "\n")
    with pytest.raises(Fail, match="pid 4242"):
        pr_gather.stage_gather(run_dir.rk, run_dir.home, SimpleNamespace(run=run_dir.run["run_id"]))
    assert run_dir.rk.seat_spawns == []
    assert not (run_dir.rdir / "packet").exists()


def test_the_claim_is_visible_while_a_gather_runs_and_gone_after(run_dir, fake_rafiki, monkeypatch):
    seen = {}
    real_estimate = pr_gather.estimate

    def spy(rk, rdir, manifest):
        seen["state"] = json.loads((rdir / "state.json").read_text())
        seen["lock"] = (rdir / pr_gather.CLAIM).exists()
        return real_estimate(rk, rdir, manifest)

    monkeypatch.setattr(pr_gather, "estimate", spy)
    summary = pr_gather.stage_gather(fake_rafiki, run_dir.home, SimpleNamespace(run=run_dir.run["run_id"]))
    assert seen["state"]["in_progress"]["stage"] == "gather"
    assert seen["state"]["in_progress"]["pid"] > 0
    assert seen["lock"] is True
    after = json.loads((run_dir.rdir / "state.json").read_text())
    assert "in_progress" not in after and after["status"] == summary["status"]
    assert not (run_dir.rdir / pr_gather.CLAIM).exists()
    # The claim is free again: on a still-gathering run, taking it a second time
    # succeeds (a finished run is refused by the status check, not by the claim).
    still = json.loads((run_dir.rdir / "state.json").read_text())
    still["status"] = "gathering"
    pr_state.save_state(run_dir.rdir, still)
    again = pr_gather.claim_gather(run_dir.rdir, run_dir.run)
    assert again.exists()
    pr_gather.release_gather(run_dir.rdir, again)
    assert not again.exists()


def test_the_claim_is_released_when_the_stage_fails(run_dir, fake_rafiki):
    state = json.loads((run_dir.rdir / "state.json").read_text())
    state["status"] = "done"
    pr_state.save_state(run_dir.rdir, state)
    with pytest.raises(Fail, match="done"):
        pr_gather.stage_gather(fake_rafiki, run_dir.home, SimpleNamespace(run=run_dir.run["run_id"]))
    assert not (run_dir.rdir / pr_gather.CLAIM).exists()
    assert "in_progress" not in json.loads((run_dir.rdir / "state.json").read_text())


def test_an_unreadable_claim_marker_still_refuses_and_says_so(run_dir):
    (run_dir.rdir / pr_gather.CLAIM).write_text("not json")
    with pytest.raises(Fail, match="marker unreadable"):
        pr_gather.stage_gather(run_dir.rk, run_dir.home, SimpleNamespace(run=run_dir.run["run_id"]))


def test_child_metrics_reports_null_tokens_when_nothing_reports_them():
    """Every PR child.json comes from here: a child whose provider reported no
    usage is not a child that used no tokens."""
    info = {"child_id": "c_1", "session_id": "s_1", "status": "idle", "model": "m",
            "labels": {}, "cost_usd": None}
    conv = {"turns": [
        {"role": "user", "content": [{"type": "text", "text": "hi"}],
         "input_tokens": None, "output_tokens": None},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}],
         "input_tokens": None, "output_tokens": None}]}
    m = child_metrics(info, conv, set())
    assert m["input_tokens"] is None and m["output_tokens"] is None
    assert m["model_turns"] == 0
    # A reported zero is a measured zero, and stays 0.
    conv["turns"][1].update({"input_tokens": 0, "output_tokens": 7})
    m = child_metrics(info, conv, set())
    assert (m["input_tokens"], m["output_tokens"], m["model_turns"]) == (0, 7, 1)
    # Reported turns are summed; unreported ones add nothing.
    conv["turns"].append({"role": "assistant", "content": [], "input_tokens": 5,
                          "output_tokens": None, "served_provider": None})
    m = child_metrics(info, conv, set())
    assert (m["input_tokens"], m["output_tokens"], m["model_turns"]) == (5, 7, 2)


def test_child_metrics_tolerates_string_content():
    """A claude-kind child's export can carry a turn's content as a plain
    string; that crashed the review stage's collect. Its tokens still count,
    and only real tool_use blocks are tallied."""
    info = {"child_id": "c_1", "session_id": "s_1", "status": "idle", "model": "m",
            "labels": {}, "cost_usd": None}
    conv = {"turns": [
        {"role": "user", "content": "review this", "input_tokens": 3, "output_tokens": None},
        {"role": "assistant", "content": ["stray", {"type": "tool_use", "id": "t1", "name": "Read",
                                                    "input": {"file_path": "x.go"}}],
         "input_tokens": 2, "output_tokens": 4}]}
    m = child_metrics(info, conv, set())
    assert (m["input_tokens"], m["output_tokens"], m["model_turns"]) == (5, 4, 2)
    assert m["tool_calls"] == {"Read": 1}


def test_dot_review_is_never_in_the_packet(run_dir):
    """Belt and braces behind pr_run_new: nothing under the target's .review/ may
    reach files.txt, whatever a run document happens to list."""
    d = run_dir.rdir / "tree" / ".review"
    d.mkdir()
    (d / "notes.md").write_text("ours\n")
    run_doc = json.loads((run_dir.rdir / "run.json").read_text())
    run_doc["files_changed"] = sorted([*run_doc["files_changed"], ".review/notes.md"])
    (run_dir.rdir / "run.json").write_text(json.dumps(run_doc, indent=2, sort_keys=True) + "\n")
    run_dir.rk.answers["gather-"] = ('```json\n{"context": [{"path": "src/a.py", "start": 1, "end": 2},'
                                     ' {"path": "README.md"}, {"path": ".review/notes.md"}],\n'
                                     ' "file_groups": []}\n```\n')
    manifest = build(run_dir)
    files = (run_dir.rdir / "packet" / "files.txt").read_text().splitlines()
    assert files == FILES_TXT
    assert [e["path"] for e in manifest["entries"]] == files
    (spawn,) = run_dir.rk.seat_spawns
    assert not [f for f in spawn["packet_files"] if ".review" in f]


# --- fix round 3: the unreported window, the diff's .review door, non-list
# answers, and the claim's order + unwind ---


def test_a_committed_dot_review_never_reaches_the_diff_patch(tmp_path, fake_rafiki):
    """pr_run_new drops .review/ from files_changed and from the frozen trees, but
    the patch is a second door into the packet: a committed .review/gold/*.jsonl
    would otherwise reach every seat through packet/diff.patch, and the lens
    triggers are matched against that diff too."""
    target = tmp_path / "repo"
    first, _ = two_commit_repo(target, remote="https://github.com/o/r.git")
    (target / ".review" / "gold").mkdir(parents=True)
    (target / ".review" / "gold" / "bugs.jsonl").write_text('{"bug": "known-bug", "grade": "real"}\n')
    git(target, "add", ".review")
    git(target, "commit", "-qm", "the target's own review config")
    head = git(target, "rev-parse", "HEAD").strip()
    home = make_home(tmp_path, target)
    lens_generic = write_lenses(tmp_path / "lenses")
    fake_rafiki.presets.update(SEATS)
    fake_rafiki.answers["gather-"] = GATHER_ANSWER
    rdir, _run = pr_runs.pr_run_new(home, "demo", "pr", base=first, head=head, daemon=DAEMON)

    manifest = pr_gather.build_packet(fake_rafiki, home, rdir, lens_generic=lens_generic)
    diff = (rdir / "packet" / "diff.patch").read_text()
    assert "src/a.py" in diff and "src/b.py" in diff       # the change itself is there
    assert "known-bug" not in diff and ".review" not in diff
    assert "packet/diff.patch" in [e["path"] for e in manifest["entries"]]
    notes = [json.loads(l)["note"] for l in (rdir / "friction.jsonl").read_text().splitlines()
             if json.loads(l)["kind"] == "review-config-drop"]
    assert any(".review/gold/bugs.jsonl" in n and "packet/diff.patch" in n for n in notes), notes


@pytest.mark.parametrize("value", ["5", "true", '"core"', '{"core": ["src/a.py"]}'])
def test_a_file_groups_that_is_not_a_list_is_friction_and_no_groups(run_dir, value):
    """Any non-list shape is friction plus no groups. A scalar used to raise
    TypeError and a mapping used to raise KeyError, after the gather seat was
    paid, which is the one thing a bad answer must never do."""
    run_dir.rk.answers["gather-"] = f'```json\n{{"context": [], "file_groups": {value}}}\n```\n'
    build(run_dir)
    assert json.loads((run_dir.rdir / "packet" / "context.json").read_text())["file_groups"] == []
    drops = [json.loads(l) for l in (run_dir.rdir / "friction.jsonl").read_text().splitlines()
             if json.loads(l)["kind"] == "packet-drop"]
    assert len(drops) == 1 and drops[0]["seat"] == "review-pr:gather"
    assert "file_groups is not a list" in drops[0]["note"]


def test_a_scalar_context_or_process_notes_never_raises(run_dir):
    run_dir.rk.answers["gather-"] = '```json\n{"context": 5, "file_groups": [], "process_notes": true}\n```\n'
    manifest = build(run_dir)
    assert json.loads((run_dir.rdir / "packet" / "context.json").read_text()) == {
        "context": [], "file_groups": []}
    notes = " ".join(json.loads(l)["note"] for l in
                     (run_dir.rdir / "friction.jsonl").read_text().splitlines()
                     if json.loads(l)["kind"] == "packet-drop")
    assert "context is not a list" in notes and "process_notes is not a list" in notes
    assert manifest["total_tokens"] > 0


def test_the_status_is_checked_under_the_claim(run_dir, monkeypatch):
    """A status read before the claim can be stale by the time the claim is ours
    (another invocation can finish and write `confirmed` in the gap), so the read
    happens with the claim already on disk."""
    seen = {}
    real_load = pr_state.load_state

    def spy(rdir):
        seen.setdefault("lock_held_at_read", (rdir / pr_gather.CLAIM).exists())
        return real_load(rdir)

    monkeypatch.setattr(pr_gather.pr_state, "load_state", spy)
    claim = pr_gather.claim_gather(run_dir.rdir, run_dir.run)
    assert seen["lock_held_at_read"] is True
    pr_gather.release_gather(run_dir.rdir, claim)
    assert not claim.exists()


def test_a_refused_claim_is_given_back(run_dir):
    """The claim is taken first, so the run it turns out not to deserve must not
    stay claimed by the invocation that refused it."""
    state = json.loads((run_dir.rdir / "state.json").read_text())
    state["status"] = "confirmed"
    pr_state.save_state(run_dir.rdir, state)
    with pytest.raises(Fail, match="confirmed"):
        pr_gather.claim_gather(run_dir.rdir, run_dir.run)
    assert not (run_dir.rdir / pr_gather.CLAIM).exists()
    after = json.loads((run_dir.rdir / "state.json").read_text())
    assert after["status"] == "confirmed" and "in_progress" not in after


def test_a_lost_daemon_still_records_the_error_and_releases_the_claim(run_dir, monkeypatch):
    """A lost daemon is the likeliest cause of a mid-stage failure, and the unwind
    must not strand the run when it happens: the run keeps a record of what aborted
    the stage, and the claim is released even though the daemon is unreachable."""
    def gone(child_id):
        raise Fail("daemon unreachable")

    monkeypatch.setattr(run_dir.rk, "child_info", gone)
    with pytest.raises(Fail, match="daemon unreachable"):
        pr_gather.stage_gather(run_dir.rk, run_dir.home, SimpleNamespace(run=run_dir.run["run_id"]))
    assert not (run_dir.rdir / pr_gather.CLAIM).exists()
    state = json.loads((run_dir.rdir / "state.json").read_text())
    assert state["status"] == "gathering" and "in_progress" not in state
    errs = [json.loads(l) for l in (run_dir.rdir / "friction.jsonl").read_text().splitlines()
            if json.loads(l)["kind"] == "stage-error"]
    assert len(errs) == 1 and "daemon unreachable" in errs[0]["note"]


def test_a_release_that_cannot_unlink_does_not_replace_the_stage_error(run_dir, monkeypatch):
    """The unwind is best effort: a claim that cannot be removed is recorded, and
    the error that aborted the stage is still the one the caller sees."""
    def gone(child_id):
        raise Fail("daemon unreachable")

    def no_unlink(path):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(run_dir.rk, "child_info", gone)
    monkeypatch.setattr(pr_gather.os, "unlink", no_unlink)
    with pytest.raises(Fail, match="daemon unreachable"):
        pr_gather.stage_gather(run_dir.rk, run_dir.home, SimpleNamespace(run=run_dir.run["run_id"]))
    assert (run_dir.rdir / pr_gather.CLAIM).exists()   # reported, never raised
    kinds = [json.loads(l)["kind"] for l in (run_dir.rdir / "friction.jsonl").read_text().splitlines()]
    assert kinds.count("stage-error") == 1 and kinds.count("claim-release-failed") == 1


def test_the_lean_estimate_prices_triggered_finders_and_no_claim_jobs(run_dir):
    # The defaults: one finder cell per triggered lens (always-lens is the
    # panel's), and a "nothing" answer is reported rather than verified, so no
    # verifier seat carries claim spawns.
    lean = {k: v for k, v in PR_JSON.items() if k not in ("finders", "split_groups", "verify_claims")}
    (run_dir.rdir / "project" / "pr.json").write_text(json.dumps(lean) + "\n")
    manifest = build(run_dir)
    assert sorted((l["name"], l["always"]) for l in manifest["lenses"]) == [
        ("always-lens", True), ("paths-lens", False)]
    est = pr_gather.estimate(run_dir.rk, run_dir.rdir, manifest)
    assert est["fundi"]["by_seat"]["review-pr:finder"] == 0.3
    assert "review-pr:verify-3" not in est["fundi"]["by_seat"]
    assert est["claude"]["input_tokens"] == 2 * manifest["total_tokens"]


def test_the_gather_seat_is_told_the_project_s_tools(run_dir):
    (run_dir.rdir / "project" / "tools.md").write_text("Use mcp__codescan__code_search on tree code.demo.\n")
    build(run_dir)
    gathers = [s["instruction"] for s in run_dir.rk.seat_spawns if s["preset"] == "review-pr:gather"]
    assert gathers and all("Use mcp__codescan__code_search on tree code.demo." in t for t in gathers)
