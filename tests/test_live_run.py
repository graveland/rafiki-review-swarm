# tests: "freeze": false reviews the checkout itself. run new refuses a checkout
# that is dirty or not at the requested ref, records no frozen tree, and every
# later stage refuses once the checkout has moved.
import json
import subprocess

import pytest

from review_swarm import runs, stages
from review_swarm.common import Fail

from .test_audit import FILES, git_repo


def commit(repo, msg):
    for cmd in (["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                               "commit", "-qm", msg]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True)


@pytest.fixture
def live(tmp_home, tmp_path):
    target = git_repo(tmp_path / "repo", FILES)
    pdir = tmp_home.projects / "sc"
    pdir.mkdir(parents=True)
    (pdir / "project.json").write_text(json.dumps({
        "name": "sc", "target": str(target), "include": ["**"], "must_cover": ["pkg/**"], "freeze": False}))
    (pdir / "concerns.json").write_text('{"concerns": [{"name": "c", "category": "defect", "prompt": "p"}]}')
    return target


def test_a_live_run_reviews_the_checkout_and_freezes_nothing(tmp_home, live):
    rdir, run = tmp_home.load_run(runs.run_new(tmp_home, "sc", None, {}))
    assert run["tree"] is None
    assert not (rdir / "tree").exists()
    assert stages.run_cwd(run) == live.resolve()


def test_a_live_run_refuses_a_dirty_checkout_and_one_not_at_the_ref(tmp_home, live):
    head = subprocess.run(["git", "-C", str(live), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    (live / "pkg" / "cli" / "fwd.go").write_text("package cli // edited\n")
    with pytest.raises(Fail, match="uncommitted edits"):
        runs.run_new(tmp_home, "sc", None, {})
    commit(live, "move on")
    with pytest.raises(Fail, match="check it out first"):
        runs.run_new(tmp_home, "sc", head, {})
    assert runs.run_new(tmp_home, "sc", "HEAD", {})


def test_every_stage_refuses_once_the_checkout_has_moved(tmp_home, live):
    _, run = tmp_home.load_run(runs.run_new(tmp_home, "sc", None, {}))
    (live / "pkg" / "kube" / "kube.go").write_text("package kube // moved\n")
    commit(live, "moved")
    with pytest.raises(Fail, match="target moved"):
        stages.run_cwd(run)


def test_a_seam_without_paths_is_size_checked_on_a_live_run_too(tmp_home, live, monkeypatch):
    _, run = tmp_home.load_run(runs.run_new(tmp_home, "sc", None, {}))
    monkeypatch.setattr(stages, "SEAM_UNION_MAX_BYTES", 10)
    doc = {"components": [{"component": "cli", "paths": ["pkg/cli/*"]}, {"component": "kube", "paths": ["pkg/kube/*"]}],
           "seams": [{"seam": "w", "components": ["cli", "kube"], "what": "w"}]}
    assert any(p.startswith("seam w: its components hold") for p in stages.validate_map(doc, run))
