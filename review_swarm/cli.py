# review-swarm: argument parsing for the operator CLI and the script-child entry (scripts/review.py)
import argparse
import json
import sys
from pathlib import Path

from . import (
    analysis,
    pr_gather,
    pr_review,
    pr_runs,
    pr_state,
    pr_verify,
    presets,
    stages,
)
from . import lenses as lenses_mod
from .common import LENSES, Fail
from .rafiki_io import Rafiki, final_answer
from .runs import Home, resolve_data, resolve_home, run_freeze, run_new
from .sandbox import Sandbox, Target

DEFAULT_REPO = "review-swarm"
STAGES = {"map": stages.stage_map, "find": stages.stage_find, "verify": stages.stage_verify,
          "gather": pr_gather.stage_gather, "review": pr_review.stage_review,
          "verify-pr": pr_verify.stage_verify_pr}


def add_stage_parsers(sub):
    """The stage commands, identical whether the operator runs them in-process
    (review-swarm) or a script child does (scripts/review.py)."""
    m = sub.add_parser("map", help="stage: carve the target into components")
    m.add_argument("run")
    m.add_argument("--model", help="override the seat's model (the preset still fixes prompt, tools, budget); recorded on the attempt")
    m.add_argument("--mappers", type=int, default=1, help="independent mappers (default 1)")
    m.add_argument("--timeout", type=int, default=1200)
    m.add_argument("--collect", metavar="NN", help="re-attach to attempt NN's mappers instead of spawning")

    fd = sub.add_parser("find", help="stage: finders over (component|seam|whole-repo) x concern, rounds until dry")
    fd.add_argument("run")
    fd.add_argument("--model", help="override the seat's model (the preset still fixes prompt, tools, budget); recorded on the attempt")
    fd.add_argument("--arm", action="append", choices=["swarm", "seam", "control", "cross"], help="repeatable; default all")
    fd.add_argument("--only", action="append", help="k=a,b with k in component|concern|arm|cell (cell = arm__subject__concern); repeatable")
    fd.add_argument("--rounds", type=int, default=3, help="max rounds per cell (stops after 2 dry rounds)")
    fd.add_argument("--timeout", type=int, default=1200, help="per-child seconds")
    fd.add_argument("--collect", metavar="NN", help="resume attempt NN: collect its children and continue its rounds")
    fd.add_argument("--seat", choices=["reviewer", "oneshot"], default="reviewer",
                    help="reviewer = pre-filled + read-only tools; "
                         "oneshot = no tools, context inline, needs rounds")
    fd.add_argument("--context", choices=["component", "neighbours"], default="component",
                    help="swarm cells: the component's files, or also its map neighbours'")
    fd.add_argument("--parallel", type=int, default=stages.FIND_CAP, help="workers in flight at once")
    fd.add_argument("--variant", default="", help="label for comparing attempts (e.g. A, B, C)")

    vf = sub.add_parser("verify", help="stage: route each find candidate to a fresh verifier (S1 = 3-lens panel)")
    vf.add_argument("run")
    vf.add_argument("--model", help="override the seat's model (the preset still fixes prompt, tools, budget); recorded on the attempt")
    vf.add_argument("--find", metavar="NN", help="find attempt whose candidates.jsonl to verify")
    vf.add_argument("--only", action="append", help="arm=swarm,seam,control,cross")
    vf.add_argument("--timeout", type=int, default=1200)
    vf.add_argument("--collect", metavar="NN", help="resume verify attempt NN")
    vf.add_argument("--parallel", type=int, default=stages.FIND_CAP, help="workers in flight at once")

    # The PR stages take one run and nothing else to aim them: the packet fixes
    # what every seat reads, so a retuned model and the per-child timeout are all
    # the operator may vary.
    for name, help_ in (("gather", "stage: build the review packet, price it, stop at awaiting-confirm"),
                        ("review", "stage: the finder cells and the always-on lens panel"),
                        ("verify-pr", "stage: verify each cluster with fresh panel children")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("run")
        sp.add_argument("--model", help="override the seat's model (the preset still fixes prompt, tools, budget); recorded on the attempt")
        sp.add_argument("--timeout", type=int, default=1800, help="per-child seconds")
        sp.add_argument("--parallel", type=int, default=stages.FIND_CAP, help="workers in flight at once")

    # Not a stage: a read of finished runs, so no seat and no daemon. It is defined
    # here because this builder is the one parser both entry points share, so the
    # script child can run it as well as the operator CLI.
    cp = sub.add_parser("compare-pr", help="compare finished PR runs: configuration, recall, cost")
    cp.add_argument("runs", nargs="+", metavar="RUN")


def stage_parser(prog):
    ap = argparse.ArgumentParser(prog=prog)
    ap.add_argument("--home", default=None, help="the review home (config); the script shim supplies it")
    ap.add_argument("--data", required=True, help="the data dir: runs/ lives here")
    sub = ap.add_subparsers(dest="stage", required=True)
    add_stage_parsers(sub)
    return ap


def review_main(argv):
    """scripts/review.py: one stage, run as a rafiki script child. Its workers
    are its own children, so the child's budget bounds the whole stage."""
    sys.stdout.reconfigure(line_buffering=True)
    args = stage_parser("review").parse_args(argv)
    if args.stage == "compare-pr":
        # Reached before Rafiki.inside(): this verb reads runs, so a child asked
        # for it needs no daemon and no result envelope.
        compare_pr_cmd(Home(resolve_home(args.home), args.data), args)
        return 0
    rk = Rafiki.inside()
    try:
        summary = STAGES[args.stage](rk, Home(resolve_home(args.home), args.data), args)
    except Fail as e:
        print(f"review: {e}", file=sys.stderr)
        rk.result({"stage": args.stage, "run": args.run, "error": str(e)})
        return 1
    rk.result(summary)
    return 0


def swarm_main(argv=None):
    # Stages run for many minutes, usually with stdout going to a file.
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(prog="review-swarm")
    ap.add_argument("--home", default=None,
                    help="the review home (config): projects/, lenses/ (default: $REVIEW_SWARM_HOME, else the current directory if it holds projects/)")
    ap.add_argument("--data", default=None, help="the data dir: runs/, builds/, .daemon/ (default: $REVIEW_SWARM_DATA; required, never a checkout)")
    ap.add_argument("--target", choices=["live", "sandbox"], default="live",
                    help="live = your rafiki profile (real reviews); sandbox = the sandbox daemon (rafiki builds under test)")
    ap.add_argument("--profile", help="rafiki profile (default: the current one; review for the sandbox)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sb = sub.add_parser("sandbox", help="the sandbox daemon, for trying rafiki builds before deploying")
    sbsub = sb.add_subparsers(dest="scmd", required=True)
    si = sbsub.add_parser("init", help="build binaries, create + migrate a fresh DB, write isolated config")
    si.add_argument("--db", help="database name (default rafiki_review_<yyyymmdd>)")
    si.add_argument("--reuse-db", action="store_true", help="use an existing database of that name")
    sbsub.add_parser("rebuild", help="rebuild binaries from the rafiki checkout's HEAD + migrate")
    sbsub.add_parser("up")
    sbsub.add_parser("down")
    sbsub.add_parser("status")
    sbsub.add_parser("env", help="shell exports for manual poking")

    sm = sub.add_parser("smoke", help="one pre-filled spawn end to end on the target")
    sm.add_argument("--dir", required=True, help="a checkout with a README.md")
    sm.add_argument("--model", default="openrouter/z-ai/glm-5.3-flash")

    p = sub.add_parser("presets", help="the review: seats on the target")
    p.add_subparsers(dest="pcmd", required=True).add_parser("sync", help="put every preset whose resolved spec differs")

    r = sub.add_parser("run", help="review runs")
    rsub = r.add_subparsers(dest="rcmd", required=True)
    rn = rsub.add_parser("new", help="pin a project's target commit into a new run")
    rn.add_argument("project")
    rn.add_argument("--ref", help="freeze this tag/commit (e.g. review-baseline-1) instead of HEAD; enables gold-set grading")
    rf = rsub.add_parser("freeze", help="give an older run its frozen tree + project snapshot")
    rf.add_argument("run")

    la = sub.add_parser("launch", help="run a stage as a script child on the target: launch -- map RUN [flags]")
    la.add_argument("--repo", default=DEFAULT_REPO, help="the registered git source holding scripts/review.py")
    la.add_argument("--max-cost", type=float, help="USD budget for the stage's whole subtree")
    la.add_argument("--max-children", type=int,
                    help="live workers the stage may run at once (default: the stage's --parallel, or --mappers if larger)")
    la.add_argument("stage_args", nargs=argparse.REMAINDER, help="-- <stage> <run> [stage flags]")

    add_stage_parsers(sub)

    gr = sub.add_parser("grade", help="one grading sheet across verify attempts (issue clusters)")
    gr.add_argument("run")
    gr.add_argument("--verify", required=True, metavar="NN[,MM...]")

    cp = sub.add_parser("compare", help="per-variant table: findings, grades, hallucinated refs, cost per backend")
    cp.add_argument("run")
    cp.add_argument("--verify", required=True, metavar="NN[,MM...]")
    cp.add_argument("--providers", action="store_true", help="also tally served providers")

    pr = sub.add_parser("promote", help="make an attempt's output the input of the next stage")
    pr.add_argument("run")
    pr.add_argument("stage")
    pr.add_argument("attempt", help="NN or NN/mapper-N")
    pr.add_argument("--force", action="store_true")

    prun = sub.add_parser("pr", help="PR runs: pin one PR or design doc, then confirm, abort, or read status")
    prsub = prun.add_subparsers(dest="prcmd", required=True)
    pn = prsub.add_parser("new", help="pin a PR or design doc into a new run")
    pn.add_argument("project")
    pn.add_argument("--mode", choices=list(pr_runs.MODES), required=True)
    pn.add_argument("--pr", type=int, help="PR number: gh supplies the shas and the intent")
    pn.add_argument("--base", help="base ref or sha, with --head")
    pn.add_argument("--head", help="head ref or sha, with --base")
    pn.add_argument("--doc", help="design mode: the design text to review")
    pn.add_argument("--since", metavar="RUN", dest="since_run",
                    help="delta mode: review the change since this run's head")
    pc = prsub.add_parser("confirm", help="accept an awaiting-confirm run's estimate and let review run")
    pc.add_argument("run")
    pab = prsub.add_parser("abort", help="end a run that has not finished")
    pab.add_argument("run")
    pst = prsub.add_parser("status", help="one run's state, estimate and counts")
    pst.add_argument("run")
    pd = prsub.add_parser("dispose", help="record a human disposition for one issue in a run's grading sheet")
    pd.add_argument("run")
    pd.add_argument("issue_id", metavar="ISSUE",
                    help="the sheet's issue key: file plus a 30-line bucket, the same key prior.json uses "
                         "(e.g. src/a.py:0)")
    pd.add_argument("disposition", choices=list(analysis.DISPOSITIONS))
    pd.add_argument("--note", help="why: the deciding lines")

    ln = sub.add_parser("lenses", help="the lens files, compiled into a project's concerns.json + manifest")
    lnsub = ln.add_subparsers(dest="lcmd", required=True)
    lb = lnsub.add_parser("build", help="compile the generic + repo lenses for a project")
    lb.add_argument("project")

    fr = sub.add_parser("friction", help="friction entries rolled up: where a run was pushed off its path")
    fr.add_argument("run", nargs="?", help="one run id")
    fr.add_argument("--project", help="every PR run of this project instead")

    rp = sub.add_parser("report", help="write a run's markdown review report")
    rp.add_argument("run")
    rp.add_argument("--out", required=True, help="where to write it (outside the run)")

    st = sub.add_parser("status", help="attempts, children, cost per stage")
    st.add_argument("run")

    kp = sub.add_parser("stop", help="stop a run's live children: the script child first, then its workers")
    kp.add_argument("run")
    kp.add_argument("--stage", choices=["map", "find", "verify"], help="only that stage's children")

    args = ap.parse_args(argv)
    try:
        home = Home(resolve_home(args.home), resolve_data(args.data))
        dispatch(args, home)
    except Fail as e:
        print(f"review-swarm: {e}", file=sys.stderr)
        return 1
    return 0


def dispatch(args, home):
    if args.cmd == "sandbox":
        sb = Sandbox(home)
        {"init": lambda: sb.init(args.db, args.reuse_db), "rebuild": sb.rebuild, "up": sb.up,
         "down": sb.down, "status": sb.status, "env": sb.print_env}[args.scmd]()
        return
    if args.cmd in ("grade", "promote", "status"):
        {"grade": analysis.grade, "promote": analysis.promote, "status": analysis.status}[args.cmd](home, args)
        return
    if args.cmd == "run" and args.rcmd == "freeze":
        run_freeze(home, args.run)
        return
    # These read and write no more than the run directory: no daemon is needed,
    # so they are answered before a target is chosen (pr new picks its own).
    if args.cmd == "pr":
        pr_command(args, home)
        return
    if args.cmd == "lenses":
        lenses_build(home, args)
        return
    if args.cmd == "friction":
        friction_table(home, args)
        return
    if args.cmd == "report":
        report_cmd(home, args)
        return
    if args.cmd == "compare-pr":
        compare_pr_cmd(home, args)
        return
    target = Target(args.target, home, args.profile)
    if args.cmd == "presets":
        presets.sync(target.client())
    elif args.cmd == "run":
        run_new(home, args.project, args.ref, target.describe())
    elif args.cmd == "launch":
        launch(target, home, args)
    elif args.cmd == "stop":
        stop_run(target.client(), args)
    elif args.cmd == "smoke":
        smoke(target.client(), args)
    elif args.cmd == "compare":
        analysis.compare(target.client(), home, args)
    else:
        STAGES[args.cmd](target.client(), home, args)


def pr_command(args, home):
    {"new": lambda: pr_new(home, args, Target(args.target, home, args.profile)),
     "confirm": lambda: pr_confirm(home, args),
     "abort": lambda: pr_abort(home, args),
     "dispose": lambda: pr_dispose(home, args),
     "status": lambda: pr_status(home, args)}[args.prcmd]()


def pr_new(home, args, target):
    """pr_run_new prints its own human line; the run id is printed again, alone,
    because that is the piece a caller running these verbs in a pipeline wants."""
    _, run = pr_runs.pr_run_new(home, args.project, args.mode, pr=args.pr, base=args.base,
                                head=args.head, doc=args.doc, since_run=args.since_run,
                                daemon=target.describe())
    print(run["run_id"])


def pr_confirm(home, args):
    rdir, run = home.load_run(args.run)
    state = pr_state.load_state(rdir)
    if state.get("status") != "awaiting-confirm":
        raise Fail(f"run {run['run_id']} is {state.get('status')}; "
                   f"only an awaiting-confirm run is confirmed")
    est = (state.get("gather") or {}).get("estimate")
    if not isinstance(est, dict):
        raise Fail(f"run {run['run_id']} records no gather estimate at {rdir / 'state.json'}; "
                   f"gather the run first")
    print(json.dumps(est, indent=1, sort_keys=True))
    for line in fit_report(est):
        print(line)
    state["status"] = "confirmed"
    pr_state.save_state(rdir, state)
    print(f"run {run['run_id']}: confirmed")


def fit_report(est):
    """What the estimate can and cannot say about fit, in words, for the confirm
    prompt and `pr status`.

    The design's one hard limit is a seat model's context window, and every seat
    in a real run is unchecked against one: no shipped preset carries a window
    and the SDK exposes no model catalog, so gather records each such seat in
    `not_reported` and reports its window as null. That is the absence of a
    check, not a claim of fit, and the operator has to be told so plainly rather
    than left to read a null out of the JSON. Nothing is defaulted in its place,
    because an assumed window would be a false claim about fit, and the blocking
    path is kept for the seat whose window IS known and is too small. A seat that
    cannot take the packet and is not known to be too small fails at run time as
    parse-error friction."""
    unreported = [b for b in est.get("not_reported") or [] if isinstance(b, dict)]
    blocking = [b for b in est.get("blocking") or [] if isinstance(b, dict)]
    seats = est.get("seats") or {}
    out = []
    if unreported:
        names = ", ".join(str(b.get("seat")) for b in unreported)
        out.append(f"fit UNCHECKED: no context window is reported for {names} (their presets carry none "
                   f"and the SDK has no model catalog to look one up in), so this estimate is not a claim "
                   f"that the packet ({est.get('total_tokens')} tokens) fits any of those seats. A seat "
                   f"that cannot take it fails at run time as parse-error friction.")
    elif not blocking:
        out.append("fit checked: every seat's context window is reported and the packet is inside it.")
    # The one hard limit is a seat's context window, and nothing is ever cut, so
    # the operator's ways out are named: confirming runs the seat anyway, and the
    # alternative is a new run with the seat repointed (or dropped) in pr.json.
    # The design's other option, running the blocked seat without it or on
    # another model inside the same run, is parked until seat policy carries a
    # window: with none reported no seat can reach this branch, so the in-run
    # remedy would be unreachable code.
    for b in blocking:
        seat = seats.get(b.get("seat")) or {}
        out.append(f"blocking: {b.get('seat')} ({seat.get('model')}, {seat.get('kind')}): "
                   f"{b.get('reason')}; confirm runs it anyway, else abort and start a new run with the "
                   f"seat repointed or dropped")
    return out


def pr_abort(home, args):
    rdir, run = home.load_run(args.run)
    state = pr_state.load_state(rdir)
    abortable(rdir, run, state)
    # Read the state again as late as possible and change only `status`. A stage
    # that saved a section of state.json between the read above and this save
    # keeps it, and the check is repeated on the fresh dict because the whole
    # point is to act on the state as it is at the moment of writing.
    fresh = pr_state.load_state(rdir)
    abortable(rdir, run, fresh)
    was = fresh.get("status")
    fresh["status"] = "aborted"
    pr_state.save_state(rdir, fresh)
    print(f"run {run['run_id']}: aborted (was {was})")


def abortable(rdir, run, state):
    """Refuse to abort a run a stage is working on, or one that has finished.

    A live stage holds the whole state dict it read when it started and saves it
    as it goes (review moves the status to `verifying`, verify finishes it as
    `done`, an unwind puts it back), so an abort written underneath it is lost
    while its children keep spending. Naming the stage, and where its children
    are, is what lets the operator stop that stage first instead.
    """
    live = live_stage(rdir, state)
    if live:
        raise Fail(f"run {run['run_id']}: {live}, so an abort now would be overwritten when that stage "
                   f"saves the status it read at its start. Stop its children first (`rafiki stop <cid>`; "
                   f"the ids are in the attempt's children.json) or wait for it to finish. A marker left "
                   f"by a process that is gone can be removed and the abort retried")
    if state.get("status") == "done":
        # A done run is the record its attempts and its report point at: rewriting
        # its status would be editing a finished run.
        raise Fail(f"run {run['run_id']} is done; a finished run is not edited")


def live_stage(rdir, state):
    """The stage live on this run, as a phrase for the refusal, or None. Both the
    claim file and the state marker are checked: the marker is what a process that
    died between the two leaves behind, and the file is what a stage holds before
    its first save."""
    for stage, name in (("gather", pr_gather.CLAIM), ("review", pr_review.CLAIM),
                        ("verify", pr_verify.CLAIM)):
        f = rdir / name
        if f.exists():
            return f"a {stage} is live ({f.name}: {claim_who(f)})"
    for key, stage in (("in_progress", "gather"), ("verify_in_progress", "verify")):
        m = state.get(key)
        if isinstance(m, dict):
            return f"a {stage} is live (state.json {key}: pid {m.get('pid')}, at {m.get('at')})"
    return None


def claim_who(path):
    """Who holds a claim file, for the refusal. An unreadable marker is reported
    as such rather than raised: the refusal is what matters."""
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return f"marker unreadable: {e}"
    if not isinstance(doc, dict):
        return f"marker is {type(doc).__name__}, not an object"
    return f"pid {doc.get('pid')} since {doc.get('at') or doc.get('started')}"


def pr_dispose(home, args):
    """Record a human disposition for one issue in the run's grading sheet.

    The sheet is the one sanctioned edit to a finished run (the model grades stay
    marked until a human confirms them), so this writes it and nothing else: the
    run's packet, verdicts and state are never touched. The issue key is the
    sheet's own, `analysis.issue_key` (file plus a 30-line bucket), the same key
    the run report and prior.json use, so a delta built from this run carries the
    disposition forward.
    """
    rdir, run = home.load_run(args.run)
    rec = analysis.set_disposition(rdir, args.issue_id, args.disposition, args.note)
    tail = f" ({rec['note']})" if rec.get("note") else ""
    print(f"run {run['run_id']}: {rec['issue']} {rec['disposition']}{tail}")


def pr_status(home, args):
    rdir, run = home.load_run(args.run)
    state = pr_state.load_state(rdir)
    where = f"{run['base'][:12]}..{run['head'][:12]}" if run.get("base") else run["head"][:12]
    print(f"run {run['run_id']}: {run['project']['name']} {run['mode']} {state.get('status')}")
    print(f"  {where} {run['target']}")
    gather = state.get("gather") or {}
    est = gather.get("estimate")
    if not isinstance(est, dict):
        print("  estimate: none yet (gather the run to build and price the packet)")
    else:
        bound = (est.get("fundi") or {}).get("usd_upper_bound")
        print(f"  estimate: {est.get('total_tokens')} tokens, confirm over {gather.get('confirm_over_tokens')}, "
              f"fundi upper bound {'UNKNOWN' if bound is None else f'${bound:.2f}'}, "
              f"claude {(est.get('claude') or {}).get('seat_count')} seats")
        for line in fit_report(est):
            print(f"  {line}")
    counts = status_counts(rdir, state)
    print("  counts: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"))


def status_counts(rdir, state):
    """What the run has produced so far, from the run's own records: the newest
    review attempt's pools and, once verify has finished, its counts. A review
    that died mid-attempt has no candidates.jsonl, which counts nothing."""
    out = {}
    ver = state.get("verify")
    # The review attempt the verdicts came from, not the newest one: a rerun
    # may have left a later, abandoned attempt behind.
    named = ver.get("review") if isinstance(ver, dict) else None
    radir = rdir / "review" / named if named else newest_attempt(rdir / "review")
    if radir is not None:
        out["candidates"] = jsonl_count(radir / "candidates.jsonl")
        out["checklist"] = jsonl_count(radir / "checklist.jsonl")
    if isinstance(ver, dict):
        out.update({k: v for k, v in ver.items() if isinstance(v, int) and not isinstance(v, bool)})
    else:
        # Verify crashed mid-attempt or has not run: its verdicts on disk are
        # still the count a reader can trust.
        vadir = newest_attempt(rdir / "verify")
        if vadir is not None:
            out["verdicts"] = jsonl_count(vadir / "verdicts.jsonl")
    return out


def newest_attempt(stage_dir):
    """The highest-numbered attempt under one stage directory, or None."""
    if not stage_dir.is_dir():
        return None
    dirs = sorted((p for p in stage_dir.iterdir() if p.is_dir() and p.name.isdigit()),
                  key=lambda p: int(p.name))
    return dirs[-1] if dirs else None


def jsonl_count(path):
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text().splitlines() if line.strip())


