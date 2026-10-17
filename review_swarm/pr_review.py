# review-swarm: the PR review stage - one finder cell per (lens, file group) plus
# an independent panel, every seat fed the same packet
"""Every seat here receives `packet/files.txt` verbatim, in the order gather
wrote it: fundi seats pre-filled, claude-kind seats as a read list. That order
is the shared prompt-cache prefix, so it is the caller's and nothing in this
module may reorder it or add a per-seat file.

Finder cells are one per lens, times one per file group when gather split the
change; the panel is one child per seat in `pr.json` `panel`, reviewing the
whole change against the intent. Findings, checklist answers, questions and
prior re-judgements pool into `review/NN/*.jsonl`, one record per line, every
line `sort_keys`: an attempt's bytes must depend only on the answers, not on
the order children happened to settle in. A pooled finding carries the always-on
lens it serves (the cell's for a finder, the one it names for a panel seat), so a
panel "findings" checklist answer can be checked against a finding that backs it.

Nothing here is dropped silently: a record the pool cannot carry, a spawn that
failed, a packet entry the two delivery modes cannot agree on and a panel lens
left unanswered each become a friction entry naming the seat and the cell.
"""

import contextlib
import hashlib
import json
import os
import sys
import time

from . import pr_state
from .analysis import hallucination_checks
from .common import (
    AtCap,
    Fail,
    fanout,
    finder_lenses,
    last_json_block,
    now_iso,
    render_prompt,
)
from .rafiki_io import final_answer
from .runs import next_attempt, run_cwd
from .stages import AT_CAP, child_metrics, model_labels, settled

DEFAULT_TIMEOUT = 1800

# The stage's claim on the run, held for as long as a review is running. It is a
# file rather than a state.json field because creating it is the atomic step
# (O_EXCL): two invocations that both read `confirmed` cannot both claim, while
# they could both read-then-write the same status.
CLAIM = "review.lock"

# The daemon routes any pre-fill path containing one of these to the glob tool
# (rafiki pkg/prefill: IsGlob), where a glob that matches nothing is fatal for
# the child and a glob that matches something reads files other than the entry
# names. A claude-kind seat reads the same listing literally. The pre-fill
# protocol carries path/start/end and no escape, so a path with a metacharacter
# cannot be made to mean one file in both delivery modes.
FUNDI_GLOB = "*?[{"

# What the panel does in addition to the always-on checklist, per run mode. pr
# has no extra duty: the panel's whole-change review is the point of the mode.
MODE_RULES = {
    "design": ("Review the design document's claims and proposed mechanisms against the current code. "
               "Add every question the design must answer before implementation to `questions`."),
    "delta": ("Re-judge each prior finding in packet/prior.json: fixed, not-fixed, or incomplete, "
              "with the deciding lines, in `prior`."),
    "pr": "",
}


def slug(text):
    """A cell name that is stable and safe as a directory name."""
    out = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in str(text).lower()).strip("-")
    return out or "cell"


def finding_id(f):
    """A finding's identity across seats: its anchor and its title, so the same
    issue raised by a finder and by the panel keeps one id and two `raised_by`s."""
    title = " ".join(str(f.get("title", "")).lower().split())
    return hashlib.sha256(f"{f.get('file')}\0{int_line(f)}\0{title}".encode()).hexdigest()[:12]


def int_line(f):
    """A finding's line as an int. A model that writes "a.py:3" into the line
    field still gets an id and a record rather than a crash at pool time."""
    try:
        return int(f.get("line") or 0)
    except (TypeError, ValueError):
        return 0


def line_contract(f):
    """Why a finding's line is not the int the schema promises, or None. The pool
    carries an int whichever way the answer read, so the difference is friction:
    a silently zeroed line hides a broken answer."""
    raw = f.get("line")
    if raw is None:
        return "no line given"
    if isinstance(raw, bool) or not isinstance(raw, int):
        return f"line {raw!r} is not a number"
    return None


def as_list(doc, key):
    """The list under `key`, or []: a model that answers a list field with a
    string or a null must not take collection down with it."""
    v = doc.get(key)
    return v if isinstance(v, list) else []


