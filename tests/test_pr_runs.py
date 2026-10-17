# tests: PR runs - mode dispatch, the pinned shas, the two frozen trees
"""The contract these tests pin down: a run freezes exactly the commits it
pinned, the base comes from `since_run` in delta mode, an unknown mode is
refused before anything is written, and the run records what gather needs to
build the packet. The repository under review is a real two-commit git repo;
nothing here touches a daemon or the network."""
import json
import subprocess

import pytest

from review_swarm import pr_runs
from review_swarm.common import Fail
from review_swarm.runs import Home


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def two_commit_repo(root, remote=None):
    """A src/a.py edit plus a new src/b.py in the second commit, so the diff and
    the changed-file list both have something to say."""
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


def make_home(tmp_path, target, name="demo", pr_json=None):
    home = Home(tmp_path / "home")
    pdir = home.projects / name
    (pdir / "gold").mkdir(parents=True)
    (pdir / "project.json").write_text(json.dumps({"name": name, "target": str(target)}) + "\n")
    (pdir / "gold" / "bugs.jsonl").write_text(json.dumps({"bug": "known-bug", "grade": "real"}) + "\n")
    if pr_json is not None:
        (pdir / "pr.json").write_text(json.dumps(pr_json) + "\n")
    return home


DAEMON = {"target": "live", "version": "test"}


def test_explicit_base_head_pins_both_shas_and_freezes_both_trees(tmp_path):
    target = tmp_path / "repo"
    first, head = two_commit_repo(target, remote="https://github.com/o/r.git")
    home = make_home(tmp_path, target)
    rdir, run = pr_runs.pr_run_new(home, "demo", "pr", base=first[:12], head=head, daemon=DAEMON)

    assert (run["base"], run["head"]) == (first, head)
    assert run["repo"] == "o/r" and run["kind"] == "pr" and run["mode"] == "pr"
    assert run["files_changed"] == ["src/a.py", "src/b.py"]
    # The frozen trees are the only thing a reviewer reads, so their bytes must
    # be the pinned commits, not whatever the checkout says now.
    assert (rdir / "tree" / "src" / "a.py").read_text() == "a = 2\n"
    assert (rdir / "tree" / "src" / "b.py").read_text() == "b = 1\n"
    assert (rdir / "base_tree" / "src" / "a.py").read_text() == "a = 1\n"
    assert not (rdir / "base_tree" / "src" / "b.py").exists()
    assert (rdir / "project" / "project.json").exists()
    # The snapshot is readable by every seat (cwd = the run directory), so the
    # calibration answer key stays in the project.
    assert not (rdir / "project" / "gold").exists()
    assert (home.projects / "demo" / "gold" / "bugs.jsonl").exists()
    # Sorted keys, so the run document's bytes are reproducible.
    assert (rdir / "run.json").read_text() == json.dumps(run, indent=2, sort_keys=True) + "\n"


def test_run_state_starts_as_gathering(tmp_path):
    target = tmp_path / "repo"
    first, head = two_commit_repo(target, remote="https://github.com/o/r.git")
    home = make_home(tmp_path, target)
    rdir, run = pr_runs.pr_run_new(home, "demo", "pr", base=first, head=head, daemon=DAEMON)
    state = json.loads((rdir / "state.json").read_text())
    assert state["status"] == "gathering" and state["run_id"] == run["run_id"]
    assert list(state) == sorted(state)


def test_a_commit_the_target_does_not_have_fails_naming_the_sha(tmp_path):
    # A local, non-existent remote path: the fetch attempt fails immediately
    # instead of reaching github, which is what makes this test hermetic.
    target = tmp_path / "repo"
    two_commit_repo(target)
    missing = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    with pytest.raises(Fail, match=missing):
        pr_runs.resolve_commit(target, str(tmp_path / "nope"), missing)


def test_resolve_commit_takes_a_local_ref_without_fetching(tmp_path):
    target = tmp_path / "repo"
    first, _ = two_commit_repo(target)
    assert pr_runs.resolve_commit(target, str(tmp_path / "nope"), "HEAD~1") == first


def test_unknown_mode_fails_before_anything_is_written(tmp_path):
    target = tmp_path / "repo"
    two_commit_repo(target)
    home = make_home(tmp_path, target)
    with pytest.raises(Fail, match="unknown mode"):
        pr_runs.pr_run_new(home, "demo", "review", daemon=DAEMON)
    assert not (home.runs / "demo").exists()


def test_pr_mode_takes_the_intent_and_the_shas_from_the_pr(tmp_path, monkeypatch):
    target = tmp_path / "repo"
    first, head = two_commit_repo(target, remote="git@github.com:o/r.git")
    home = make_home(tmp_path, target)
    seen = {}

    def fake_view(pr, repo):
        seen.update({"pr": pr, "repo": repo})
        return {"baseRefOid": first, "headRefOid": head, "title": "Add b", "body": "Why b exists."}

    monkeypatch.setattr(pr_runs, "gh_pr_view", fake_view)
    _rdir, run = pr_runs.pr_run_new(home, "demo", "pr", pr=7, daemon=DAEMON)
    assert seen == {"pr": 7, "repo": "o/r"}
    assert (run["base"], run["head"]) == (first, head)
    assert (run["title"], run["body"], run["pr"]) == ("Add b", "Why b exists.", 7)