def lenses_build(home, args):
    """Compile a project's lenses the way gather does, so a lens edit is validated
    (and a bad lens named) before a run pays for a packet."""
    pdir = home.project_dir(args.project)
    cfg = pdir / "project.json"
    try:
        doc = json.loads(cfg.read_text())
    except (OSError, ValueError) as e:
        raise Fail(f"project {args.project}: {cfg}: {e}") from e
    if not isinstance(doc, dict):
        raise Fail(f"project {args.project}: {cfg}: expected an object, got {type(doc).__name__}")
    target = doc.get("target")
    if not isinstance(target, str) or not target.strip():
        raise Fail(f"project {args.project}: {cfg} records no target to take the repo dir from")
    pdir = home.project_dir(args.project, Path(target).expanduser().resolve())
    # A PR project names its lens_dir in pr.json; an audit-only project in audit.json.
    f = pdir / "pr.json"
    if not f.exists():
        f = pdir / "audit.json"
    try:
        pjson = json.loads(f.read_text())
    except FileNotFoundError:
        raise Fail(f"no PR seat config at {pdir / 'pr.json'} and no audit config at {f}") from None
    except (OSError, ValueError) as e:
        raise Fail(f"{f}: {e}") from e
    if not isinstance(pjson, dict):
        raise Fail(f"{f}: expected an object, got {type(pjson).__name__}")
    lens_dir = pjson.get("lens_dir")
    # layers: shipped LENSES < <home>/lenses < the project's lens_dir; output goes to the data dir, never the home.
    out_dir = home.data / "builds" / args.project
    built = lenses_mod.build(LENSES, Path(lens_dir).expanduser() if lens_dir else None,
                             pr_gather.gold_slugs(pdir), out_dir, home_dir=home.lens_dir)
    manifest = out_dir / "lenses.manifest.json"
    try:
        doc = json.loads(manifest.read_text())
    except (OSError, ValueError) as e:
        raise Fail(f"{manifest}: {e}") from e
    print(json.dumps(doc, indent=1, sort_keys=True))
    print(f"lenses: {len(built)} compiled to {out_dir}")


