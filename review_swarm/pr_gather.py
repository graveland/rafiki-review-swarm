# review-swarm: the gather stage - build the review packet once, estimate what
# running it costs, and stop at awaiting-confirm when the operator has to decide
import hashlib
import json
import os
from fnmatch import fnmatchcase
from pathlib import Path

from . import analysis, pr_state, presets
from . import lenses as lenses_mod
from .common import (
    LENSES,
    Fail,
    fanout,
    finder_lenses,
    git,
    last_json_block,
    now_iso,
    render_prompt,
    run,
)
from .rafiki_io import final_answer, prefill_read
from .runs import with_tools
from .stages import wait_settled

GATHER_TIMEOUT = 1200       # one seat, one answer
CONTEXT_BUDGET_TOKENS = 20_000   # what the gather seat may spend on context ranges
CONTEXT_FRACTION = 0.6      # share of a seat's window the packet may take before it blocks
CLAIM = ".gather.lock"      # this stage's in-progress marker on disk

# A path with any of these is a glob to the daemon's pre-fill reader, which runs
# the glob tool and refuses the child when it matches nothing (rafiki
# pkg/prefill: IsGlob is a plain ContainsAny and has no escape, and a glob
# cannot carry a line range). So a seat that is pre-filled and a seat that reads
# the list itself cannot be given the same path, and the producer keeps such
# paths out of the packet instead.
GLOB_CHARS = "*?[{"

_WINDOW_KEYS = ("context_window", "contextWindow", "context_length", "context")
_MODEL_KEYS = ("id", "model", "name")


def has_glob(path):
    return any(c in str(path) for c in GLOB_CHARS)


def is_review_config(path):
    """<target>/.review/ is this tool's own config location, committed into some
    targets: it is never part of the change under review."""
    p = str(path)
    return p == ".review" or p.startswith(".review/")


def review_config_paths(run):
    """The paths under the target's .review/ inside the pinned range, sorted. The
    diff comes out of `git diff`, a second door into the packet beside the frozen
    trees, so the exclusion there is reported from here rather than silent. A
    target without one answers nothing."""
    names = git(run["target"], "diff", "--name-only", run["base"], run["head"], "--", ".review")
    return sorted(l for l in names.splitlines() if l)


def _field(row, keys):
    for k in keys:
        v = row.get(k) if isinstance(row, dict) else getattr(row, k, None)
        if v is not None:
            return v
    return None


def sdk_windows(rk):
    """{model: context window} from the SDK's model catalog, when the SDK has
    one (the pinned one does not, so this is {})."""
    fn = getattr(rk.c, "models", None)
    if not callable(fn):
        return {}
    rows = fn() or []
    out = {}
    # A catalog keyed by model id carries it in the key, a row list in the row.
    for key, row in (rows.items() if isinstance(rows, dict) else [(None, r) for r in rows]):
        model, window = _field(row, _MODEL_KEYS) or key, _field(row, _WINDOW_KEYS)
        if isinstance(model, str) and isinstance(window, int) and window > 0:
            out[model] = window
    return out


def pr_json(rdir):
    """The run's seat config. Validated here rather than at each use: a missing
    seat key would otherwise spawn a hard-coded seat name or silently drop that
    seat from the estimate."""
    f = rdir / "project" / "pr.json"
    if not f.exists():
        raise Fail(f"no PR seat config at {f}")
    try:
        doc = json.loads(f.read_text())
    except json.JSONDecodeError as e:
        raise Fail(f"{f}: {e}") from e
    for key in ("gather", "finder", "panel", "verifiers"):
        if not doc.get(key):
            raise Fail(f"{f}: no {key} seat configured")
    excl = doc.get("packet_exclude", [])
    if not isinstance(excl, list) or not all(isinstance(g, str) for g in excl):
        raise Fail(f"{f}: packet_exclude must be a list of glob strings, got {excl!r}")
    return doc


def run_seats(pjson):
    """Every seat this run will spawn, in a fixed order: gather, the finder, the
    panel as listed, then the verifiers sorted by the panel seat they serve."""
    names = [pjson["gather"], pjson["finder"], *pjson["panel"]]
    names += [pjson["verifiers"][p] for p in sorted(pjson["verifiers"])]
    return list(dict.fromkeys(names))


def record_friction(rdir, entry):
    """A friction entry that must never replace an error already in flight. The
    unwind and the claim-refusal paths run on the way out, and what they record
    is about the very failure they are unwinding, so a failure to write it is
    reported and swallowed rather than masking the original."""
    try:
        pr_state.append_friction(rdir, entry)
    except Fail as e:
        print(f"gather: could not record {entry.get('kind')} friction: {e}")


def claim_holder(path):
    """Who holds the claim, for the refusal message. An unreadable marker is
    reported as such rather than raised: the refusal is what matters."""
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return f"marker unreadable: {e}"
    return f"pid {doc.get('pid')} since {doc.get('at')}"