def finding_lens(job, f):
    """The always-on lens a pooled finding serves. A finder cell is one lens, so
    its findings carry the cell's; a panel seat reviews the whole change and names
    the lens on the finding itself, and pr_verify checks a panel "findings"
    checklist answer against that declaration. A panel finding that names no lens
    therefore backs no answer.

    Both shapes carry the key, so a candidate's lens reads the same way wherever
    it came from."""
    if job["kind"] == "finder":
        return str(job["lens"] or "").strip()
    return str(f.get("lens") or "").strip()


def read_json(path, what):
    try:
        return json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise Fail(f"{path}: {what}: {e}") from e


def stage_friction(rdir, kind, note, seat="review-pr"):
    """Friction for something the stage itself decided, not a seat's answer: the
    entry names the stage (or "packet") because there is no cell behind it."""
    pr_state.append_friction(rdir, {"seat": seat, "kind": kind, "note": note})


def claim_review(rdir):
    """Take the stage's claim on the run, or refuse if another review holds it.
    The file names its holder by pid and start time: after a process dies without
    unwinding it is the only record of which review is still out there (its
    attempt is in children.json and the status)."""
    f = rdir / CLAIM
    try:
        fd = os.open(f, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as e:
        try:
            holder = f.read_text().strip()
        except OSError:
            holder = "(unreadable)"
        raise Fail(f"review is already running: {f} is held by {holder}; if that review is gone, "
                   f"remove the file and start again") from e
    except OSError as e:
        raise Fail(f"claim review {f}: {e}") from e
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps({"pid": os.getpid(), "started": now_iso()}, sort_keys=True) + "\n")
    except OSError as e:
        # A claim file with nothing in it is worse than none: it would refuse the
        # next review while naming no holder.
        with contextlib.suppress(OSError):
            f.unlink()
        raise Fail(f"claim review {f}: {e}") from e


def release_review(rdir):
    """Drop the claim. Best effort and loud about it: a lock left behind is what
    refuses the next review, so it must not go unsaid."""
    f = rdir / CLAIM
    try:
        f.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"warning: {f} not removed ({e}); the next review will refuse until it is", file=sys.stderr)


def abandon_review(rk, rdir, state, spawned, exc):
    """End a review that did not finish, best effort at every step and silent
    about none of them: record why we are unwinding, ask every child we spawned
    to stop, name each child that would not stop, then put the run back at a
    status the next invocation can act on and give the claim back.

    A lost daemon is the likeliest cause of the exception that brought us here,
    and it is also what makes `stop` raise, so an unwind that gave up at the
    first failed stop would do nothing exactly when it is most needed and strand
    the run at `reviewing` with the lock held and no record of why. Every
    exception leaves a `stage-error` entry (a Fail raised mid-stage has no other
    record in the run), every refused stop leaves a `stop-failed` entry naming
    the child that may still be live, and the status and the claim are always
    moved on. A child named `stop-failed` may outlive the attempt, which is what
    the entry says; the next review can refuse or stop it from the record."""
    stage_friction(rdir, "stage-error", f"{type(exc).__name__}: {exc}")
    stop_failed = []
    for key in sorted(spawned):
        cid = spawned[key]["child_id"]
        try:
            rk.stop(cid)
        except Exception as e:  # noqa: BLE001
            # Not just Fail: a dead transport can surface as an OSError or the
            # SDK's ConnectError, and the one thing this path must not do is stop
            # unwinding because of it.
            stop_failed.append(key)
            stage_friction(rdir, "stop-failed", f"{key}: {cid}: {type(e).__name__}: {e}")
            print(f"review: {key}: STOP FAILED {type(e).__name__}: {e}", file=sys.stderr)
    state["status"] = "confirmed"
    pr_state.save_state(rdir, state)
    release_review(rdir)
    tail = ("children stopped, run back at confirmed" if not stop_failed else
            f"{len(stop_failed)} child(ren) not stopped ({', '.join(sorted(stop_failed))}), "
            f"run back at confirmed; see the stop-failed friction")
    print(f"review: {type(exc).__name__}: {exc}; {tail}", file=sys.stderr)


