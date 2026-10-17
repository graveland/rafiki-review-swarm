# tests: the review home and data dir split - where config, runs and lens builds
# live, and the one CLI path that proves a build never lands in the home.
import json
import subprocess
from types import SimpleNamespace

import pytest

from review_swarm import cli, runs
from review_swarm.common import Fail
from review_swarm.runs import Home, resolve_data, resolve_home


# --- resolve_home ----------------------------------------------------------------

def test_resolve_home_explicit_beats_env_beats_cwd(tmp_path):
    cwd = tmp_path / "cwd"
    (cwd / "projects").mkdir(parents=True)
    assert resolve_home("explicit", env={"REVIEW_SWARM_HOME": "env"}, cwd=cwd) == "explicit"
    assert resolve_home(None, env={"REVIEW_SWARM_HOME": "env"}, cwd=cwd) == "env"
    assert resolve_home(None, env={}, cwd=cwd) == str(cwd)


def test_resolve_home_uses_cwd_with_projects_and_refuses_without(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    with pytest.raises(Fail, match="REVIEW_SWARM_HOME"):
        resolve_home(None, env={}, cwd=cwd)
    (cwd / "projects").mkdir()
    assert resolve_home(None, env={}, cwd=cwd) == str(cwd)


def test_resolve_home_empty_env_falls_through_to_cwd(tmp_path):
    cwd = tmp_path / "cwd"
    (cwd / "projects").mkdir(parents=True)
    assert resolve_home(None, env={"REVIEW_SWARM_HOME": ""}, cwd=cwd) == str(cwd)


# --- resolve_data ----------------------------------------------------------------

def test_resolve_data_explicit_beats_env_and_env_alone_is_used(tmp_path):
    explicit, env = tmp_path / "explicit", tmp_path / "env"
    assert resolve_data(str(explicit), env={"REVIEW_SWARM_DATA": str(env)}) == str(explicit)
    assert resolve_data(None, env={"REVIEW_SWARM_DATA": str(env)}) == str(env)


def test_resolve_data_rejects_a_relative_path():
    with pytest.raises(Fail, match="absolute path"):
        resolve_data("relative/data", env={})
    with pytest.raises(Fail, match="absolute path"):
        resolve_data(None, env={"REVIEW_SWARM_DATA": "relative/data"})


def test_resolve_data_has_no_default():
    with pytest.raises(Fail, match="REVIEW_SWARM_DATA"):
        resolve_data(None, env={})


def test_resolve_data_empty_env_is_not_a_data_dir():
    with pytest.raises(Fail, match="REVIEW_SWARM_DATA"):
        resolve_data(None, env={"REVIEW_SWARM_DATA": ""})


# --- Home -------------------------------------------------------------------------

def test_home_keeps_data_and_config_apart(tmp_path):
    home = Home(tmp_path / "h", tmp_path / "d")
    assert home.runs == (tmp_path / "d").resolve() / "runs"
    assert home.projects == (tmp_path / "h").resolve() / "projects"
    assert Home(tmp_path).runs == tmp_path.resolve() / "runs"


def test_lens_dir_is_none_until_the_home_has_one(tmp_path):
    home = Home(tmp_path)
    assert home.lens_dir is None
    (tmp_path / "lenses").mkdir()
    assert home.lens_dir == (tmp_path / "lenses").resolve()


# --- the CLI keeps lens builds out of the home ------------------------------------

def test_lenses_build_writes_to_the_data_dir_not_the_home(tmp_path):
    home_dir, data_dir = tmp_path / "h", tmp_path / "d"
    pdir = home_dir / "projects" / "demo"
    pdir.mkdir(parents=True)
    target = tmp_path / "target"
    target.mkdir()
    (pdir / "project.json").write_text(json.dumps({"name": "demo", "target": str(target)}))
    (pdir / "pr.json").write_text(json.dumps({}))
    write_lens(home_dir / "lenses", "house", applies_to="pr")
    assert cli.swarm_main(["--home", str(home_dir), "--data", str(data_dir),
                           "lenses", "build", "demo"]) == 0
    manifest = json.loads((data_dir / "builds" / "demo" / "lenses.manifest.json").read_text())
    assert "house" in [lens["name"] for lens in manifest["lenses"]]
    assert not (home_dir / "projects" / "demo" / "build").exists()


def test_lenses_build_reads_an_audit_only_project(tmp_path):
    home_dir, data_dir = tmp_path / "h", tmp_path / "d"
    pdir = home_dir / "projects" / "demo"
    pdir.mkdir(parents=True)
    target = tmp_path / "target"
    target.mkdir()
    (pdir / "project.json").write_text(json.dumps({"name": "demo", "target": str(target)}))
    (pdir / "audit.json").write_text(json.dumps({"lens_dir": str(tmp_path / "project-lenses")}))
    write_lens(tmp_path / "project-lenses", "audit-rule", applies_to="audit")
    assert cli.swarm_main(["--home", str(home_dir), "--data", str(data_dir),
                           "lenses", "build", "demo"]) == 0
    manifest = json.loads((data_dir / "builds" / "demo" / "lenses.manifest.json").read_text())
    assert "audit-rule" in [lens["name"] for lens in manifest["lenses"]]


# --- the CLI reports a missing home without a traceback --------------------------

def test_swarm_main_reports_a_missing_home_without_a_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # no projects/ here
    monkeypatch.delenv("REVIEW_SWARM_HOME", raising=False)
    monkeypatch.delenv("REVIEW_SWARM_DATA", raising=False)
    assert cli.swarm_main(["presets", "sync"]) == 1
    assert capsys.readouterr().err.startswith("review-swarm: ")


# --- run_new and launch pass the home's lenses and the data dir ------------------

def write_lens(d, name, applies_to="audit"):
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(f"---\nname: {name}\ncategory: defect\napplies_to: [{applies_to}]\n---\n\nHunt {name}.\n")
    return p


def git_repo(path, files):
    path.mkdir()
    for rel, text in files.items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    for cmd in (["init", "-q"], ["add", "."],
                ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                 "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(path), *cmd], check=True)
    return path


def test_run_new_compiles_the_home_lens_layer(tmp_path):
    target = git_repo(tmp_path / "repo", {"src/a.py": "x = 1\n"})
    home = Home(tmp_path / "h", tmp_path / "d")
    pdir = home.projects / "p"
    pdir.mkdir(parents=True)
    (pdir / "project.json").write_text(json.dumps({
        "name": "p", "target": str(target), "include": ["**"], "must_cover": ["src/**"]}))
    project_lenses = tmp_path / "project-lenses"
    project_lenses.mkdir()
    (pdir / "audit.json").write_text(json.dumps({"lens_dir": str(project_lenses)}))
    write_lens(home.path / "lenses", "house", applies_to="audit")
    rdir, _ = home.load_run(runs.run_new(home, "p", None, {}))
    assert "house" in [c["name"] for c in runs.run_concerns(rdir)]


class _Recorder:
    def __init__(self):
        self.kw = None

    def spawn_script(self, name, **kw):
        self.kw = kw
        return "c_1"


class _FakeTarget:
    kind = "live"
    profile = None

    def __init__(self, recorder):
        self.recorder = recorder

    def client(self):
        return self.recorder


def test_launch_passes_the_data_dir_and_no_home(tmp_path):
    home = Home(tmp_path / "h", tmp_path / "d")
    rdir = home.runs / "p" / "run-1"
    rdir.mkdir(parents=True)
    (rdir / "run.json").write_text(json.dumps({"run_id": "run-1", "project": {"name": "p"}}))
    recorder = _Recorder()
    args = SimpleNamespace(stage_args=["--", "gather", "run-1"], repo="review-swarm",
                           max_cost=None, max_children=None)
    cli.launch(_FakeTarget(recorder), home, args)
    kw = recorder.kw
    assert kw["args"] == ["--data", str(home.data), "gather", "run-1"]
    assert "--home" not in kw["args"]
    assert kw["cwd"] == home.data