def compare_pr_cmd(home, args):
    """Sorted JSON, so the same comparison of the same runs is the same bytes."""
    print(json.dumps(analysis.compare_pr(home, args.runs), indent=1, sort_keys=True))


def friction_table(home, args):
    if bool(args.run) == bool(args.project):
        raise Fail("friction: pass exactly one of a run id or --project P")
    # a parallel task: import it where it is used, not at module top
    from . import pr_report
    rows = pr_report.friction_rollup(home, run=args.run, project=args.project)
    if not rows:
        print("no friction recorded")
        return
    header = ("seat", "kind", "count", "runs")
    cells = [[friction_cell(r.get(c)) for c in header] for r in rows]
    widths = [max(len(h), *(len(row[i]) for row in cells)) for i, h in enumerate(header)]
    print("  ".join(h.ljust(w) for h, w in zip(header, widths)).rstrip())
    for row in cells:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip())


def friction_cell(v):
    """One rollup value as text. `runs` is a list of run ids in one reading of the
    interface and a count in the other, so both print rather than assuming."""
    if v is None:
        return ""
    if isinstance(v, (list, tuple, set)):
        return ",".join(sorted(str(x) for x in v))
    return str(v)


def report_cmd(home, args):
    # a parallel task: import it where it is used, not at module top
    from . import audit_report, pr_report
    rdir, run = home.load_run(args.run)
    # A PR run records its mode; a whole-repo audit run (map, find, verify) has none.
    write = pr_report.write_report if run.get("mode") else audit_report.write_audit_report
    out = write(rdir, Path(args.out).expanduser())
    print(f"report: {out}")


