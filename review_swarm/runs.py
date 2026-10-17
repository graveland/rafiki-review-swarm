# review-swarm: runs - pinned targets, frozen trees, per-attempt stage dirs under --home
import datetime
import fnmatch
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import lenses as lenses_mod
from .common import LENSES, Fail, git, now_iso


class Home:
    """The review home: projects/ and lenses/ (config, read-only to stages), and under the data dir, runs/<project>/<run-id>/ (frozen tree + every stage's attempts). Nothing a run needs lives next to the code."""

    def __init__(self, path, data=None):
        self.path = Path(path).expanduser().resolve()
        self.projects = self.path / "projects"
        self.data = Path(data).expanduser().resolve() if data else self.path
        self.runs = self.data / "runs"

    @property
    def lens_dir(self):
        d = self.path / "lenses"
        return d if d.is_dir() else None

    def load_run(self, run_id):
        matches = sorted(self.runs.glob(f"*/{run_id}"))
        if len(matches) != 1:
            raise Fail(f"run {run_id!r}: {'not found' if not matches else 'ambiguous'} under {self.runs}")
        rdir = matches[0]
        return rdir, json.loads((rdir / "run.json").read_text())

    def project_dir(self, name, target=None):
        """Per-project review content: <target>/.review/ when the target carries
        it, else projects/<name>/ (for targets we don't write into)."""
        if target is not None and (Path(target) / ".review" / "project.json").exists():
            return Path(target) / ".review"
        d = self.projects / name
        if not (d / "project.json").exists():
            raise Fail(f"no project config: neither {target}/.review/project.json nor {d}/project.json")
        return d


def resolve_home(explicit, env=None, cwd=None):
    """--home, then REVIEW_SWARM_HOME, then the current directory when it holds projects/."""
    env = os.environ if env is None else env
    if explicit:
        return explicit
    if env.get("REVIEW_SWARM_HOME"):
        return env["REVIEW_SWARM_HOME"]
    here = Path(cwd) if cwd is not None else Path.cwd()
    if (here / "projects").is_dir():
        return str(here)
    raise Fail("no review home: pass --home, set REVIEW_SWARM_HOME, or run from a directory containing projects/")


def resolve_data(explicit, env=None):
    """--data, then REVIEW_SWARM_DATA. There is no default: runs must never land in a checkout."""
    env = os.environ if env is None else env
    d = explicit or env.get("REVIEW_SWARM_DATA")
    if d:
        p = Path(d).expanduser()
        if not p.is_absolute():
            raise Fail(f"data dir {d!r} must be an absolute path")
        return str(p)
    raise Fail("no data dir: pass --data or set REVIEW_SWARM_DATA (an absolute path outside the home repo)")


def target_state(target):
    return {"commit": git(target, "rev-parse", "HEAD").strip(),
            "dirty": git(target, "status", "--porcelain").strip() != ""}


def match_globs(paths, globs):
    out = []
    for p in paths:
        for g in globs:
            # "src/**" matches everything under src/; fnmatch's * already crosses "/".
            if fnmatch.fnmatchcase(p, g.replace("**", "*")):
                out.append(p)
                break
    return out


def check_pinned(run):
    """Reviewers read the live checkout, so the run is only meaningful while
    the target still is the tree it was pinned to."""
    now = target_state(Path(run["target"]))
    pinned = run["target_state"]
    if now["commit"] != pinned["commit"] or now["dirty"] != pinned["dirty"]:
        raise Fail(f"target moved since `run new`: pinned {pinned['commit'][:12]}"
                   f"{' (dirty)' if pinned['dirty'] else ''}, now {now['commit'][:12]}"
                   f"{' (dirty)' if now['dirty'] else ''}; restore it or start a new run")
    if run["target_state"]["dirty"]:
        # A dirty tree can drift without HEAD moving; warn, don't refuse.
        print("warning: the target was pinned dirty; uncommitted edits since then are not detected",
              file=sys.stderr)


def freeze_tree(target, commit, dest):
    """A read-only copy of the target at the pinned commit. Reviewers read this,
    never the live checkout, so line anchors stay stable while work goes on."""
    dest.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(target), "archive", commit], capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)


def run_cwd(run):
    if run.get("tree"):
        return Path(run["tree"])
    check_pinned(run)
    return Path(run["target"])


def run_concerns(rdir):
    return json.loads((rdir / "project" / "concerns.json").read_text())["concerns"]


ANSWER_CONTRACT = "Answer with ONE final message"


def with_tools(rdir, instruction):
    """A seat's instruction with the project's tools.md (what the machine running
    the review offers beyond the seat's built-in tools: a code index, its tree
    names) placed just before the answer contract. A tool a seat is not told
    about goes unused, even when it is loaded."""
    tools = run_text(rdir, "tools.md")
    if not tools:
        return instruction
    block = "TOOLS AVAILABLE FOR THIS PROJECT:\n" + tools + "\n\n"
    i = instruction.rfind(ANSWER_CONTRACT)
    return instruction[:i] + block + instruction[i:] if i >= 0 else instruction + "\n\n" + block