def claim_gather(rdir, run):
    """Take this run for a gather, and record that it is taken. Two concurrent
    gathers would both pass the status guard and both rewrite packet/, so the
    claim file is created with O_EXCL: the loser gets a Fail naming the run and
    the holder. The status is then read and checked under the claim, never before
    it: a state another invocation wrote while this one waited for the claim must
    not be saved back over by a stale dict. A refusal gives the claim back. The
    claim is mirrored into state.json, where an operator can see it. Returns the
    claim file to release."""
    path = rdir / CLAIM
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        raise Fail(f"run {run['run_id']}: a gather is already in progress "
                   f"({claim_holder(path)}). Wait for it; if that process is gone, remove {path}") from None
    except OSError as e:
        raise Fail(f"run {run['run_id']}: cannot take the gather claim at {path}: {e}") from e
    at = now_iso()
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"stage": "gather", "pid": os.getpid(), "at": at}, sort_keys=True) + "\n")
    try:
        state = pr_state.load_state(rdir)
        if state.get("status") != "gathering":
            raise Fail(f"run {run['run_id']} is {state.get('status')}; a packet is only built while a run is "
                       f"gathering. Confirm or abort this run, or start a new one: packet/ is the packet its "
                       f"review and verify attempts were run against, so it is never rewritten")
        state["in_progress"] = {"stage": "gather", "pid": os.getpid(), "at": at}
        pr_state.save_state(rdir, state)
    except Fail:
        # The claim is ours and we are declining it, or the save that mirrors it
        # into state.json failed: give it back either way, so a failure here
        # never strands the run for every later invocation. The error itself is
        # re-raised, so the caller still sees what went wrong.
        release_gather(rdir, path)
        raise
    return path


def release_gather(rdir, path):
    """Give the claim back. Best effort: it runs in a finally (and from the
    refusal path), and a failure here must not replace the error that brought us
    here. An unlink that fails for any other reason is recorded, not raised: the
    claim file is then still on disk, and the next invocation's refusal names it."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        record_friction(rdir, {"kind": "claim-release-failed",
                               "note": f"{path}: {e}; the claim is still on disk and the next gather is "
                                       f"refused until it is removed"})
    try:
        state = pr_state.load_state(rdir)
        if state.get("in_progress"):
            del state["in_progress"]
            pr_state.save_state(rdir, state)
    except Fail as e:
        print(f"gather: could not clear the in-progress marker ({e})")


def seat_presets(rk, pjson):
    """{name: {version, kind, model, max_cost, context_window}} from the daemon,
    so the packet records the seat policy it was built against. The kind rule is
    seat_kind's: an absent kind is a fundi seat. A preset with no max_cost stays
    null: the seat has no cap, which is not the same as a cap of zero, and a
    preset that carries no window stays null for the same reason."""
    out = {}
    for name in run_seats(pjson):
        live = presets.live_seat(rk, name)
        if live is None:
            raise Fail(f"preset {name} not found")
        spec = (live.get("spec") or live) if isinstance(live, dict) else live
        # `get` is called in this same iteration, so closing over the loop's `spec`
        # cannot go stale; the noqa is narrow to B023.
        get = spec.get if isinstance(spec, dict) else lambda k: getattr(spec, k, None)  # noqa: B023
        out[name] = {
            "version": live.get("version") if isinstance(live, dict) else getattr(live, "version", None),
            "kind": get("kind") or "fundi",
            "model": get("model"),
            "max_cost": get("max_cost"),
            "context_window": _field(spec, _WINDOW_KEYS),
        }
    return out


def gh_json(*args):
    """One `gh api` call, parsed. Its own function because the threads section is
    the only part of the packet that comes from the network, and a test drives
    both its answers and its failures without a gh."""
    p = run(["gh", *args])
    try:
        return json.loads(p.stdout or "[]")
    except json.JSONDecodeError as e:
        raise Fail(f"gh {' '.join(map(str, args))}: {e}") from e


def as_int(v):
    """A line number, or None for "not reported": a comment with no line is not a
    comment on line 0."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def is_bot(row):
    user = row.get("user") or {}
    return user.get("type") == "Bot" or str(user.get("login") or "").endswith("[bot]")


def latest_bot_comments_only(rows):
    """A bot's issue comments are status reports that each push supersedes (a
    coverage report per CI run), so only its newest one is kept: nineteen
    copies of a repo-wide coverage table made a 7K-line PR's packet 1.2M
    tokens, 80% of it threads.md. A bot's line comments on the diff are
    findings, not reports, and are left alone by only applying this to the
    issue-comments API."""
    newest = {}
    for i, row in enumerate(rows):
        if isinstance(row, dict) and is_bot(row):
            login = str((row.get("user") or {}).get("login") or "?")
            key = (str(row.get("created_at") or ""), as_int(row.get("id")) or 0, i)
            if login not in newest or key > newest[login][0]:
                newest[login] = (key, i)
    keep = {i for _, i in newest.values()}
    return [row for i, row in enumerate(rows)
            if not (isinstance(row, dict) and is_bot(row)) or i in keep]