def launch(target, home, args):
    rest = args.stage_args[1:] if args.stage_args[:1] == ["--"] else args.stage_args
    # Parse locally first: a typo fails here, not minutes later in a child log.
    parsed = stage_parser("launch").parse_args(["--home", str(home.path), "--data", str(home.data)] + rest)
    if parsed.stage == "compare-pr":
        raise Fail("launch runs one stage; compare-pr is not a stage: run it directly")
    home.load_run(parsed.run)
    # The stage keeps --parallel workers in flight; without a grant the daemon's
    # default live-children cap would refuse the rest of every batch.
    max_children = args.max_children or max(getattr(parsed, "parallel", stages.FIND_CAP), getattr(parsed, "mappers", 0))
    rk = target.client()
    cid = rk.spawn_script(f"review-{parsed.stage}-{parsed.run}", cwd=home.data, repo=args.repo, script="review",
                          args=["--data", str(home.data)] + rest, max_cost=args.max_cost, max_children=max_children,
                          labels={"review/run": parsed.run, "review/stage": parsed.stage})
    print(f"launched {cid}: {parsed.stage} {parsed.run} on {target.kind} ({args.repo}:review)")
    # A script child has no conversation events; --all prints its stdout/stderr.
    print(f"output:  rafiki{' -P ' + target.profile if target.profile else ''} logs {cid} --all")