def packet_files(rdir):
    """The ordered packet every seat gets, read straight out of files.txt. The
    order is the prefix the provider's prompt cache keys on, so it is passed
    through, never rebuilt here. An entry the fundi pre-fill would glob leaves the
    list for every seat, with the drop recorded in friction: the two delivery
    modes must never disagree about which file an entry means."""
    f = rdir / "packet" / "files.txt"
    try:
        listed = [l.strip() for l in f.read_text().splitlines() if l.strip()]
    except (OSError, UnicodeDecodeError) as e:
        raise Fail(f"{f} not found: gather the packet first: {e}") from e
    if not listed:
        raise Fail(f"{f}: no packet files")
    files = [p for p in listed if not any(c in p for c in FUNDI_GLOB)]
    for p in dict.fromkeys(listed):  # one entry per path, however often it is listed
        if p not in files:
            # gather keeps such paths out of files.txt when it builds the packet;
            # this is the stage's own guard for a packet written any other way (an
            # older run, a hand edit).
            stage_friction(rdir, "packet-drop",
                           f"{p}: contains one of {FUNDI_GLOB!r}, which a fundi pre-fill expands as a "
                           "glob and a claude seat reads literally; left out of every seat's packet",
                           seat="packet")
    if not files:
        raise Fail(f"{f}: every packet path is a fundi pre-fill glob; nothing left to deliver")
    return files


def finder_cells(lenses, groups):
    """One cell per lens, times one per file group when gather split the change:
    a scope narrow enough that one child can cover it in one pass."""
    cells = []
    for lens in lenses:
        name = str(lens.get("name") or "")
        if not name:
            raise Fail("packet/lenses.json: a lens has no name")
        prompt = str(lens.get("prompt") or "")
        if not groups:
            cells.append({"key": f"finder-{slug(name)}", "lens": name, "prompt": prompt,
                          "scope": "the whole change"})
            continue
        for i, g in enumerate(groups, 1):
            gname = str(g.get("name") or f"group-{i}")
            paths = ", ".join(str(p) for p in (g.get("paths") or []))
            cells.append({"key": f"finder-{slug(name)}--{i}-{slug(gname)}", "lens": name, "prompt": prompt,
                          "scope": f"the {gname} group: {paths}"})
    return cells


def panel_jobs(panel, always, mode):
    """One child per panel preset. The checklist is the always-on lenses by
    name: every seat already reads packet/lenses.json, which carries each
    lens's text, so the names are what the checklist has to pin down."""
    checklist = "\n".join(f"- {n}" for n in always)
    instruction = render_prompt("task-pr-panel.md", mode=mode, checklist=checklist,
                                mode_rules=MODE_RULES[mode])
    jobs = []
    for preset in panel:
        key = str(preset).rsplit(":", 1)[-1]
        if any(j["key"] == key for j in jobs):
            raise Fail(f"pr.json panel lists {preset!r} twice; a panel seat is one child")
        jobs.append({"key": key, "seat": preset, "kind": "panel", "lens": None, "always": always,
                     "source": "panel", "instruction": instruction})
    return jobs


def write_jsonl(path, rows):
    with path.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True) + "\n")