def run_text(rdir, name):
    f = rdir / "project" / name
    return f.read_text().strip() if f.exists() else ""


def next_attempt(stage_dir):
    """The stage's next attempt dir. mkdir() without exist_ok is the lock: two
    stages started at once can compute the same number, but only one mkdir
    lands, and the loser recomputes and retries."""
    stage_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        nums = [int(p.name) for p in stage_dir.iterdir() if p.is_dir() and p.name.isdigit()]
        d = stage_dir / f"{(max(nums) + 1) if nums else 1:02d}"
        try:
            d.mkdir()
        except FileExistsError:
            continue
        return d
    raise Fail(f"{stage_dir}: no free attempt number after 100 tries")


def run_new(home, project_name, ref, daemon):
    """daemon: what the run records about the rafiki it will run on, e.g.
    {"target": "live", "version": "..."}."""
    pdir = home.project_dir(project_name)
    project = json.loads((pdir / "project.json").read_text())
    target = Path(project["target"]).expanduser().resolve()
    pdir = home.project_dir(project_name, target)
    if ref:
        # A named baseline: freeze exactly that commit, independent of HEAD
        # and of the checkout's state.
        commit = git(target, "rev-parse", f"{ref}^{{commit}}").strip()
        ts = {"commit": commit, "ref": ref, "dirty": False}
        tracked = git(target, "ls-tree", "-r", "--name-only", commit).split()
    else:
        ts = target_state(target)
        if ts["dirty"]:
            print(f"note: {target} has uncommitted edits; the run freezes committed HEAD {ts['commit'][:12]} only")
        tracked = git(target, "ls-files").split()
    files = match_globs(tracked, project["include"])
    must = match_globs(files, project["must_cover"])
    if not must:
        raise Fail("must_cover matched no files")
    run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    rdir = home.runs / project["name"] / run_id
    rdir.mkdir(parents=True)
    shutil.copytree(pdir, rdir / "project")
    concerns = compile_audit_lenses(rdir / "project", home.lens_dir)
    freeze_tree(target, ts["commit"], rdir / "tree")
    run_doc = {
        "run_id": run_id, "project": project, "target": str(target), "target_state": ts,
        "tree": str(rdir / "tree"), "files": files, "must_cover": must,
        "daemon": daemon, "created": now_iso(),
    }
    (rdir / "run.json").write_text(json.dumps(run_doc, indent=2) + "\n")
    gold = rdir / "project" / "gold" / f"{ref}.jsonl" if ref else None
    if gold is not None and gold.exists():
        print(f"gold set: {gold} ({sum(1 for _ in gold.open())} entries); `grade` pre-fills matches")
    print(f"run {run_id}: {project['name']} @ {ts['commit'][:12]}{' (' + ref + ')' if ref else ''}, {len(files)} files "
          f"({len(must)} must be covered), frozen at {rdir / 'tree'}"
          f"{f'; {len(concerns)} audit lenses compiled' if concerns else ''}")
    return run_id


def audit_config(pdir):
    """audit.json: the whole-repo pipeline's seats and lens layer. Absent means
    the legacy shape, a hand-written concerns.json and the review:* seats."""
    f = Path(pdir) / "audit.json"
    if not f.exists():
        return {}
    try:
        doc = json.loads(f.read_text())
    except ValueError as e:
        raise Fail(f"{f}: {e}") from e
    if not isinstance(doc, dict):
        raise Fail(f"{f}: expected an object, got {type(doc).__name__}")
    return doc


def compile_audit_lenses(pdir, home_dir=None):
    """With a lens_dir in audit.json, the run's concerns.json is compiled from
    the generic + repo lens layers at `run new`, so a run snapshots the lenses it
    was started with and no separate build step can leave it stale."""
    cfg = audit_config(pdir)
    if not cfg.get("lens_dir"):
        return None
    if (Path(pdir) / "concerns.json").exists():
        raise Fail(f"{pdir}: audit.json names a lens_dir and concerns.json also exists; keep one")
    lenses = lenses_mod.load_layers(LENSES, Path(cfg["lens_dir"]).expanduser(), home_dir=home_dir)
    concerns = lenses_mod.audit_concerns(lenses, cfg.get("exclude_lenses") or ())
    if not concerns:
        raise Fail(f"{cfg['lens_dir']}: no lens applies to audit")
    (Path(pdir) / "concerns.json").write_text(json.dumps({"concerns": concerns}, indent=1, sort_keys=True) + "\n")
    return concerns


def run_freeze(home, run_id):
    """Give a run created before frozen trees existed its tree + project snapshot."""
    rdir, run = home.load_run(run_id)
    if run.get("tree"):
        raise Fail(f"run already frozen at {run['tree']}")
    target = Path(run["target"])
    freeze_tree(target, run["target_state"]["commit"], rdir / "tree")
    if not (rdir / "project").exists():
        shutil.copytree(home.project_dir(run["project"]["name"], target), rdir / "project")
    run["tree"] = str(rdir / "tree")
    (rdir / "run.json").write_text(json.dumps(run, indent=2) + "\n")
    print(f"froze {run['target_state']['commit'][:12]} at {run['tree']}")