def stop_run(rk, args):
    """`stop <run> [--stage …]`: stop the run's live children: the script
    child first (killing it does not sweep the children it spawned), then the
    workers. Workers are listed fresh after the scripts are down: a running
    script refills its slots as workers settle, so the pre-stop snapshot can
    miss workers it spawned while it was dying. A stop that errors is printed
    and the loop continues, because the SDK's stop() can time out on a live
    script child (a known rafiki gap): those ids are reported as possibly
    still live."""
    want = {"review/run": args.run}
    if args.stage:
        want["review/stage"] = args.stage
    kids = [c for c in rk.c.list(labels=want) if c.status != "exited"]
    stuck = []

    def stop_ids(ids):
        for cid in ids:
            try:
                rk.stop(cid, report=True)
                print(f"stopped {cid}")
            except Fail as e:
                print(f"{cid}: STOP FAILED {e}; may still be live")
                stuck.append(cid)

    scripts = [c.child_id for c in kids if c.kind == "script"]
    stop_ids(scripts)
    # Re-list once the scripts are stopped: the snapshot above predates the
    # script's death, and it spawns replacement workers as its slots settle.
    fresh = [c.child_id for c in rk.c.list(labels=want)
             if c.status != "exited" and c.kind != "script"]
    stop_ids(fresh)
    total = len(scripts) + len(fresh)
    if not total:
        print(f"nothing live for run {args.run}{' stage ' + args.stage if args.stage else ''}")
    elif stuck:
        print(f"{total - len(stuck)} of {total} stopped; may still be live: {' '.join(stuck)}")
    else:
        print(f"{total} stopped")