def collect_child(rk, adir, rdir, tree, mode, job, info, conv, packet_files):
    """Write one child's directory and return what it pools: findings, checklist
    answers, questions, prior re-judgements, or the parse error that replaced
    them. Nothing here is dropped for looking wrong: a hallucinated reference or
    a breached instruction is recorded in friction and the record still pools,
    because the verifiers, not this stage, decide what is real."""
    cdir = adir / job["key"]
    m = child_metrics(info, conv, set(packet_files))
    m["wall_s"] = round(time.time() - job["spawned_at"], 1)
    m["delivery"] = job["delivery"]
    (cdir / "child.json").write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")
    answer = final_answer(conv) if conv else None
    (cdir / "raw.md").write_text(answer or "")
    rk.stop(job["child_id"])

    out = {"findings": [], "checklist": [], "questions": [], "prior": [], "parse_error": None}
    doc = None
    if answer is None:
        out["parse_error"] = f"no final answer (status {info.get('status')})"
    else:
        try:
            doc = last_json_block(answer)
        except (ValueError, json.JSONDecodeError) as e:
            out["parse_error"] = str(e)
        else:
            if not isinstance(doc, dict):
                out["parse_error"] = "the final json block is not an object"
    if out["parse_error"] is not None:
        (cdir / "parse-error.txt").write_text(out["parse_error"] + "\n")
        friction(rdir, job, "parse-error", out["parse_error"])
        return out
    (cdir / "result.json").write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")

    for note in as_list(doc, "process_notes"):
        friction(rdir, job, "self", str(note))

    # findings[i]: a row the pool cannot carry as a finding. Every skip is
    # recorded: a dropped row must not look like a seat that answered cleanly.
    for i, f in enumerate(as_list(doc, "findings")):
        if not isinstance(f, dict):
            friction(rdir, job, "contract-violation",
                     f"findings[{i}]: {type(f).__name__} is not an object; dropped")
            continue
        # line is normalized to an int on the way into the pool: the schema
        # promises one, and both the reference check and the later clustering
        # int() it, so a model that wrote "a.py:3" there must not crash either.
        rec = {**f, "line": int_line(f), "id": finding_id(f), "raised_by": job["seat"],
               "source": job["source"], "lens": finding_lens(job, f)}
        out["findings"].append(rec)
        where = f"{rec.get('file')}:{rec.get('line')} {rec.get('title')}"
        bad_line = line_contract(f)
        if bad_line:
            friction(rdir, job, "contract-violation", f"{where}: {bad_line}; pooled as {rec['line']}")
        checks = hallucination_checks(tree, rec)
        if checks.get("file_missing"):
            friction(rdir, job, "hallucination", f"{where}: file is not in tree/")
        elif checks.get("line_out_of_range"):
            friction(rdir, job, "hallucination", f"{where}: line is out of range")
    if job["kind"] != "panel":
        return out

    answered = set()
    for i, c in enumerate(as_list(doc, "checklist")):
        if not isinstance(c, dict):
            friction(rdir, job, "contract-violation",
                     f"checklist[{i}]: {type(c).__name__} is not an object; dropped")
            continue
        lens = str(c.get("lens") or "").strip()
        answered.add(lens)
        out["checklist"].append({"lens": lens, "answer": c.get("answer"), "reason": c.get("reason"),
                                 "raised_by": job["seat"]})
        if c.get("answer") == "nothing" and not str(c.get("reason") or "").strip():
            friction(rdir, job, "instruction-breach",
                     f"lens {lens}: answered nothing with no reason")
    for lens in job["always"]:
        if lens not in answered:
            friction(rdir, job, "instruction-breach", f"no checklist answer for lens {lens}")
    if mode == "design":
        for q in as_list(doc, "questions"):
            out["questions"].append({"question": str(q), "raised_by": job["seat"]})
    if mode == "delta":
        for i, p in enumerate(as_list(doc, "prior")):
            if not isinstance(p, dict):
                friction(rdir, job, "contract-violation",
                         f"prior[{i}]: {type(p).__name__} is not an object; dropped")
                continue
            out["prior"].append({**p, "raised_by": job["seat"]})
    return out


def friction(rdir, job, kind, note):
    """One friction entry per event, naming the seat and the cell: a finder's
    instruction is the same across cells, so only the cell tells them apart."""
    pr_state.append_friction(rdir, {"seat": job["seat"], "kind": kind, "note": note, "cell": job["key"]})


def unverified_lenses(rdir, jobs, records):
    """A panel seat that produced no answer leaves each always-on lens with no
    claim at all, which must not read as a seat that answered every lens: every
    such lens is recorded as a breach naming it."""
    for key in sorted(jobs):
        job = jobs[key]
        if job["kind"] != "panel":
            continue
        rec = records.get(key)
        if rec is not None and rec["parse_error"] is None:
            continue  # a seat that answered: its own checklist decides
        for lens in job["always"]:
            friction(rdir, job, "instruction-breach",
                     f"no checklist answer for lens {lens}: the seat produced no answer")