def threads_text(pr, repo):
    """Review comments and issue comments as `## <path>:<line> <user>` blocks.
    Rows are sorted within each API's list so the file does not depend on the
    order the API happened to return. Every page is read: `--paginate --slurp`
    collects one array per page, so the file is the whole thread, never the
    first 30 rows of it, except that a bot's issue comments collapse to its
    newest (latest_bot_comments_only). Returns (text, errors): a failed API call, or a comment
    whose line cannot be read, is reported and the block is written without a
    line rather than at line 0."""
    if not (pr and repo):
        return "", []
    rows_out, errors = [], []
    for api in (f"repos/{repo}/pulls/{pr}/comments", f"repos/{repo}/issues/{pr}/comments"):
        try:
            rows = gh_json("api", api, "--paginate", "--slurp")
        except Fail as e:
            errors.append(f"{api}: {e}")
            continue
        # `--slurp` wraps each page in its own array, so the rows are the
        # concatenation of the pages; a single page flattens to itself. A shape
        # that is neither (the tests' fake, a hand-fed answer) is taken as the
        # rows it is.
        if rows and all(isinstance(page, list) for page in rows):
            rows = [row for page in rows for row in page]
        if not isinstance(rows, list):
            errors.append(f"{api}: expected a list of comments, got {type(rows).__name__}")
            continue
        if "/issues/" in api:
            rows = latest_bot_comments_only(rows)
        blocks = []
        for row in rows:
            if not isinstance(row, dict):
                errors.append(f"{api}: comment is not an object: {row!r}")
                continue
            path = str(row.get("path") or "")
            line = as_int(row.get("line") if row.get("line") is not None else row.get("original_line"))
            if not path:
                where = "(issue comment)"
            elif line is None:
                where = path
                errors.append(f"{api}: comment on {path} reports no line: {row.get('line')!r}")
            else:
                where = f"{path}:{line}"
            blocks.append((path, -1 if line is None else line, where,
                           str((row.get("user") or {}).get("login") or "?"),
                           str(row.get("body") or "").strip()))
        blocks.sort(key=lambda b: (b[0], b[1], b[3], b[4]))
        rows_out += [f"## {where} {user}\n{body}\n" for _, _, where, user, body in blocks]
    return "\n".join(rows_out), errors


