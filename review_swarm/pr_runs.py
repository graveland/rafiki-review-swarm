# review-swarm: PR runs - pin one PR (or a design doc) into a run directory with
# a frozen head tree, a frozen base tree, and the packet record the stages read
import datetime
import json
import re
import shutil
from pathlib import Path

from . import pr_state
from .common import Fail, git, now_iso, run
from .runs import freeze_tree

MODES = ("design", "pr", "delta")

# A remote URL in one of the shapes git writes. The scp-like form must not take
# "https://..." for a host, hence the (?!//) guard.
_URL = re.compile(r"^(?:ssh|git|https?)://(?:[^@/]+@)?[\w.-]+(?::\d+)?/(?P<path>.+?)/?$")
_SCP = re.compile(r"^(?:[^@/]+@)?[\w.-]+:(?!//)(?P<path>.+?)/?$")


def remote_url(target):
    """The target's origin URL, or its only remote's. A run records owner/repo
    from here, so a repo with no usable remote cannot be a PR run."""
    p = run(["git", "-C", str(target), "remote", "get-url", "origin"], check=False)
    if p.returncode == 0 and p.stdout.strip():
        return p.stdout.strip()
    remotes = git(target, "remote").splitlines()
    if len(remotes) == 1:
        return git(target, "remote", "get-url", remotes[0]).strip()
    raise Fail(f"{target}: no origin remote to take owner/repo from"
               f" (remotes: {', '.join(remotes) or 'none'})")


def review_config(path):
    """<target>/.review/ is this tool's own config location, committed into some
    targets. It is never part of the change under review."""
    p = str(path)
    return p == ".review" or p.startswith(".review/")


def owner_repo(url):
    for rx in (_URL, _SCP):
        m = rx.match(url.strip())
        if not m:
            continue
        path = m.group("path").strip("/")
        path = path.removesuffix(".git")
        parts = path.split("/")
        if len(parts) == 2 and all(parts):
            return f"{parts[0]}/{parts[1]}"
        raise Fail(f"remote {url!r}: expected owner/repo, got {path!r}")
    raise Fail(f"remote {url!r}: not a git URL in a shape that names owner/repo")


def gh_pr_view(pr, repo):
    """`gh pr view` for one PR: the shas to pin and the intent to review. Its own
    function so a test can drive the PR side without a gh or a network."""
    p = run(["gh", "pr", "view", str(pr), "-R", repo, "--json",
             "baseRefOid,headRefOid,title,body"])
    try:
        doc = json.loads(p.stdout)
    except json.JSONDecodeError as e:
        raise Fail(f"gh pr view {pr} -R {repo}: {e}") from e
    if not isinstance(doc, dict):
        raise Fail(f"gh pr view {pr} -R {repo}: expected an object, got {type(doc).__name__}")
    return doc


def resolve_commit(target, remote, ref):
    """A ref or sha as the full commit sha, fetching it when the target does not
    have it yet: a PR's shas are often newer than any local fetch."""
    p = run(["git", "-C", str(target), "rev-parse", f"{ref}^{{commit}}"], check=False)
    if p.returncode == 0 and p.stdout.strip():
        return p.stdout.strip()
    try:
        run(["git", "-C", str(target), "fetch", remote, ref])
    except Fail as e:
        raise Fail(f"commit {ref}: not in {target} and `git fetch {remote} {ref}` failed: {e}") from e
    p = run(["git", "-C", str(target), "rev-parse", f"{ref}^{{commit}}"], check=False)
    if p.returncode != 0 or not p.stdout.strip():
        raise Fail(f"commit {ref}: fetched from {remote} but still not resolvable in {target}")
    return p.stdout.strip()


def resolve_branch(target, names=("main", "master")):
    """A design run reviews the design against the target's trunk, whichever of
    the two names the repo uses."""
    for name in names:
        p = run(["git", "-C", str(target), "rev-parse", f"{name}^{{commit}}"], check=False)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    raise Fail(f"{target}: neither {' nor '.join(names)} resolves; a design run needs a trunk to freeze")


def commit_intent(target, base, head):
    """An explicit base..head has no PR description, so the commits are the
    intent: the head subject as the title and the range's log as the body."""
    title = git(target, "log", "-1", "--format=%s", head).strip()
    body = git(target, "log", "--no-color", "--format=%h %s%n%n%b", f"{base}..{head}").strip()
    return title, body