def test_delta_takes_its_base_from_the_since_run(tmp_path, monkeypatch):
    target = tmp_path / "repo"
    first, head = two_commit_repo(target, remote="https://github.com/o/r.git")
    home = make_home(tmp_path, target)
    # The since run only has to record the head it reviewed; that is where the
    # delta's base comes from.
    sdir = home.runs / "demo" / "20240101-000000"
    sdir.mkdir(parents=True)
    (sdir / "run.json").write_text(json.dumps({"run_id": "20240101-000000", "head": first}) + "\n")
    monkeypatch.setattr(pr_runs, "gh_pr_view",
                        lambda pr, repo: {"baseRefOid": first, "headRefOid": head,
                                          "title": "Add b", "body": "Why b exists."})
    rdir, run = pr_runs.pr_run_new(home, "demo", "delta", pr=7, since_run="20240101-000000",
                                   daemon=DAEMON)
    assert (run["base"], run["head"]) == (first, head)
    assert run["since_run"] == "20240101-000000"
    assert (rdir / "base_tree" / "src" / "a.py").read_text() == "a = 1\n"


def test_delta_needs_a_since_run(tmp_path):
    target = tmp_path / "repo"
    two_commit_repo(target, remote="https://github.com/o/r.git")
    home = make_home(tmp_path, target)
    with pytest.raises(Fail, match="since_run"):
        pr_runs.pr_run_new(home, "demo", "delta", pr=7, daemon=DAEMON)


def test_design_pins_the_trunk_and_has_no_base(tmp_path):
    target = tmp_path / "repo"
    _, head = two_commit_repo(target)
    home = make_home(tmp_path, target)
    doc = tmp_path / "design.md"
    doc.write_text("# Design\n\nThe plan.\n")
    rdir, run = pr_runs.pr_run_new(home, "demo", "design", doc=str(doc), daemon=DAEMON)
    assert run["head"] == head and run["base"] is None
    assert "base_tree" not in run and "repo" not in run
    assert run["doc"] == str(doc)
    assert run["files_changed"] == []
    assert not (rdir / "base_tree").exists()


def test_design_needs_a_doc_that_exists(tmp_path):
    target = tmp_path / "repo"
    two_commit_repo(target)
    home = make_home(tmp_path, target)
    with pytest.raises(Fail, match="doc"):
        pr_runs.pr_run_new(home, "demo", "design", daemon=DAEMON)
    with pytest.raises(Fail, match="not found"):
        pr_runs.pr_run_new(home, "demo", "design", doc=str(tmp_path / "nope.md"), daemon=DAEMON)


def test_owner_repo_parses_the_named_shapes(tmp_path):
    assert pr_runs.owner_repo("git@github.com:o/r.git") == "o/r"
    assert pr_runs.owner_repo("https://github.com/o/r") == "o/r"
    assert pr_runs.owner_repo("https://github.com/o/r.git") == "o/r"
    assert pr_runs.owner_repo("ssh://git@git.example.com/acme/widgets.git") == "acme/widgets"
    with pytest.raises(Fail, match="owner/repo"):
        pr_runs.owner_repo("git@github.com:o/sub/r.git")


def test_a_target_that_commits_dot_review_keeps_it_out_of_the_run(tmp_path):
    """<target>/.review/ is this tool's own config location, committed into some
    targets, and a gold/ under it would put the calibration answer key in a tree
    every seat can read. It is not part of the change under review."""
    target = tmp_path / "repo"
    first, _ = two_commit_repo(target, remote="https://github.com/o/r.git")
    (target / ".review" / "gold").mkdir(parents=True)
    (target / ".review" / "gold" / "bugs.jsonl").write_text('{"bug": "known-bug", "grade": "real"}\n')
    (target / ".review" / "notes.md").write_text("ours\n")
    git(target, "add", ".review")
    git(target, "commit", "-qm", "config")
    head = git(target, "rev-parse", "HEAD").strip()
    home = make_home(tmp_path, target)

    rdir, run = pr_runs.pr_run_new(home, "demo", "pr", base=first, head=head, daemon=DAEMON)
    assert run["files_changed"] == ["src/a.py", "src/b.py"]   # .review is not in this change
    assert not (rdir / "tree" / ".review").exists()
    assert not (rdir / "base_tree" / ".review").exists()
    dropped = [json.loads(l) for l in (rdir / "friction.jsonl").read_text().splitlines()
               if json.loads(l)["kind"] == "review-config-drop"]
    assert any("tree/.review" in d["note"] for d in dropped)
    assert sorted({d["note"].split(" is under")[0] for d in dropped if " is under" in d["note"]}) == [
        ".review/gold/bugs.jsonl", ".review/notes.md"]
    # The repo's own copy is untouched, and lens catches still validate against it.
    assert (target / ".review" / "gold" / "bugs.jsonl").exists()


def test_dot_review_is_not_a_changed_file_even_in_delta_mode(tmp_path, monkeypatch):
    target = tmp_path / "repo"
    first, _ = two_commit_repo(target, remote="https://github.com/o/r.git")
    (target / ".review").mkdir()
    (target / ".review" / "notes.md").write_text("ours\n")
    git(target, "add", ".review")
    git(target, "commit", "-qm", "config and a fix")
    head = git(target, "rev-parse", "HEAD").strip()
    home = make_home(tmp_path, target)
    sdir = home.runs / "demo" / "20240101-000000"
    sdir.mkdir(parents=True)
    (sdir / "run.json").write_text(json.dumps({"run_id": "20240101-000000", "head": first}) + "\n")
    monkeypatch.setattr(pr_runs, "gh_pr_view",
                        lambda pr, repo: {"baseRefOid": first, "headRefOid": head,
                                          "title": "T", "body": "B"})
    rdir, run = pr_runs.pr_run_new(home, "demo", "delta", pr=7, since_run="20240101-000000",
                                   daemon=DAEMON)
    assert ".review/notes.md" not in run["files_changed"]
    assert not (rdir / "tree" / ".review").exists()