def gold_slugs(pdir):
    """Every bug slug the project's gold sets name, so a lens that claims to
    catch one can be checked against a slug that exists."""
    out = set()
    for f in sorted((pdir / "gold").glob("*.jsonl")):
        for n, line in enumerate(f.open(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise Fail(f"{f}:{n}: {e}") from e
            if not row.get("bug"):
                raise Fail(f"{f}:{n}: gold entry has no bug slug")
            out.add(row["bug"])
    return out


def prior_members(row):
    """The distinct issues a verdict bucket holds. A row written before members
    existed still carries one issue: its representative's fields stand in for it,
    exactly as the run report reads such a row."""
    members = [m for m in (row.get("members") or []) if isinstance(m, dict)]
    if members:
        return members
    return [{"id": row.get("id") or row.get("key"), "verdict": row.get("verdict"),
             "title": row.get("title"), "file": row.get("file"), "line": row.get("line"),
             "claim": row.get("claim")}]


def since_run_dir(home, run):
    """The since run's directory, or a Fail naming why it cannot be a delta's
    prior. A delta inherits a prior only from a run that is there, is done and is
    this project's: an unfinished run, or one of another project, would put
    verdicts from a different review into this packet. The failure is loud, never
    a silently empty prior."""
    since = run.get("since_run")
    if not since:
        raise Fail("delta run has no since_run to take prior verdicts from")
    sdir, srun = home.load_run(since)
    status = pr_state.load_state(sdir).get("status")
    if status != "done":
        raise Fail(f"since run {since} ({sdir}) is {status!r}, not 'done': delta takes prior verdicts "
                   f"only from a finished run, never an empty prior from one still running")
    project = (run.get("project") or {}).get("name") if isinstance(run.get("project"), dict) else None
    sproject = (srun.get("project") or {}).get("name") if isinstance(srun.get("project"), dict) else None
    if not project or sproject != project:
        raise Fail(f"since run {since} ({sdir}) is {sproject or 'no project'}, not {project or 'no project'}: "
                   f"a delta compares one project's change, so another project's verdicts are not its prior")
    return sdir


def latest_verdicts_file(sdir):
    """The since run's newest verify attempt's verdicts.jsonl: the attempt the
    report reads, so the one a delta inherits. Merging every attempt would carry
    a finding twice under two outcomes, so only the newest is read. A done run
    was verified, so no verdicts at all is a broken record, not an empty prior."""
    attempts = sorted((p for p in (sdir / "verify").glob("*")
                       if p.is_dir() and p.name.isdigit() and (p / "verdicts.jsonl").exists()),
                      key=lambda p: int(p.name))
    if not attempts:
        raise Fail(f"since run has no verify attempt with verdicts.jsonl under {sdir / 'verify'}: "
                   f"there is no prior to carry")
    return attempts[-1] / "verdicts.jsonl"


def prior_rows(home, run):
    """The delta packet's prior.json: every issue the since run's verifiers
    confirmed or contested, so this review does not re-litigate the settled
    parts.

    An issue is selected by its OWN verdict, never its bucket's: a bucket is one
    row but holds several distinct issues, so a confirmed issue sharing a bucket
    with a refuted sibling is still prior and the refuted one is not. Each row
    also carries the issue's disposition: the triage outcome a human recorded in
    the since run's grading sheet (agree / disagree / need-info; see
    `analysis.set_disposition`), keyed the way the run report and compare key a
    verdict (`analysis.issue_key`), and null while none is recorded. A calibration
    grade is not a disposition: a grade says whether the bot was right, which is a
    different question from what was decided about the finding. A since run that is
    missing, is not done, or belongs to another project is a Fail naming which (see
    `since_run_dir`)."""
    sdir = since_run_dir(home, run)
    vfile = latest_verdicts_file(sdir)
    dispositions = analysis.dispositions_by_issue(sdir)
    rows = []
    for n, line in enumerate(vfile.open(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise Fail(f"{vfile}:{n}: {e}") from e
        if not isinstance(row, dict):
            raise Fail(f"{vfile}:{n}: a verdict row must be an object, got {type(row).__name__}")
        for m in prior_members(row):
            verdict = str(m.get("verdict") or "")
            if verdict not in ("confirmed", "contested"):
                continue
            issue = {"id": m.get("id") or row.get("key"),
                     **{k: m.get(k) for k in ("title", "file", "line", "claim")},
                     "verdict": verdict}
            issue["disposition"] = dispositions.get(analysis.issue_key(issue), "") or None
            rows.append(issue)
    # Sorted for byte-stability. The line here is an ordering key only: a row
    # with no line sorts first rather than being pinned to line 0.
    rows.sort(key=lambda r: (str(r.get("file")), as_int(r.get("line")) or -1, str(r.get("id"))))
    return rows


def range_line(entry):
    """One context entry as a packet line: `tree/<path>:<start>-<end>` when the
    gather seat gave a range the pre-fill syntax can carry, else the whole file."""
    path = str(entry["path"]).removeprefix("./")
    start, end = entry.get("start"), entry.get("end")
    if (isinstance(start, int) and isinstance(end, int) and not isinstance(start, bool)
            and 1 <= start <= end):
        return f"tree/{path}:{start}-{end}"
    return f"tree/{path}"


def entry_size(rdir, entry):
    """(bytes, tokens) for one packet entry, measured on exactly what the seat is
    told to read: the same `path`/`path:a-b` syntax rafiki_io parses."""
    spec = prefill_read(entry)
    f = rdir / spec["path"]
    try:
        raw = f.read_bytes()
    except OSError as e:
        raise Fail(f"packet entry {entry}: {e}") from e
    if "start" not in spec:
        n = len(raw)
    else:
        lines = raw.decode("utf-8", errors="replace").splitlines()[spec["start"] - 1:spec["end"]]
        n = len(("\n".join(lines) + "\n").encode())
    return n, n // 4


def list_field(rdir, seat, doc, key):
    """doc[key] as a list. A shape that is not a list at all (a scalar, a mapping)
    is recorded and treated as empty, exactly like a malformed element: the seat has
    already been paid, so a bad answer shrinks the packet and is named in friction
    and never raises a TypeError or a KeyError out of the stage."""
    v = doc.get(key)
    if v is None:
        return []
    if isinstance(v, list):
        return v
    pr_state.append_friction(rdir, {"seat": seat, "kind": "packet-drop",
                                    "note": f"{key} is not a list but {type(v).__name__}: {v!r}; "
                                            f"the answer is treated as having no {key}"})
    return []


def gather_context(rk, rdir, run, packet_files, instruction, seat):
    """One gather seat, one answer. A seat that never spawns, never answers, or
    answers without a JSON block leaves an empty context and a friction entry:
    the run continues with a smaller packet rather than dying here. Every row
    the seat sent that cannot become a packet line is friction too, so a smaller
    packet is never silent."""
    labels = {"review/run": run["run_id"], "review/stage": "gather", "review/seat": seat}
    try:
        cid, _, delivery = rk.spawn_seat(f"gather-{run['run_id']}", preset=seat, cwd=rdir,
                                         packet_files=packet_files, instruction=instruction,
                                         labels=labels)
    except Fail as e:
        pr_state.append_friction(rdir, {"seat": seat, "kind": "spawn-error", "note": str(e)})
        print(f"gather: SPAWN FAILED {e}")
        return {"context": [], "file_groups": []}, []
    print(f"gather: {cid} ({delivery})")
    info, conv = wait_settled(rk, [cid], timeout=GATHER_TIMEOUT)[cid]
    rk.stop(cid)
    answer = final_answer(conv) if conv else None
    try:
        if answer is None:
            raise ValueError(f"no final answer (status {info.get('status')})")
        doc = last_json_block(answer)
        if not isinstance(doc, dict):
            # ValueError, not TypeError: the handler below catches it as a parse
            # error, which is the intent (a TypeError would escape it).
            raise ValueError("answer is not a JSON object")  # noqa: TRY004
    except (ValueError, json.JSONDecodeError) as e:
        pr_state.append_friction(rdir, {"seat": seat, "kind": "parse-error", "note": str(e)})
        print(f"gather: PARSE ERROR {e}; continuing with an empty context")
        return {"context": [], "file_groups": []}, []
    context = []
    for i, c in enumerate(list_field(rdir, seat, doc, "context")):
        if not isinstance(c, dict):
            pr_state.append_friction(rdir, {"seat": seat, "kind": "packet-drop",
                                            "note": f"context[{i}] is not an object: {c!r}"})
            continue
        if not c.get("path"):
            pr_state.append_friction(rdir, {"seat": seat, "kind": "packet-drop",
                                            "note": f"context[{i}] has no path: {c!r}"})
            continue
        start, end = c.get("start"), c.get("end")
        context.append({"path": str(c["path"]).removeprefix("./"),
                        "start": start if isinstance(start, int) and not isinstance(start, bool) else None,
                        "end": end if isinstance(end, int) and not isinstance(end, bool) else None,
                        "why": str(c.get("why") or "")})
    return ({"context": context, "file_groups": list_field(rdir, seat, doc, "file_groups")},
            [str(n) for n in list_field(rdir, seat, doc, "process_notes")])


def build_packet(rk, home, rdir, *, lens_generic):
    """Write packet/ in the fixed order the seats depend on, and return the
    manifest. Byte-identical for identical inputs: nothing here reads a clock,
    a network answer that is not sorted, or an unordered mapping.

    Refuses to write unless the run is still gathering: packet/ is the record
    every finished attempt points at, so a done or aborted run is never
    rebuilt in place. A new packet needs a new run."""
    rdir = Path(rdir)
    run = json.loads((rdir / "run.json").read_text())
    state = pr_state.load_state(rdir)
    if state.get("status") != "gathering":
        raise Fail(f"run {run['run_id']} is {state.get('status')}; a packet is only built while a run is "
                   f"gathering. Confirm or abort this run, or start a new one: packet/ is the packet its "
                   f"review and verify attempts were run against, so it is never rewritten")
    pjson = pr_json(rdir)
    mode = run["mode"]
    packet = rdir / "packet"
    packet.mkdir(exist_ok=True)

    if mode == "design":
        intent = Path(run["doc"]).read_text()
    else:
        intent = f"# {run.get('title') or ''}\n\n{run.get('body') or ''}".rstrip() + "\n"
    (packet / "intent.md").write_text(intent)

    diff = ""
    if mode in ("pr", "delta"):
        # `.review/` is this tool's own config, and a gold/ under it is the
        # calibration answer key. The frozen trees never carry it, but the diff is
        # a second door into the packet: without the exclusion a committed
        # .review/gold/*.jsonl reaches every seat through the patch, and the lens
        # triggers are matched against it too. The repo root is the include so the
        # exclusion is stated against the whole range, not just a subdirectory.
        diff = git(run["target"], "diff", "--no-color", run["base"], run["head"],
                   "--", ".", ":(exclude).review")
        (packet / "diff.patch").write_text(diff)
        for p in review_config_paths(run):
            pr_state.append_friction(rdir, {"kind": "review-config-drop",
                                            "note": f"{p} is under the target's .review/, this tool's own "
                                                    f"config; left out of packet/diff.patch"})

    threads, gh_errors = threads_text(run.get("pr"), run.get("repo")) if mode in ("pr", "delta") else ("", [])
    (packet / "threads.md").write_text(threads)
    for note in gh_errors:
        pr_state.append_friction(rdir, {"kind": "gh-error", "note": note})

    if mode == "delta":
        (packet / "prior.json").write_text(
            json.dumps({"verdicts": prior_rows(home, run)}, indent=1, sort_keys=True) + "\n")

    lens_dir = pjson.get("lens_dir")
    # The gold sets stay in the project, not in the run: the snapshot is readable
    # by every seat (cwd is the run directory), and gold is the calibration
    # answer key. `catches` is validated against the slugs a repo lens claims.
    project_src = home.project_dir(run["project"]["name"], run["target"])
    built = lenses_mod.build(lens_generic, Path(lens_dir).expanduser() if lens_dir else None,
                             gold_slugs(project_src), rdir / "project", home_dir=home.lens_dir)
    selected = lenses_mod.select(built, mode, sorted(run["files_changed"]), diff)
    (packet / "lenses.json").write_text(json.dumps({"lenses": [
        {"name": l.name, "category": l.category, "prompt": l.prompt, "always": l.triggers == "always"}
        for l in selected]}, indent=1, sort_keys=True) + "\n")

    # What may go into the packet, and what each refusal is recorded as. A path
    # with a glob character cannot be pre-filled (the daemon reads it as a glob
    # and refuses the child when it matches nothing) while a seat reading the
    # list itself would open it literally, so the two delivery modes would
    # diverge: such a path never reaches files.txt. A file the head tree does
    # not have cannot be pre-filled either (the deletion itself is in the diff),
    # and .review/ is this tool's config, not the change under review.
    changed = []
    for p in run["files_changed"]:
        if is_review_config(p):
            continue   # reported by pr_run_new, which dropped it from the run
        if has_glob(p):
            pr_state.append_friction(rdir, {"kind": "packet-drop",
                                            "note": f"{p}: a path with {GLOB_CHARS!r} is a glob to the "
                                                    f"pre-fill reader and a literal path to a seat that reads "
                                                    f"the list; left out of the packet"})
            continue
        # A generated file can be most of a packet while its change is a few diff
        # lines, which the diff already carries; the whole file only crowds the
        # change out of a seat's window. A gather range into it still reaches the
        # packet, since the seat asked for that part on purpose.
        excluded = next((g for g in pjson.get("packet_exclude", []) if fnmatchcase(p, g)), None)
        if excluded:
            pr_state.append_friction(rdir, {"kind": "packet-drop",
                                            "note": f"{p}: matches pr.json packet_exclude {excluded!r}; "
                                                    f"the whole file is left out of the packet, its change "
                                                    f"is in packet/diff.patch"})
            continue
        if (rdir / "tree" / p).is_file():
            changed.append(p)
    gather_files = ["packet/intent.md"] + (["packet/diff.patch"] if mode in ("pr", "delta") else []) \
        + [f"tree/{p}" for p in changed]
    instruction = with_tools(rdir, render_prompt(
        "task-pr-gather.md", intent=intent,
        changed_files="\n".join(run["files_changed"]) or "(no changed files in this run)",
        budget_tokens=str(CONTEXT_BUDGET_TOKENS)))
    seat = pjson["gather"]
    context, notes = gather_context(rk, rdir, run, gather_files, instruction, seat)
    # A group that is not {name, paths} would meet a downstream `g.get` and crash
    # review, so a malformed answer is treated as having no groups at all, with
    # friction naming what arrived (never a Fail: the seat has already been paid).
    malformed = [i for i, g in enumerate(context["file_groups"])
                 if not isinstance(g, dict) or not isinstance(g.get("name"), str)
                 or not isinstance(g.get("paths"), list)
                 or not all(isinstance(p, str) for p in g.get("paths") or [])]
    for i in malformed:
        pr_state.append_friction(rdir, {"seat": seat, "kind": "packet-drop",
                                        "note": f"file_groups[{i}] is not {{name, paths}}: "
                                                f"{context['file_groups'][i]!r}; the answer is treated as "
                                                f"having no file_groups"})
    if malformed:
        context["file_groups"] = []
    (packet / "context.json").write_text(json.dumps(context, indent=1, sort_keys=True) + "\n")
    for note in notes:
        pr_state.append_friction(rdir, {"seat": seat, "kind": "self", "note": note})

    entries = ["packet/intent.md"]
    if mode in ("pr", "delta"):
        entries.append("packet/diff.patch")
    entries.append("packet/threads.md")
    if mode == "delta":
        entries.append("packet/prior.json")
    entries.append("packet/lenses.json")
    entries += [f"tree/{p}" for p in changed]
    for c in context["context"]:
        line = range_line(c)
        # The .review/ exclusion is checked on the context entry's own
        # repo-relative path, not the `tree/`-prefixed packet line: the prefix
        # would hide the dot-directory from the guard.
        if is_review_config(c["path"]):
            continue   # reported by pr_run_new, which dropped it from the tree
        path = prefill_read(line)["path"]
        if has_glob(path):
            pr_state.append_friction(rdir, {"seat": seat, "kind": "packet-drop",
                                            "note": f"context {line}: a path with {GLOB_CHARS!r} is a glob to "
                                                    f"the pre-fill reader and a literal path to a seat that "
                                                    f"reads the list; left out of the packet"})
            continue
        if (rdir / path).is_file():
            entries.append(line)
        else:
            # The gather seat's own context choice, dropped: friction, so a
            # smaller packet than the seat asked for is auditable.
            pr_state.append_friction(rdir, {"seat": seat, "kind": "packet-drop",
                                            "note": f"context {line} is not in the frozen tree; "
                                                    f"left out of the packet"})
    # This list is the byte-identical prefix every review seat shares; nothing
    # else may reorder it.
    (packet / "files.txt").write_text("\n".join(entries) + "\n")

    sized = []
    for e in entries:
        n, tokens = entry_size(rdir, e)
        sized.append({"path": e, "bytes": n, "tokens": tokens})
    manifest = {
        "entries": sized,
        "total_tokens": sum(s["tokens"] for s in sized),
        "mode": mode,
        "target": run["target"], "base": run.get("base"), "head": run["head"],
        "lenses_manifest_sha256": hashlib.sha256(
            (rdir / "project" / "lenses.manifest.json").read_bytes()).hexdigest(),
        "lenses": [{"name": l.name, "sha256": l.sha256, "always": l.triggers == "always"} for l in selected],
        "seats": seat_presets(rk, pjson),
    }
    (packet / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    print(f"packet: {len(entries)} entries, {manifest['total_tokens']} tokens at {packet}")
    return manifest


def estimate(rk, rdir, manifest):
    """What the packet costs per seat kind, and whether it fits. Dollars are an
    upper bound from every fundi seat's max_cost; claude-kind seats report the
    token volume they will read instead, since they are not OpenRouter dollars.
    A fundi seat whose preset sets no max_cost is uncapped, not free: it is left
    out of the sum and named in `uncapped`, and the bound is null, because a
    number there would be a bound we cannot stand behind.

    A seat whose context window nothing reports gets `context_window: null` and a
    `not_reported` entry naming it, which is information, not a refusal: no real
    preset carries a window and the SDK has no catalog verb, so treating an
    unreported window as a block would block every seat of every run. Nothing is
    assumed in its place either, and the blocking path is kept for the seat whose
    window IS known and is too small for the packet.

    The estimate also says plainly whether the fit was checked at all, in
    `fit_checked` and `fit_note`: in production no seat carries a window, so the
    design's one hard limit cannot fire for any of them, and the operator must be
    told that rather than have to infer it from a column of nulls.

    Beyond the finder cells and the panel seats, the estimate prices the verifier
    spawns each expected cluster draws and the claim jobs a panel's "checked:
    nothing" answers raise. The claim jobs are priced by the kind of the verifier
    seat that runs each one, like every other row, because a bound that leaves
    them out is not offered as a bound."""
    pjson = pr_json(rdir)
    seats = manifest["seats"]
    panel = list(pjson["panel"])
    n_lenses = len(manifest["lenses"])
    total = manifest["total_tokens"]
    names = run_seats(pjson)
    gather, finder = pjson["gather"], pjson["finder"]
    fan = fanout(pjson, str(rdir / "project" / "pr.json"))
    n_finders = len(finder_lenses(manifest["lenses"], fan))
    if fan["split_groups"]:
        ctx = rdir / "packet" / "context.json"
        groups = (json.loads(ctx.read_text()).get("file_groups") or []) if ctx.exists() else []
        n_finders *= max(len(groups), 1)

    by_seat, fundi_usd, uncapped = {}, 0.0, []

    def add(name, caps):
        """caps: how many spawns of this seat the bound covers."""
        nonlocal fundi_usd
        cost = seats[name]["max_cost"]
        if cost is None:
            if name not in uncapped:
                uncapped.append(name)
            return
        fundi_usd += cost * caps
        by_seat[name] = round(by_seat.get(name, 0.0) + cost * caps, 4)

    for name in names:
        if seats[name]["kind"] != "fundi":
            continue
        if name == gather:
            add(name, 1)
        elif name == finder:
            add(name, n_finders)   # one cell per finder lens (x file group when split)
        elif name in panel:
            add(name, 1)
    # Each expected cluster (one per lens, one per panel seat) gets two verifier
    # spawns, priced at the cheapest capped fundi verify seat.
    fundi_verify = [n for n in names if n not in (gather, finder)
                    and n not in panel and seats[n]["kind"] == "fundi"]
    capped = [n for n in fundi_verify if seats[n]["max_cost"] is not None]
    for n in fundi_verify:
        if n not in capped:
            add(n, 1)   # no cap to multiply: add() only names it in uncapped
    if capped:
        cheapest = min(capped, key=lambda n: (seats[n]["max_cost"], n))
        add(cheapest, 2 * (n_lenses + len(panel)))

    # The claim jobs: every panel seat answers every always-on lens, and a
    # "checked: nothing" answer is itself a claim verifiers try to refute (design
    # section 4), so one claim per (panel seat, always-on lens) is checked by the
    # fresh children on the verifier twins of the panel seats that did not raise
    # it. The verifier seat serving one panel seat therefore carries
    # n_always x (panel seats - 1) claim spawns, which is the ruling's
    # "always-on lenses x panel seats x 2" for the standard three-seat panel. Each
    # is priced by the kind of the verifier seat that runs it, like every other
    # row: a fundi verifier seat adds its capped dollars (and leaves the bound null
    # when it is uncapped), a claude-kind one the packet volume it re-reads.
    n_always = sum(1 for l in manifest["lenses"] if l.get("always"))
    claim_spawns = n_always * max(len(panel) - 1, 0) if fan["verify_claims"] else 0
    claude_claim = 0
    if claim_spawns:
        for p in panel:
            v = (pjson.get("verifiers") or {}).get(p)
            if not v or v not in seats:
                continue
            if seats[v]["kind"] == "claude":
                claude_claim += claim_spawns
            else:
                add(v, claim_spawns)

    claude = [n for n in names if seats[n]["kind"] == "claude"]
    catalog = sdk_windows(rk)

    def window(name):
        """The preset's window if it carries one, else the SDK catalog's, else
        null: no default, because an assumed window is a false claim about fit."""
        return seats[name]["context_window"] or catalog.get(seats[name]["model"])

    blocking, not_reported = [], []
    for name in names:
        win = window(name)
        if win is None:
            pr_state.append_friction(rdir, {
                "seat": name, "kind": "context-window",
                "note": f"seat {name} model {seats[name]['model']}: neither the preset nor the SDK model "
                        f"catalog reports a context window, so the packet cannot be checked against it"})
            not_reported.append({"seat": name, "reason": "context window not reported"})
        elif total > CONTEXT_FRACTION * win:
            blocking.append({"seat": name, "reason": "packet exceeds context"})
    # The design's one hard limit can only fire for a seat whose window something
    # reports. No shipped preset carries one and the SDK has no catalog, so in
    # production every seat is unchecked, and that has to be said plainly rather
    # than left for the operator to infer from a null. No window is assumed, and
    # an unchecked fit is informational, never a block: treating it as one would
    # stop every run before a single seat is spawned.
    fit_checked = not not_reported
    if fit_checked:
        fit_note = f"fit is checked against every seat's context window ({len(names)} seats)"
    elif len(not_reported) == len(names):
        fit_note = ("fit is NOT CHECKED: no seat carries a context window, so the packet is not "
                    "checked against any seat's limit (informational, not blocking)")
    else:
        fit_note = (f"fit is NOT CHECKED for {len(not_reported)} of {len(names)} seats: no context "
                    f"window is reported for them, so the packet is not checked against theirs "
                    f"(informational, not blocking)")
    return {
        "total_tokens": total,
        "fundi": {"usd_upper_bound": None if uncapped else round(fundi_usd, 4),
                  "label": "upper bound", "by_seat": by_seat, "uncapped": sorted(uncapped)},
        "claude": {"input_tokens": total * (len(claude) + claude_claim), "seat_count": len(claude),
                   "seats": claude},
        "seats": {n: {**seats[n], "context_window": window(n)} for n in names},
        "lenses": n_lenses, "panel": len(panel),
        "blocking": blocking,
        "not_reported": not_reported,
        "fit_checked": fit_checked,
        "fit_note": fit_note,
    }


@pr_state.friction_stage("gather")
def stage_gather(rk, home, args):
    """Build the packet, price it, and stop at awaiting-confirm when the operator
    has to decide. Nothing is ever cut: the packet on disk is the whole packet.

    The run is claimed before anything is written and released when the stage
    returns, so a second invocation while this one runs fails with a clear
    message instead of building a second packet into the same directory. The
    status guard lives in build_packet, so a run that is not gathering is
    refused before any byte of its packet is touched. A failure that aborts the
    stage is written to the run as a `stage-error` friction entry before the
    claim is released: state.json still says gathering, so that record is what
    tells a later reader the run was aborted rather than left unfinished."""
    rdir, run = home.load_run(args.run)
    claim = claim_gather(rdir, run)
    try:
        manifest = build_packet(rk, home, rdir, lens_generic=LENSES)
        est = estimate(rk, rdir, manifest)
        limit = pr_json(rdir).get("confirm_over_tokens")
        over = limit is not None and manifest["total_tokens"] > limit
        status = "awaiting-confirm" if (over or est["blocking"]) else "confirmed"
        state = pr_state.load_state(rdir)
        state["status"] = status
        state["gather"] = {"packet": "packet", "files": "packet/files.txt",
                           "manifest": "packet/manifest.json",
                           "estimate": est, "over_threshold": over, "confirm_over_tokens": limit}
        pr_state.save_state(rdir, state)
    except Fail as e:
        # This stage is the only record of what aborted it: state.json still says
        # gathering, so without this a mid-stage failure leaves the run silent.
        record_friction(rdir, {"kind": "stage-error", "note": f"gather {run['run_id']}: {e}"})
        raise
    except Exception as e:
        record_friction(rdir, {"kind": "stage-error", "note": f"gather {run['run_id']}: {e}"})
        raise Fail(f"gather {run['run_id']}: {e}") from e
    finally:
        # Best effort, reported and never raised: the claim is bookkeeping and must
        # not replace the error that brought us here. It does not need the daemon,
        # so a lost one (the likeliest cause of that error) still releases the run.
        try:
            release_gather(rdir, claim)
        except Fail as e:
            print(f"gather: could not release the in-progress claim: {e}")
    bound = est["fundi"]["usd_upper_bound"]
    print(f"gather: {manifest['total_tokens']} tokens (threshold {limit}), fundi upper bound "
          f"{'UNKNOWN' if bound is None else f'${bound:.2f}'}, claude {est['claude']['seat_count']} seats "
          f"x {manifest['total_tokens']} tokens")
    for name in est["fundi"]["uncapped"]:
        print(f"gather: seat {name} sets no max_cost, so the fundi bound is incomplete (not $0)")
    print(f"gather: {est['fit_note']}")
    for b in est["not_reported"]:
        print(f"gather: seat {b['seat']}: context window not reported, so the packet's fit is not checked "
              f"(informational, not blocking)")
    for b in est["blocking"]:
        print(f"gather: seat {b['seat']} cannot confirm the packet ({b['reason']})")
    print(f"gather: {status}" + ("" if status == "confirmed" else f"; confirm or abort run {run['run_id']}"))
    return {"stage": "gather", "run": run["run_id"], "status": status, "tree": run["tree"],
            "packet": str(rdir / "packet"), "total_tokens": manifest["total_tokens"],
            "estimate": est, "blocking": est["blocking"], "not_reported": est["not_reported"]}