def pr_run_new(home, project, mode, *, pr=None, base=None, head=None, doc=None,
               since_run=None, daemon):
    """Pin a change into a new run directory: `<project>/<run-id>` with the
    head tree frozen, the base tree frozen (pr/delta), and run.json + state.json
    recording what was pinned. Returns (run directory, the run document)."""
    if mode not in MODES:
        raise Fail(f"unknown mode {mode!r}: expected one of {', '.join(MODES)}")
    pdir = home.project_dir(project)
    project_doc = json.loads((pdir / "project.json").read_text())
    target = Path(project_doc["target"]).expanduser().resolve()
    pdir = home.project_dir(project, target)

    repo = title = body = since = None
    doc_path = None
    if mode == "design":
        if not doc:
            raise Fail("design mode: doc=<path to the design text> is required")
        doc_path = Path(doc).expanduser()
        if not doc_path.is_file():
            raise Fail(f"design doc {doc_path} not found")
        head_sha = resolve_branch(target)
        base_sha = None
    else:
        remote = remote_url(target)
        repo = owner_repo(remote)
        if mode == "delta" and not since_run:
            raise Fail("delta mode: since_run=<run id> is required")
        if pr is not None:
            meta = gh_pr_view(pr, repo)
            title, body = meta.get("title") or "", meta.get("body") or ""
            pr_base = meta["baseRefOid"]
            pr_head = meta["headRefOid"]
        elif mode == "pr" and base and head:
            pr_base, pr_head = base, head
        elif mode == "pr":
            raise Fail("pr mode: pass pr=<number>, or base=<ref> and head=<ref>")
        else:
            # delta without a PR number cannot ask gh for the current head.
            raise Fail("delta mode: pr=<number> is required")
        if mode == "delta":
            sdir, srun = home.load_run(since_run)
            if not srun.get("head"):
                raise Fail(f"since run {since_run} ({sdir}) records no head")
            since = since_run
            base_sha = resolve_commit(target, remote, srun["head"])
        else:
            base_sha = resolve_commit(target, remote, pr_base)
        head_sha = resolve_commit(target, remote, pr_head)
        if pr is None:
            # An explicit base..head has no PR description to take the intent from.
            title, body = commit_intent(target, base_sha, head_sha)

    files = sorted(l for l in git(target, "diff", "--name-only", base_sha, head_sha).splitlines() if l) \
        if base_sha else []

    # <target>/.review/ is this tool's own config location, committed into some
    # targets: it is never part of the change under review, and a gold/ under it
    # would put the calibration answer key in a tree every seat can read.
    dropped = [p for p in files if review_config(p)]
    files = [p for p in files if not review_config(p)]
    run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    rdir = home.runs / project_doc["name"] / run_id
    if rdir.exists():
        # Two runs in the same second would otherwise share a directory, and a
        # run is never edited once it exists.
        raise Fail(f"run {run_id} already exists at {rdir}; retry in a second")
    rdir.mkdir(parents=True)
    # gold/ stays in the project: every seat runs with cwd = the run directory
    # and can read by relative path, and gold is the calibration answer key.
    # Grading reads it from projects/<p>/gold/, never from the run.
    shutil.copytree(pdir, rdir / "project", ignore=shutil.ignore_patterns("gold"))
    # Reviewers read the frozen trees, so a moved target or a force-push cannot
    # shift the line anchors under a review that is already running.
    freeze_tree(target, head_sha, rdir / "tree")
    if base_sha:
        freeze_tree(target, base_sha, rdir / "base_tree")
    for path, what in ((rdir / "tree", "tree"), (rdir / "base_tree", "base_tree")):
        if (path / ".review").exists():
            shutil.rmtree(path / ".review")
            pr_state.append_friction(rdir, {"kind": "review-config-drop",
                                            "note": f"{what}/.review comes from the target's own checkout; "
                                                    f"left out of the run as this tool's config"})
    for p in dropped:
        pr_state.append_friction(rdir, {"kind": "review-config-drop",
                                        "note": f"{p} is under the target's .review/, this tool's own "
                                                f"config; left out of the change under review"})
    run_doc = {
        "run_id": run_id, "project": project_doc, "mode": mode, "target": str(target),
        "base": base_sha, "head": head_sha, "tree": str(rdir / "tree"),
        "files_changed": files, "daemon": daemon, "created": now_iso(), "kind": "pr",
    }
    if base_sha:
        run_doc["base_tree"] = str(rdir / "base_tree")
    if since:
        run_doc["since_run"] = since
    if mode == "design":
        run_doc["doc"] = str(doc_path)
    else:
        # gather builds intent.md and threads.md from these: the PR metadata is
        # snapshotted here so a later edit to the PR body cannot rewrite a run.
        run_doc.update({"pr": pr, "repo": repo, "title": title, "body": body})
    (rdir / "run.json").write_text(json.dumps(run_doc, indent=2, sort_keys=True) + "\n")
    pr_state.save_state(rdir, {"status": "gathering", "run_id": run_id, "mode": mode, "kind": "pr"})
    print(f"run {run_id}: {project_doc['name']} {mode} {str(base_sha)[:12]}..{str(head_sha)[:12]}, "
          f"{len(files)} changed files, frozen at {rdir}")
    return rdir, run_doc