@pr_state.friction_stage("review")
def stage_review(rk, home, args):
    rdir, run = home.load_run(args.run)
    mode = run.get("mode")
    if mode not in MODE_RULES:
        raise Fail(f"run mode {mode!r} is not one of {sorted(MODE_RULES)}")

    files = packet_files(rdir)
    prj = read_json(rdir / "project" / "pr.json", "the run's seat config (start the run again)")
    ledoc = read_json(rdir / "packet" / "lenses.json", "gather the packet first")
    lenses = ledoc.get("lenses")
    if not isinstance(lenses, list) or not lenses:
        raise Fail(f"{rdir / 'packet' / 'lenses.json'}: no lenses")
    ctx = rdir / "packet" / "context.json"
    # gather writes an empty context when its own child fails to parse, so an
    # absent file means the same thing: no groups, every cell whole-change.
    groups = (read_json(ctx, "gather the packet first").get("file_groups") or []) if ctx.exists() else []
    if not isinstance(groups, list) or not all(isinstance(g, dict) for g in groups):
        raise Fail(f"{ctx}: file_groups must be a list of objects")
    # The panel's checklist is the always-on lenses by name; a lens with no name
    # is refused in finder_cells below, so nothing is dropped silently here.
    always = [str(l["name"]) for l in lenses if l.get("always") and l.get("name")]
    finder = prj.get("finder")
    if not finder:
        raise Fail(f"{rdir / 'project' / 'pr.json'}: no finder seat")
    panel = prj.get("panel") or []
    if not panel:
        raise Fail(f"{rdir / 'project' / 'pr.json'}: no panel seats")

    jobs = {}
    fan = fanout(prj, str(rdir / "project" / "pr.json"))
    for c in finder_cells(finder_lenses(lenses, fan), groups if fan["split_groups"] else []):
        jobs[c["key"]] = {"key": c["key"], "seat": finder, "kind": "finder", "lens": c["lens"],
                          "always": always, "source": f"finder:{c['lens']}",
                          "instruction": render_prompt("task-pr-find.md", mode=mode, scope=c["scope"],
                                                       lens_name=c["lens"], lens_prompt=c["prompt"])}
    for j in panel_jobs(panel, always, mode):
        if j["key"] in jobs:
            raise Fail(f"review child {j['key']!r} is taken; rename the seat")
        jobs[j["key"]] = j

    # Everything the stage reads is in place, so this is the point of no return:
    # claim the run for review (before any attempt dir exists, so a refused
    # invocation leaves no trace), then read the status while holding the claim.
    # The claim is the atomic step (O_EXCL), so a second invocation that read
    # `confirmed` before this one moved the status cannot also claim the run, and
    # the status this stage saves is the one it read under its claim: an
    # invocation that starts after a review finished sees `verifying` and
    # refuses instead of running the review a second time.
    claim_review(rdir)
    try:
        state = pr_state.load_state(rdir)
        if state.get("status") != "confirmed":
            raise Fail(f"run is {state.get('status')}; confirm it first")
        adir = next_attempt(rdir / "review")
        pr_state.friction_attempt(adir.name)
        state["status"] = "reviewing"
        pr_state.save_state(rdir, state)
    except BaseException:
        # Nothing has spawned yet and the run has not been claimed in its status:
        # give the claim back rather than leave the run stuck behind a lock held
        # by a stage that never started.
        release_review(rdir)
        raise
    timeout = getattr(args, "timeout", DEFAULT_TIMEOUT)
    model = getattr(args, "model", None)
    tree = run_cwd(run)
    spawned = {}

    def save_children():
        (adir / "children.json").write_text(json.dumps(spawned, indent=2, sort_keys=True) + "\n")

    def spawn(job):
        cdir = adir / job["key"]
        cdir.mkdir(exist_ok=True)
        (cdir / "task.md").write_text(job["instruction"])
        labels = {"review/run": run["run_id"], "review/stage": "review", "review/attempt": adir.name,
                  "review/seat": job["seat"], "review/cell": job["key"], **model_labels(model)}
        if job["lens"]:
            labels["review/lens"] = job["lens"]
        try:
            # cwd is the run directory: every packet path is relative to it
            # (packet/... and tree/...), on the pre-fill and the read-list path
            # alike. The seat's own kind decides which of the two it gets. The
            # seat root is the run directory, and the answer key is deliberately
            # not in it: gather's project/ snapshot carries no gold set.
            cid, sid, delivery = rk.spawn_seat(f"review-{adir.name}-{job['key']}"[:120], preset=job["seat"],
                                               cwd=rdir, packet_files=files, instruction=job["instruction"],
                                               labels=labels, model=model)
        except AtCap:
            return AT_CAP
        except Fail as e:
            # A spawn that failed for any reason other than the cap is not
            # retried: the seat is wrong, so the same call would fail again.
            (cdir / "spawn-error.txt").write_text(f"{e}\n")
            friction(rdir, job, "spawn-error", str(e))
            job["done"], job["error"] = True, str(e)
            print(f"  {job['key']}: SPAWN FAILED {e}")
            return None
        job.update({"child_id": cid, "conversation_id": sid, "delivery": delivery, "spawned_at": time.time()})
        spawned[job["key"]] = {"child_id": cid, "conversation_id": sid, "delivery": delivery}
        save_children()
        return cid

    def run_children():
        """Every child of this attempt: spawn up to the cap, collect whoever has
        settled. A spawn refused by the cap is requeued, never counted."""
        queue, pending, records = list(jobs), {}, {}
        while queue or pending:
            while queue and len(pending) < args.parallel:
                key = queue.pop(0)
                cid = spawn(jobs[key])
                if cid is AT_CAP:
                    if not pending:
                        raise Fail("spawn refused at the subtree's live-children cap with nothing of ours "
                                   "running; launch grants max_children, or stop what holds the slots")
                    queue.insert(0, key)
                    break  # wait for a running child to settle and free a slot
                if cid:
                    pending[cid] = jobs[key]
            for cid in list(pending):
                job = pending[cid]
                got = settled(rk, cid, timed_out=time.time() - job["spawned_at"] > timeout)
                if got is None:
                    continue
                records[job["key"]] = collect_child(rk, adir, rdir, tree, mode, job, *got, files)
                del pending[cid]
                job["done"] = True
                print(f"  {job['key']}: {len(records[job['key']]['findings'])} findings"
                      f"{', PARSE ERROR' if records[job['key']]['parse_error'] else ''}")
            if pending:
                time.sleep(3)
        if not spawned:
            raise Fail(f"no review child started; see {adir}/spawn-error.txt and the daemon log")
        return records

    try:
        records = run_children()
        unverified_lenses(rdir, jobs, records)
        pooled = {"findings": [], "checklist": [], "questions": [], "prior": []}
        for key in sorted(records):
            for field in pooled:
                pooled[field] += records[key][field]
        write_jsonl(adir / "candidates.jsonl", pooled["findings"])
        write_jsonl(adir / "checklist.jsonl", pooled["checklist"])
        if mode == "design":
            write_jsonl(adir / "questions.jsonl", pooled["questions"])
        if mode == "delta":
            write_jsonl(adir / "prior.jsonl", pooled["prior"])
        state["status"] = "verifying"
        pr_state.save_state(rdir, state)
    except BaseException as e:
        # A refusal, a bug or a Ctrl-C: all of them leave a running review behind
        # if we walk away, so all of them go through the same unwind.
        abandon_review(rk, rdir, state, spawned, e)
        raise
    release_review(rdir)
    errs = sum(1 for r in records.values() if r["parse_error"])
    print(f"review {adir.name}: {len(spawned)} children, {len(pooled['findings'])} candidates, "
          f"{len(pooled['checklist'])} checklist answers, {errs} parse failures")
    print(f"inspect: {adir}/candidates.jsonl and {adir}/<cell>/")
    return {"stage": "review", "run": run["run_id"], "attempt": adir.name,
            "children": len(spawned), "candidates": len(pooled["findings"]), "parse_failures": errs}