def smoke(rk, args):
    """One pre-filled spawn against a checkout, end to end: spawn, prompt,
    settle, export, stop. Proves the daemon + executor + pre-fill path."""
    target = Path(args.dir).expanduser().resolve()
    if not (target / "README.md").exists():
        raise Fail(f"{target / 'README.md'} not found; smoke pre-fills README.md:1-12")
    cid, conv_id = rk.spawn("smoke", cwd=target, prefill_lines=["README.md:1-12"],
                            labels={"review/stage": "smoke"}, model=args.model)
    print(f"spawned {cid} (conversation {conv_id})")
    rk.prompt(cid, "Without calling any tools, quote the first sentence of the README you were given.")
    info, conv = stages.wait_settled(rk, [cid], timeout=300)[cid]
    rk.stop(cid)
    if conv is None:
        raise Fail(f"smoke child {cid} did not settle (status {info.get('status')})")
    turns = conv["turns"]
    shape_ok = (turns[0]["role"] == "user"
                and turns[1]["content"][0].get("id") == "prefill_0001"
                and turns[2]["content"][0].get("tool_use_id") == "prefill_0001")
    tool_calls = sum(1 for t in turns[3:] for b in (t.get("content") or []) if b.get("type") == "tool_use")
    print(f"prefill shape: {'ok' if shape_ok else 'WRONG'}; turns {len(turns)}; "
          f"tool calls after prefill {tool_calls}; cost ${info.get('cost_usd') or 0:.4f}")
    print(f"answer: {final_answer(conv)[:300]}")
    if not shape_ok:
        raise Fail("pre-fill rows are not the expected r0/r1/r2 shape")
