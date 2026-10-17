# review-swarm: the stages (map, find, verify), each resumable from its attempt dir
import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path

from rafiki import ConnectError

from .common import PROMPTS, AtCap, Fail, as_line, last_json_block, parse_only, render_prompt
from .presets import check_resumed, check_seats
from .rafiki_io import final_answer
from .runs import (
    audit_config,
    match_globs,
    next_attempt,
    run_concerns,
    run_cwd,
    run_text,
)

# Default workers in flight per stage (--parallel); launch grants the stage this
# many. A PR review is ~70 short finder cells, so 8 left most of the run queued.
FIND_CAP = 24
AT_CAP = object()  # spawn_cell/spawn_job: refused at the live-children cap, requeue

# The whole-repo seats when audit.json names none: the fundi seats these stages
# were built on. A project picks its own seat group in audit.json "seats".
AUDIT_SEATS = {"mapper": "review:mapper", "finder": "review:reviewer", "verifier": "review:verifier"}


def honors_map_lenses(rdir):
    """Whether find runs only the lenses the map chose for each component and
    seam (the default), or every lens everywhere (audit.json
    "honor_map_lenses": false, for comparing against runs made before)."""
    return audit_config(rdir / "project").get("honor_map_lenses", True)


def audit_seat(rdir, role):
    seats = audit_config(rdir / "project").get("seats") or {}
    unknown = sorted(set(seats) - set(AUDIT_SEATS))
    if unknown:
        raise Fail(f"audit.json seats: unknown role(s) {', '.join(unknown)}; expected {sorted(AUDIT_SEATS)}")
    return seats.get(role, AUDIT_SEATS[role])


def model_labels(model):
    return {"review/model": model} if model else {}


def child_metrics(info, conv, prefill_paths):
    """Per-child numbers for later analysis. Token fields are summed over
    reported turns only, and stay null when no turn reported them: a child whose
    provider never reported usage is not a child that used no tokens (a
    where 0 is a measured zero and null is not reported)."""
    turns = (conv or {}).get("turns") or []
    tools, rereads = {}, []
    tin = tout = tcache = None
    model_turns = 0
    providers = {}
    for t in turns:
        if t.get("input_tokens") is not None:
            tin = (tin or 0) + t["input_tokens"]
            tcache = (tcache or 0) + (t.get("cache_read_tokens") or 0)
            model_turns += 1
            if t.get("role") == "assistant":
                prov = t.get("served_provider") or "?"
                providers[prov] = providers.get(prov, 0) + 1
        if t.get("output_tokens") is not None:
            tout = (tout or 0) + t["output_tokens"]
        content = t.get("content") or []
        # A claude-kind child's turns can carry content as a plain string
        # rather than a list of blocks; neither a string nor a non-dict block
        # is a tool call.
        if isinstance(content, str):
            continue
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "tool_use" or str(b.get("id", "")).startswith("prefill_"):
                continue
            tools[b["name"]] = tools.get(b["name"], 0) + 1
            path = (b.get("input") or {}).get("path") or (b.get("input") or {}).get("file_path")
            if b["name"] == "read" and path and path.lstrip("./") in prefill_paths:
                rereads.append(path)
    labels = info.get("labels") or {}
    return {
        "child_id": info.get("child_id"), "conversation_id": info.get("session_id"),
        "status": info.get("status"), "model": info.get("model"),
        "preset": labels.get("rafiki/preset"), "preset_id": labels.get("rafiki/preset-id"),
        "cost_usd": info.get("cost_usd"), "model_turns": model_turns,
        "input_tokens": tin, "output_tokens": tout, "cache_read_tokens": tcache,
        "providers": providers,
        "tool_calls": tools, "prefill_rereads": rereads,
    }


def settled(rk, cid, timed_out=False):
    """(info, conv) once cid has settled on an answer (idle with a final
    answer, or exited), or once timed_out; else None. The answer check, not
    idle alone, is the settle: a fundi child is idle between its spawn and its
    prompt landing, and a re-attached --collect has no record of the send."""
    info = rk.child_info(cid)
    status = info.get("status")
    if status not in ("idle", "exited") and not timed_out:
        return None
    try:
        conv = rk.export(cid)
    except ConnectError as e:
        if e.code != "not_found":
            raise
        # Nothing captured yet (a claude child is idle before its first model
        # request lands): no transcript, so the answer check below keeps an
        # idle child waiting and lets an exited or timed-out one collect empty.
        conv = None
    if final_answer(conv) is None and status == "idle" and not timed_out:
        return None
    return info, conv


def wait_settled(rk, pending, *, timeout=900, poll=3.0):
    """pending: child ids. Returns {child_id: (info, conv)} once each child has
    settled; children still unsettled at the timeout come back with conv None
    and info["status"] as last seen."""
    pending, done = list(pending), {}
    deadline = time.time() + timeout
    while pending and time.time() < deadline:
        for cid in list(pending):
            got = settled(rk, cid)
            if got is not None:
                done[cid] = got
                pending.remove(cid)
        if pending:
            time.sleep(poll)
    for cid in pending:
        done[cid] = (rk.child_info(cid), None)
    return done


# --- map -----------------------------------------------------------------------


GLOB_CHARS = set("*?[")


def expand_paths(paths, files):
    """A component's paths with globs expanded against the run's files, in
    order, without repeats. A map of a large repo names directories by glob
    rather than listing hundreds of files."""
    out = []
    for p in paths or []:
        for f in (match_globs(files, [p]) if GLOB_CHARS & set(p) else [p]):
            if f not in out:
                out.append(f)
    return out


# A seam with no paths of its own is reviewed over every file of every
# component it joins; past this many bytes (~60K tokens) that is most of a
# seat's context spent on code the boundary does not touch.
SEAM_UNION_MAX_BYTES = 210_000


def seam_files(sm, owned, files):
    """A seam's own boundary files when the map names them, else every file of
    the components it joins."""
    if sm.get("paths"):
        return expand_paths(sm["paths"], files)
    return sorted({p for n in sm["components"] if n in owned for p in owned[n]})


def seam_problems(sm, owned, run):
    name, problems = sm.get("seam"), []
    for g in sm.get("paths") or []:
        if GLOB_CHARS & set(g) and not match_globs(run["files"], [g]):
            problems.append(f"seam {name}: glob {g} matches no file")
        elif not GLOB_CHARS & set(g) and g not in run["files"]:
            problems.append(f"seam {name}: unknown path {g}")
    if not sm.get("paths") and run.get("tree"):
        tree = Path(run["tree"])
        size = sum((tree / p).stat().st_size for p in seam_files(sm, owned, run["files"]) if (tree / p).is_file())
        if size > SEAM_UNION_MAX_BYTES:
            problems.append(f"seam {name}: its components hold ~{size // 3500}K tokens; "
                            "name the boundary's own files in paths")
    return problems


def validate_map(doc, run, concerns=None, defects=None):
    """concerns: the lens names a map may choose from. defects: the per-component
    defect lenses, given only when find honors the map's choices; each must be
    chosen somewhere, since a defect lens the map leaves off every component
    would silently never run."""
    problems = []
    comps = doc.get("components")
    if not isinstance(comps, list) or not comps:
        return ["no components"]
    lenses = set(concerns) if concerns is not None else None
    by_name = {c.get("component"): expand_paths(c.get("paths"), run["files"]) for c in comps}
    for sm in doc.get("seams") or []:
        for l in sm.get("lenses") or []:
            if lenses is not None and l not in lenses:
                problems.append(f"seam {sm.get('seam')}: unknown concern {l}")
        problems += seam_problems(sm, by_name, run)
    if defects is not None:
        chosen = {l for c in comps for l in c.get("lenses") or []}
        problems += [f"defect lens {d} is on no component" for d in sorted(set(defects) - chosen)]
    files = set(run["files"])
    seen = {}
    for c in comps:
        name = c.get("component")
        for g in c.get("paths") or []:
            if GLOB_CHARS & set(g) and not match_globs(run["files"], [g]):
                problems.append(f"{name}: glob {g} matches no file")
        for p in expand_paths(c.get("paths"), run["files"]):
            if p not in files:
                problems.append(f"{name}: unknown path {p}")
            seen.setdefault(p, []).append(name)
        for l in c.get("lenses") or []:
            if lenses is not None and l not in lenses:
                problems.append(f"{name}: unknown concern {l}")
    for p in run["must_cover"]:
        owners = seen.get(p, [])
        if len(owners) != 1:
            problems.append(f"{p}: in {len(owners)} components {owners}")
    return problems


def compare_maps(doc, ref, files):
    """File-by-file: which component each map puts it in, and the files whose
    grouping differs (two files together in one map but apart in the other)."""
    def owner(d):
        return {p: c["component"] for c in d.get("components", []) for p in expand_paths(c.get("paths"), files)}
    a, b = owner(doc), owner(ref)
    files = sorted(set(a) & set(b))
    split, merged = [], []
    for i, f in enumerate(files):
        for g in files[i + 1:]:
            together_a, together_b = a[f] == a[g], b[f] == b[g]
            if together_b and not together_a:
                split.append((f, g))
            elif together_a and not together_b:
                merged.append((f, g))
    lines = ["file -> mapper / reference"]
    lines += [f"  {f}: {a.get(f)} / {b.get(f)}" for f in sorted(set(a) | set(b))]
    lines.append(f"pairs the reference groups but the mapper splits: {len(split)}")
    lines += [f"  {f} | {g}" for f, g in split]
    lines.append(f"pairs the mapper groups but the reference splits: {len(merged)}")
    lines += [f"  {f} | {g}" for f, g in merged]
    return "\n".join(lines) + "\n"


# Above this the mapper gets a file listing and the project's context files
# instead of every file: ~100K tokens, past which pre-filling a whole repo stops
# fitting a seat (and a claude seat cannot be pre-filled at all).
MAP_PREFILL_MAX_BYTES = 350_000


def map_packet(tree, run):
    """(files to deliver, listing text). Small repos go to the mapper whole; a
    large one as a per-file line count plus context_files, read further with
    tools."""
    sizes = {p: (tree / p).stat().st_size for p in run["files"] if (tree / p).is_file()}
    if sum(sizes.values()) <= MAP_PREFILL_MAX_BYTES:
        return list(run["files"]), ""
    context = [p for p in run["project"].get("context_files", []) if p in sizes]
    lines = [f"{p} {sum(1 for _ in (tree / p).open(errors='replace'))}" for p in run["files"] if p in sizes]
    listing = ("The repository is too large to hand you whole. Every file in scope, with its line count, "
               "is listed below; read what you need with your tools. Name a component's paths with globs "
               "(e.g. `pkg/store/*.go`) where that is exact.\n\nFILES (path, lines):\n" + "\n".join(lines) + "\n")
    return context, listing


def map_lens_names(rdir):
    """(lenses a map may choose, defect lenses it must place) for validate_map."""
    local = [c for c in run_concerns(rdir) if not c.get("packet")]
    defects = [c["name"] for c in local if c["category"] == "defect"] if honors_map_lenses(rdir) else None
    return [c["name"] for c in local], defects


def stage_map(rk, home, args):
    rdir, run = home.load_run(args.run)
    if args.collect:
        adir = rdir / "map" / args.collect
        if not (adir / "children.json").exists():
            raise Fail(f"{adir}/children.json not found; nothing to collect")
        return collect_map(rk, rdir, adir, run, args.timeout)
    target = run_cwd(run)
    preset = audit_seat(rdir, "mapper")
    seats = check_seats(rk, [preset])
    adir = next_attempt(rdir / "map")
    (adir / "seats.json").write_text(json.dumps({"presets": seats, "model": args.model}, indent=2) + "\n")
    packet, listing = map_packet(target, run)
    task = render_prompt("task-map.md",
                         lenses=", ".join(c["name"] for c in run_concerns(rdir) if not c.get("packet")),
                         must_cover="\n".join(run["must_cover"]), listing=listing)
    (adir / "task.md").write_text(task)
    (adir / "prefill.txt").write_text("\n".join(packet) + "\n")
    shutil.copytree(PROMPTS, adir / "prompts")
    children = {}
    for i in range(1, args.mappers + 1):
        name = f"map-{run['run_id']}-{adir.name}-{i}"
        try:
            cid, conv_id, _ = rk.spawn_seat(name, preset=preset, cwd=target, packet_files=packet,
                                            instruction=task, model=args.model,
                                            labels={"review/run": run["run_id"], "review/stage": "map",
                                                    "review/attempt": adir.name, "review/mapper": str(i),
                                                    **model_labels(args.model)})
        except Fail as e:
            # Most often the child died during its pre-fill (see the daemon
            # log for the child's own error); record it and keep going.
            cdir = adir / f"mapper-{i}"
            cdir.mkdir()
            (cdir / "spawn-error.txt").write_text(f"{e}\n")
            print(f"mapper {i}: SPAWN FAILED {e}")
            continue
        children[cid] = {"mapper": i, "conversation_id": conv_id, "spawned_at": time.time()}
        # Written after every spawn: the children outlive this process, and
        # `map --collect` re-attaches to them from this file alone.
        (adir / "children.json").write_text(json.dumps(children, indent=2) + "\n")
        print(f"mapper {i}: {cid}")
    if not children:
        raise Fail(f"no mapper started; see {adir}/mapper-*/spawn-error.txt and the daemon log")
    return collect_map(rk, rdir, adir, run, args.timeout)


def collect_map(rk, rdir, adir, run, timeout):
    """Wait for this attempt's mappers and write their outputs. Safe to re-run:
    mappers already collected (their child.json exists) are skipped."""
    children = json.loads((adir / "children.json").read_text())
    pending = [cid for cid, c in children.items()
               if not (adir / f"mapper-{c['mapper']}" / "child.json").exists()]
    if not pending:
        print(f"all mappers in {adir.name} already collected")
    print(f"waiting for {len(pending)} mapper(s): {', '.join(pending)}")
    results = wait_settled(rk, pending, timeout=timeout)
    ref_file = rdir / "project" / "reference-map.json"
    valid = 0
    for cid, (info, conv) in results.items():
        i = children[cid]["mapper"]
        cdir = adir / f"mapper-{i}"
        cdir.mkdir(exist_ok=True)
        if conv is None and info.get("status") not in ("idle", "exited"):
            print(f"mapper {i}: still {info.get('status')} at the timeout; re-run with --collect {adir.name}")
            continue
        m = child_metrics(info, conv, set((adir / "prefill.txt").read_text().split()))
        m["wall_s"] = round(time.time() - children[cid]["spawned_at"], 1)
        (cdir / "child.json").write_text(json.dumps(m, indent=2) + "\n")
        answer = final_answer(conv) if conv else None
        (cdir / "raw.md").write_text(answer or "")
        rk.stop(cid)
        if answer is None:
            (cdir / "parse-error.txt").write_text(f"no final answer (status {info.get('status')})\n")
            print(f"mapper {i}: NO ANSWER (status {info.get('status')})")
            continue
        try:
            doc = last_json_block(answer)
        except (ValueError, json.JSONDecodeError) as e:
            (cdir / "parse-error.txt").write_text(f"{e}\n")
            print(f"mapper {i}: PARSE ERROR {e}")
            continue
        (cdir / "map.json").write_text(json.dumps(doc, indent=2) + "\n")
        problems = validate_map(doc, run, *map_lens_names(rdir))
        valid += not problems
        (cdir / "validation.txt").write_text("\n".join(problems) + ("\n" if problems else "ok\n"))
        if ref_file.exists():
            (cdir / "vs-reference.txt").write_text(compare_maps(doc, json.loads(ref_file.read_text()), run["files"]))
        print(f"mapper {i}: {len(doc.get('components', []))} components, {len(doc.get('seams', []))} seams, "
              f"{'valid' if not problems else str(len(problems)) + ' problems'}, "
              f"${m['cost_usd'] or 0:.4f}, {m['model_turns']} model turns, tools {m['tool_calls'] or '-'}")
    print(f"inspect: {adir}")
    print(f"then:    review-swarm promote {run['run_id']} map {adir.name}[/mapper-N]  (then hand-edit map/final.json)")
    return {"stage": "map", "run": run["run_id"], "attempt": adir.name, "mappers": len(children), "valid": valid}


# --- find ----------------------------------------------------------------------


def find_cells(rdir, run, arms, only, context_policy="component"):
    """Every (arm, subject, concern) the find stage covers. A subject is a map
    component (swarm), a seam between components (seam), or the whole repo
    (control); its files are what the finder gets pre-filled."""
    final = rdir / "map" / "final.json"
    if not final.exists():
        raise Fail("no map/final.json; promote a map attempt first")
    fmap = json.loads(final.read_text())
    comps = {c["component"]: c for c in fmap["components"]}
    owned = {c["component"]: expand_paths(c["paths"], run["files"]) for c in fmap["components"]}
    context = run["project"].get("context_files", [])
    concerns = run_concerns(rdir)
    honor = honors_map_lenses(rdir)
    cfg = audit_config(rdir / "project")
    test_globs = cfg.get("test_globs") or []
    lens_seats = cfg.get("lens_seats") or {}
    unknown = sorted(set(lens_seats) - {c["name"] for c in concerns})
    if unknown:
        raise Fail(f"audit.json lens_seats: unknown lens(es) {', '.join(unknown)}")
    subjects = []
    if "swarm" in arms:
        for c in fmap["components"]:
            files = list(owned[c["component"]])
            if context_policy == "neighbours":
                files += [p for n in c.get("neighbours") or [] if n in comps
                          for p in owned[n] if p not in files]
            subjects.append({"arm": "swarm", "name": c["component"], "files": files,
                             "lenses": c.get("lenses") if honor else None,
                             "scope": f"component `{c['component']}`: {c.get('purpose', '')}",
                             "notes": "\n".join(x for x in [
                                 f"Neighbours: {', '.join(c.get('neighbours') or []) or 'none'}.",
                                 f"Notes from the map: {c['notes']}" if c.get("notes") else ""] if x)})
    if "seam" in arms:
        for sm in fmap.get("seams", []):
            files = seam_files(sm, owned, run["files"])
            subjects.append({"arm": "seam", "name": sm["seam"].replace(" ", "-"), "files": files,
                             "lenses": sm.get("lenses") if honor else None,
                             "scope": f"the seam `{sm['seam']}` between components {', '.join(sm['components'])}",
                             "notes": f"What crosses this seam: {sm['what']}\nReview the BOUNDARY: each side's "
                                      "assumptions about the other. Problems internal to one side are out of scope."})
    if "control" in arms:
        subjects.append({"arm": "control", "name": "whole-repo", "files": run["files"],
                         "scope": "the whole repository", "notes": ""})
    # A lens with a packet is cross-cutting (flag spelling across every command,
    # one capability built twice): one cell over the files its packet names,
    # never a per-component cell, where it could only see a fragment.
    if "cross" in arms:
        for con in concerns:
            if not con.get("packet"):
                continue
            files = match_globs(run["files"], con["packet"])
            if not files:
                raise Fail(f"lens {con['name']}: packet {con['packet']} matches no file in the run")
            subjects.append({"arm": "cross", "name": con["name"], "files": files, "only": con["name"],
                             "scope": "the whole repository, through the files below chosen for this concern",
                             "notes": "Your files are a slice picked to make this concern tractable across the repo. "
                                      "Follow what they point to with your read-only tools."})
    cells = []
    for sub in subjects:
        for con in concerns:
            if not places(sub, con):
                continue
            cell = {**sub, "concern": con}
            if only.get("arm") and sub["arm"] not in only["arm"]:
                continue
            if only.get("component") and sub["name"] not in only["component"]:
                continue
            if only.get("concern") and con["name"] not in only["concern"]:
                continue
            cell["key"] = f"{sub['arm']}__{sub['name']}__{con['name']}"
            if only.get("cell") and cell["key"] not in only["cell"]:
                continue
            files = sub["files"]
            if test_globs and sub["arm"] in ("swarm", "seam") and not con.get("tests"):
                # Tests are most of a component's bytes and few lenses judge
                # them; the finder still reads one when a finding depends on it.
                files = [f for f in files if not match_globs([f], test_globs)]
                if len(files) < len(sub["files"]):
                    cell["notes"] = "\n".join(x for x in [cell["notes"], TESTS_OMITTED] if x)
            lens_context = match_globs(run["files"], con.get("context") or [])
            cell["prefill"] = list(dict.fromkeys(files + lens_context + context))
            cell["preset"] = lens_seats.get(con["name"])
            cells.append(cell)
    return cells


def places(sub, con):
    """Whether lens con gets a cell on subject sub. A cross-cutting lens runs
    only in its own cross cell; the control arm takes every other lens; a
    component or seam takes a lens its triggers allow and its map chose."""
    if sub["arm"] == "cross":
        return con["name"] == sub["only"]
    if con.get("packet"):
        return False
    if sub["arm"] == "control":
        return True
    chosen = sub.get("lenses")
    return lens_applies(con, sub["files"]) and (chosen is None or con["name"] in chosen)


def skipped_lenses(rdir, run, cells):
    """{arm/subject: [lens, ...]}: per component or seam this find covers, the
    lenses its triggers allow that the map left off."""
    if not honors_map_lenses(rdir):
        return {}
    local = [c for c in run_concerns(rdir) if not c.get("packet")]
    ran = {}
    for c in cells:
        if c["arm"] in ("swarm", "seam"):
            ran.setdefault((c["arm"], c["name"]), (c["files"], set()))[1].add(c["concern"]["name"])
    out = {}
    for (arm, name), (files, names) in ran.items():
        cut = sorted(con["name"] for con in local if con["name"] not in names and lens_applies(con, files))
        if cut:
            out[f"{arm}/{name}"] = cut
    return out


def lens_applies(con, files):
    """A lens whose triggers name paths runs only on subjects holding one of
    them (the front-end lens on the CLI package, say); diff_matches has no
    meaning without a diff and is ignored here."""
    trig = con.get("triggers") or "always"
    if trig == "always" or not trig.get("paths"):
        return True
    return bool(match_globs(files, trig["paths"]))


def dedup_key(f):
    return f"{f.get('file')}:{as_line(f.get('line')) // 30}"


def finding_id(f, concern):
    title = " ".join(str(f.get("title", "")).lower().split())
    return hashlib.sha256(f"{f.get('file')}\0{concern}\0{title}".encode()).hexdigest()[:12]


def find_task(rdir, cell, reported, context=None):
    con = cell["concern"]
    if con["category"] == "defect":
        rules = "\n\n".join(x for x in [run_text(rdir, "severity.md"), run_text(rdir, "evidence.md"),
                                         run_text(rdir, "bug-classes.md") and
                                         "Also sweep every instance of these known bug shapes:\n" + run_text(rdir, "bug-classes.md")] if x)
        rules += '\nSet category="defect" and severity.'
    elif con["category"] == "drift":
        rules = (run_text(rdir, "evidence.md") + '\nSet category="drift". Put the doc path in doc, the doc\'s claim '
                 '(quoted) in claim, and the code reality in file, line and evidence: where the code contradicts '
                 'the claim, or, for a rule the doc says is enforced, that nothing enforces or pins it.')
    else:
        rules = 'Set category="quality" and effort (S/M/L). Only report issues genuinely worth fixing.'
    suppress = ""
    if reported:
        suppress = ("\nAlready reported in this scope (do NOT re-report these or near-duplicates; hunt elsewhere):\n"
                    + "\n".join(f"- {r}" for r in reported) + "\n")
    fields = {"scope": cell["scope"], "scope_notes": cell["notes"], "concern_name": con["name"],
              "concern_prompt": con["prompt"], "category_rules": rules, "suppress": suppress,
              "category": con["category"]}
    if context is not None:
        return render_prompt("task-find-oneshot.md", context=context, **fields)
    return render_prompt("task-find.md", **fields)


NEEDS_MAX_BYTES = 80_000   # extra inline context one round may add to a cell (~20K tokens)
GREP_MAX_HITS = 60


def render_inline(tree, specs):
    """Files/ranges in the Read tool's `%6d\t%s` numbering, `=== path ===`
    headers; grep specs as `=== grep <re> ===` with path:line: hits."""
    out = []
    for sp in specs:
        if "grep" in sp:
            r = subprocess.run(["grep", "-rnIE", "--", sp["grep"], "."], cwd=tree, capture_output=True, text=True, check=False)
            hits = [h.removeprefix("./") for h in r.stdout.splitlines()][:GREP_MAX_HITS]
            out.append(f"=== grep {sp['grep']} ({len(hits)} hits{', truncated' if len(hits) == GREP_MAX_HITS else ''}) ===\n"
                       + ("\n".join(hits) if hits else "(no matches)"))
            continue
        f = tree / str(sp["path"]).lstrip("./")
        if not f.is_file():
            out.append(f"=== {sp['path']} ===\n(no such file)")
            continue
        lines = f.read_text(errors="replace").splitlines()
        a = max(1, int(sp.get("start") or 1))
        b = min(len(lines), int(sp.get("end") or len(lines)))
        rng = "" if (a == 1 and b == len(lines)) else f" (lines {a}-{b})"
        out.append(f"=== {sp['path']}{rng} ===\n" + "\n".join(f"{i:6d}\t{lines[i - 1]}" for i in range(a, b + 1)))
    return "\n\n".join(out)


def resolve_needs(tree, needs, have):
    """Turn a finder's `needs` into new context specs, skipping what the cell
    already has and capping the round's added bytes."""
    added, budget = [], NEEDS_MAX_BYTES
    for n in needs or []:
        if not isinstance(n, dict):
            continue
        if n.get("grep"):
            sp = {"grep": str(n["grep"])}
        elif n.get("path"):
            sp = {"path": str(n["path"]).lstrip("./"), "start": n.get("start"), "end": n.get("end")}
            if {"path": sp["path"], "start": None, "end": None} in have:
                continue  # whole file already present
        else:
            continue
        if sp in have or sp in added:
            continue
        size = len(render_inline(tree, [sp]))
        if size > budget:
            continue
        budget -= size
        added.append(sp)
    return added


FIND_SEATS = {"oneshot": "review:oneshot"}


def find_seat(rdir, seat):
    """--seat reviewer (the default) is the project's finder seat; oneshot is a fixed experimental seat."""
    return audit_seat(rdir, "finder") if (seat or "reviewer") == "reviewer" else FIND_SEATS[seat]


TESTS_OMITTED = "This component's tests are not in your files; read one when a finding depends on what it pins."


def find_presets(default, cells):
    """Every seat a find attempt spawns on: the finder plus any lens_seats."""
    return sorted({default} | {c["preset"] for c in cells if c.get("preset")})


def save_find_state(adir, st):
    (adir / "state.json").write_text(json.dumps(st, indent=2) + "\n")


def stage_find(rk, home, args):
    rdir, run = home.load_run(args.run)
    cwd = run_cwd(run)
    if args.collect:
        adir = rdir / "find" / args.collect
        st = json.loads((adir / "state.json").read_text())
        # An attempt started before seats were configurable recorded only --seat.
        st.setdefault("preset", find_seat(rdir, st.get("seat")))
        st["seats"] = check_resumed(rk, find_presets(st["preset"], [cs["cell"] for cs in st["cells"].values()]),
                                    st.get("seats"))
    else:
        arms = set(args.arm or ["swarm", "control", "seam", "cross"])
        cells = find_cells(rdir, run, arms, parse_only(args.only), args.context)
        if not cells:
            raise Fail("no cells match")
        skipped = skipped_lenses(rdir, run, cells)
        preset = find_seat(rdir, args.seat)
        if (args.seat or "reviewer") != "reviewer":
            # An experimental --seat replaces every seat, lens_seats included.
            cells = [{**c, "preset": None} for c in cells]
        seats = check_seats(rk, find_presets(preset, cells))
        adir = next_attempt(rdir / "find")
        shutil.copytree(PROMPTS, adir / "prompts")
        st = {"variant": args.variant or args.model or "", "seat": args.seat, "preset": preset,
              "context": args.context,
              "model": args.model, "seats": seats,
              "max_rounds": args.rounds, "dry_target": 2 if args.rounds > 1 else 1, "round": 0,
              "cells": {c["key"]: {"cell": c, "dry": 0, "seen": [], "reported": [], "done": False,
                                   "extra": []}
                        for c in cells},
              "children": {}}
        save_find_state(adir, st)
        # What the map cut is part of the run's record: a lens left off a
        # component is a lens never run there.
        (adir / "skipped-lenses.json").write_text(json.dumps(skipped, indent=2, sort_keys=True) + "\n")
        for subject, names in sorted(skipped.items()):
            print(f"  {subject}: map skips {', '.join(names)}")
        print(f"find {adir.name} [{args.variant}]: {len(cells)} cells, seat {args.seat}, context {args.context}, "
              f"up to {args.rounds} rounds, {args.parallel} in flight")
    cand_file = adir / "candidates.jsonl"
    oneshot = st.get("seat") == "oneshot"

    def collect(cid, info, conv):
        ch = st["children"][cid]
        cs = st["cells"][ch["cell"]]
        cell = cs["cell"]
        cdir = adir / ch["dir"]
        m = child_metrics(info, conv, set(cell["prefill"]))
        m["wall_s"] = round(time.time() - ch["spawned_at"], 1)
        (cdir / "child.json").write_text(json.dumps(m, indent=2) + "\n")
        answer = final_answer(conv) if conv else None
        (cdir / "raw.md").write_text(answer or "")
        rk.stop(cid)
        ch["collected"] = True
        found = []
        try:
            if answer is None:
                raise ValueError(f"no final answer (status {info.get('status')})")
            doc = last_json_block(answer)
            found = doc.get("findings") if isinstance(doc, dict) else None
            if not isinstance(found, list):
                # ValueError, not TypeError: the handler below catches it as a parse
                # error, which is the intent (a TypeError would escape it).
                raise ValueError("final json has no findings list")  # noqa: TRY004
            if oneshot:
                new = resolve_needs(cwd, doc.get("needs"), [{"path": p, "start": None, "end": None}
                                                             for p in cell["prefill"]] + cs["extra"])
                cs["extra"] += new
                ch["needs"], ch["needs_added"] = len(doc.get("needs") or []), len(new)
                (cdir / "needs.json").write_text(json.dumps({"asked": doc.get("needs") or [], "added": new}, indent=2) + "\n")
        except (ValueError, json.JSONDecodeError) as e:
            (cdir / "parse-error.txt").write_text(f"{e}\n")
            ch["parse_error"] = True
            return
        fresh = []
        for f in found:
            if not isinstance(f, dict) or not f.get("file"):
                continue
            k = dedup_key(f)
            if k in cs["seen"]:
                continue
            cs["seen"].append(k)
            cs["reported"].append(f"{f.get('file')}:{f.get('line')} {f.get('title')}")
            f.update({"id": finding_id(f, cell["concern"]["name"]), "arm": cell["arm"], "subject": cell["name"],
                      "concern": cell["concern"]["name"], "round": ch["round"], "child_id": cid})
            fresh.append(f)
        with (cdir / "findings.jsonl").open("w") as fh:
            for f in found:
                fh.write(json.dumps(f) + "\n")
        with cand_file.open("a") as fh:
            for f in fresh:
                fh.write(json.dumps(f) + "\n")
        ch["reported"], ch["fresh"] = len(found), len(fresh)

    def spawn_cell(key):
        cs = st["cells"][key]
        cell = cs["cell"]
        r = st["round"]
        rel = f"r{r}/{key}"
        cdir = adir / rel
        cdir.mkdir(parents=True, exist_ok=True)
        if oneshot:
            specs = [{"path": p, "start": None, "end": None} for p in cell["prefill"]] + cs["extra"]
            task = find_task(rdir, cell, cs["reported"], context=render_inline(cwd, specs))
            (cdir / "context.json").write_text(json.dumps(specs, indent=2) + "\n")
        else:
            task = find_task(rdir, cell, cs["reported"])
            (cdir / "prefill.txt").write_text("\n".join(cell["prefill"]) + "\n")
        (cdir / "task.md").write_text(task)
        name = f"find-{adir.name}-r{r}-{key}"[:120]
        labels = {**model_labels(st.get("model")),
                  "review/run": run["run_id"], "review/stage": "find",
                  "review/attempt": adir.name, "review/round": str(r),
                  "review/variant": st.get("variant") or "",
                  "review/arm": cell["arm"], "review/component": cell["name"],
                  "review/concern": cell["concern"]["name"]}
        try:
            if oneshot:
                cid, conv_id = rk.spawn(name, cwd=cwd, prefill_lines=None, preset=st["preset"],
                                        model=st.get("model"), labels=labels)
                rk.prompt(cid, task)
            else:
                cid, conv_id, _ = rk.spawn_seat(name, preset=cell.get("preset") or st["preset"], cwd=cwd,
                                                packet_files=cell["prefill"],
                                                instruction=task, model=st.get("model"), labels=labels)
        except AtCap:
            return AT_CAP
        except Fail as e:
            (cdir / "spawn-error.txt").write_text(f"{e}\n")
            cs["error"], cs["done"] = str(e), True
            print(f"  {key}: SPAWN FAILED {e}")
            return None
        st["children"][cid] = {"cell": key, "round": r, "dir": rel, "conversation_id": conv_id,
                               "spawned_at": time.time(), "collected": False}
        save_find_state(adir, st)
        return cid

    def run_round(queue):
        pending = {cid: ch for cid, ch in st["children"].items()
                   if ch["round"] == st["round"] and not ch["collected"]}
        queue = [k for k in queue if not any(ch["cell"] == k and ch["round"] == st["round"]
                                             for ch in st["children"].values())]
        while queue or pending:
            while queue and len(pending) < args.parallel:
                key = queue.pop(0)
                cid = spawn_cell(key)
                if cid is AT_CAP:
                    if not pending:
                        raise Fail("spawn refused at the subtree's live-children cap with nothing of ours running; "
                                   "launch grants max_children, or stop what holds the slots")
                    queue.insert(0, key)
                    break  # wait for a running child to settle
                if cid:
                    pending[cid] = st["children"][cid]
            for cid in list(pending):
                ch = pending[cid]
                got = settled(rk, cid, timed_out=time.time() - ch["spawned_at"] > args.timeout)
                if got is None:
                    continue
                collect(cid, *got)
                save_find_state(adir, st)
                del pending[cid]
                print(f"  r{ch['round']} {ch['cell']}: {ch.get('reported', 0)} reported, {ch.get('fresh', 0)} fresh"
                      f"{', needs ' + str(ch['needs']) + ' asked/' + str(ch['needs_added']) + ' added' if 'needs' in ch else ''}"
                      f"{' PARSE ERROR' if ch.get('parse_error') else ''}")
            if pending:
                time.sleep(3)

    while True:
        active = [k for k, cs in st["cells"].items() if not cs["done"]]
        resuming = any(not ch["collected"] for ch in st["children"].values())
        if not active or (st["round"] >= st["max_rounds"] and not resuming):
            break
        if not resuming:
            st["round"] += 1
            save_find_state(adir, st)
        print(f"round {st['round']}: {len(active)} cells")
        run_round(active)
        for k in active:
            cs = st["cells"][k]
            if cs.get("error"):
                continue  # never reviewed: not dry, reported as errored
            this = [ch for ch in st["children"].values() if ch["cell"] == k and ch["round"] == st["round"]]
            got = sum(ch.get("fresh", 0) for ch in this)
            asked = sum(ch.get("needs_added", 0) for ch in this)
            cs["dry"] = cs["dry"] + 1 if (got == 0 and asked == 0) else 0
            if cs["dry"] >= st["dry_target"] or st["round"] >= st["max_rounds"]:
                cs["done"] = True
        save_find_state(adir, st)

    n = sum(1 for _ in cand_file.open()) if cand_file.exists() else 0
    kids = list(st["children"].values())
    errs = sum(1 for ch in kids if ch.get("parse_error"))
    errored = [k for k, cs in st["cells"].items() if cs.get("error")]
    print(f"find {adir.name}: {st['round']} rounds, {len(kids)} children, {errs} parse failures, {n} candidates"
          f"{f', {len(errored)} cells NEVER REVIEWED (spawn failed)' if errored else ''}")
    print(f"inspect: {adir}/candidates.jsonl and {adir}/r*/<cell>/")
    return {"stage": "find", "run": run["run_id"], "attempt": adir.name, "rounds": st["round"],
            "children": len(kids), "parse_failures": errs, "candidates": n, "errored_cells": len(errored)}


# --- verify ----------------------------------------------------------------------

S1_LENSES = [
    "Your lens: CORRECTNESS. Is the claimed path actually wrong, or does something the finder missed make it safe?",
    "Your lens: REACHABILITY. Does the trigger reproduce from the code as written, every step in order?",
    "Your lens: BLAST RADIUS. If real, is the impact genuinely S1, or overstated?",
]
SEV_ORDER = ["S1", "S2", "S3"]


def adjust_severity(sev, adj):
    if sev not in SEV_ORDER:
        return sev
    i = SEV_ORDER.index(sev)
    if adj == "downgrade":
        i = min(i + 1, 2)
    elif adj == "upgrade":
        i = max(i - 1, 0)
    return SEV_ORDER[i]


CLUSTER_LINES = 10
SEV_RANK = {"S1": 0, "S2": 1, "S3": 2}


def cluster_findings(findings):
    """{representative id: [member ids]}. Findings of one category in one file
    within CLUSTER_LINES of each other are one issue seen through several lenses:
    verified once, through its most severe report, with the others shown to the
    verifier, and the verdict applied to all of them."""
    groups = []
    for f in sorted(findings, key=lambda f: (f.get("category") or "", str(f.get("file")), as_line(f.get("line")))):
        g = groups[-1] if groups else None
        if (g and g[-1].get("category") == f.get("category") and g[-1].get("file") == f.get("file")
                and as_line(f.get("line")) - as_line(g[-1].get("line")) <= CLUSTER_LINES):
            g.append(f)
        else:
            groups.append([f])
    out = {}
    for g in groups:
        rep_f = min(g, key=lambda f: (SEV_RANK.get(f.get("severity"), 3), f["id"]))
        out[rep_f["id"]] = [f["id"] for f in g]
    return out


def verify_prefill(tree, f, context):
    path = str(f.get("file", "")).lstrip("./")
    full = tree / path
    lines = []
    if full.is_file():
        n = sum(1 for _ in full.open(errors="replace"))
        if n <= 2000:
            lines.append(path)
        else:
            line = as_line(f.get("line")) or 1
            lines.append(f"{path}:{max(1, line - 60)}-{line + 60}")
    # A drift finding is judged against the doc it quotes, so the doc comes too.
    doc = str(f.get("doc") or "").lstrip("./")
    if doc and doc not in lines and (tree / doc).is_file():
        lines.append(doc)
    return lines + [c for c in context if c not in lines]


def verify_jobs(f):
    """One verifier per finding; an S1 defect gets the three-lens panel."""
    fid = f["id"]
    if f.get("category") in ("quality", "drift"):
        return [{"key": fid, "finding": fid, "kind": f["category"], "lens": ""}]
    if f.get("severity") == "S1":
        return [{"key": f"{fid}-l{i}", "finding": fid, "kind": "defect", "lens": l}
                for i, l in enumerate(S1_LENSES, 1)]
    return [{"key": fid, "finding": fid, "kind": "defect", "lens": ""}]


def verify_task(rdir, f, job, also=()):
    common = {k: str(f.get(k, "") or "(none given)") for k in
              ("title", "file", "line", "severity", "claim", "evidence", "repro", "effort", "doc")}
    # The verifier judges against what the lens is for: "worth fixing" for a
    # refactor-inventory lens is a different question from "worth touching
    # this one copy", and a verifier that is not told says no to every copy.
    lens = next((c for c in run_concerns(rdir) if c["name"] == f.get("concern")), None)
    common["concern_prompt"] = f"{f.get('concern')}: {lens['prompt']}" if lens else "(not recorded)"
    common["also"] = ("\nAlso reported at this location, through other lenses (judge the issue as a whole):\n"
                      + "\n".join(f"- [{o.get('concern')}] {o.get('title')}: {o.get('claim')}" for o in also) + "\n"
                      if also else "")
    if job["kind"] == "quality":
        return render_prompt("task-verify-quality.md", **common)
    if job["kind"] == "drift":
        return render_prompt("task-verify-drift.md", **common)
    return render_prompt("task-verify-defect.md", lens=job["lens"],
                         severity_rubric=run_text(rdir, "severity.md"), **common)


def stage_verify(rk, home, args):
    rdir, run = home.load_run(args.run)
    tree = run_cwd(run)
    context = run["project"].get("context_files", [])
    if args.collect:
        adir = rdir / "verify" / args.collect
        st = json.loads((adir / "state.json").read_text())
        st.setdefault("preset", AUDIT_SEATS["verifier"])
        st["seats"] = check_resumed(rk, [st["preset"]], st.get("seats"))
    else:
        if not args.find:
            raise Fail("verify needs --find NN (or --collect NN)")
        src = rdir / "find" / args.find / "candidates.jsonl"
        if not src.exists():
            raise Fail(f"{src} not found")
        findings = {}
        for line in src.open():
            f = json.loads(line)
            findings.setdefault(f["id"], f)
        only = parse_only(args.only)
        for key in ("arm", "category", "concern"):
            if only.get(key):
                findings = {k: f for k, f in findings.items() if f.get(key) in only[key]}
        preset = audit_seat(rdir, "verifier")
        seats = check_seats(rk, [preset])
        adir = next_attempt(rdir / "verify")
        shutil.copytree(PROMPTS, adir / "prompts")
        clusters = cluster_findings(findings.values())
        jobs = {j["key"]: {**j, "done": False} for rep_id in clusters for j in verify_jobs(findings[rep_id])}
        st = {"source": str(src.relative_to(rdir)), "findings": findings, "clusters": clusters, "jobs": jobs,
              "children": {}, "model": args.model, "seats": seats, "preset": preset}
        (adir / "state.json").write_text(json.dumps(st, indent=2) + "\n")
        print(f"verify {adir.name}: {len(findings)} findings in {len(clusters)} clusters, {len(jobs)} verifier jobs, "
              f"{args.parallel} in flight")

    def save():
        (adir / "state.json").write_text(json.dumps(st, indent=2) + "\n")

    def spawn_job(key):
        job = st["jobs"][key]
        f = st["findings"][job["finding"]]
        jdir = adir / key
        jdir.mkdir(exist_ok=True)
        members = (st.get("clusters") or {}).get(f["id"], [f["id"]])
        task = verify_task(rdir, f, job, [st["findings"][m] for m in members if m != f["id"]])
        prefill = verify_prefill(tree, f, context)
        (jdir / "task.md").write_text(task)
        (jdir / "prefill.txt").write_text("\n".join(prefill) + "\n")
        try:
            cid, conv_id, _ = rk.spawn_seat(f"verify-{adir.name}-{key}", preset=st["preset"], cwd=tree,
                                            packet_files=prefill, instruction=task, model=st.get("model"),
                                            labels={**model_labels(st.get("model")),
                                                    "review/run": run["run_id"], "review/stage": "verify",
                                                    "review/attempt": adir.name, "review/finding": f["id"]})
        except AtCap:
            return AT_CAP
        except Fail as e:
            (jdir / "spawn-error.txt").write_text(f"{e}\n")
            job["done"], job["error"] = True, "spawn"
            print(f"  {key}: SPAWN FAILED {e}")
            return None
        st["children"][cid] = {"job": key, "conversation_id": conv_id, "spawned_at": time.time(), "collected": False}
        save()
        return cid

    def collect(cid, info, conv):
        ch = st["children"][cid]
        job = st["jobs"][ch["job"]]
        jdir = adir / ch["job"]
        f = st["findings"][job["finding"]]
        m = child_metrics(info, conv, set(verify_prefill(tree, f, context)))
        m["wall_s"] = round(time.time() - ch["spawned_at"], 1)
        (jdir / "child.json").write_text(json.dumps(m, indent=2) + "\n")
        answer = final_answer(conv) if conv else None
        (jdir / "raw.md").write_text(answer or "")
        rk.stop(cid)
        ch["collected"], job["done"] = True, True
        try:
            if answer is None:
                raise ValueError(f"no final answer (status {info.get('status')})")
            v = last_json_block(answer)
            if not isinstance(v, dict):
                # ValueError, not TypeError: the handler below catches it as a parse
                # error, which is the intent (a TypeError would escape it).
                raise ValueError("verdict is not an object")  # noqa: TRY004
            job["verdict"] = v
            (jdir / "verdict.json").write_text(json.dumps(v, indent=2) + "\n")
        except (ValueError, json.JSONDecodeError) as e:
            (jdir / "parse-error.txt").write_text(f"{e}\n")
            job["error"] = "parse"

    pending = {cid: ch for cid, ch in st["children"].items() if not ch["collected"]}
    queue = [k for k, j in st["jobs"].items()
             if not j["done"] and not any(ch["job"] == k for ch in st["children"].values())]
    while queue or pending:
        while queue and len(pending) < args.parallel:
            key = queue.pop(0)
            cid = spawn_job(key)
            if cid is AT_CAP:
                if not pending:
                    raise Fail("spawn refused at the subtree's live-children cap with nothing of ours running; "
                               "launch grants max_children, or stop what holds the slots")
                queue.insert(0, key)
                break
            if cid:
                pending[cid] = st["children"][cid]
        for cid in list(pending):
            ch = pending[cid]
            got = settled(rk, cid, timed_out=time.time() - ch["spawned_at"] > args.timeout)
            if got is None:
                continue
            collect(cid, *got)
            save()
            del pending[cid]
            j = st["jobs"][ch["job"]]
            print(f"  {ch['job']}: {json.dumps(j.get('verdict'))[:120] if j.get('verdict') else j.get('error')}")
        if pending:
            time.sleep(3)

    # An attempt from before clustering verified every finding on its own.
    rep_of = {m: rep_id for rep_id, members in (st.get("clusters") or {f: [f] for f in st["findings"]}).items()
              for m in members}
    out = []
    for fid, f in st["findings"].items():
        jobs = [j for j in st["jobs"].values() if j["finding"] == rep_of[fid]]
        votes = [j["verdict"] for j in jobs if j.get("verdict")]
        r = {**f, "verdicts": votes, "cluster": rep_of[fid]}
        if f.get("category") == "quality":
            v = votes[0] if votes else None
            r["real"] = bool(v and v.get("real"))
            r["upheld"] = bool(v and v.get("real") and v.get("worth_it"))
            r["incomplete"] = v is None
        else:
            need = 2 if len(jobs) > 1 else 1
            upheld_votes = [v for v in votes if v.get("refuted") is False]
            tied = len(votes) > 1 and len(upheld_votes) * 2 == len(votes)
            r["incomplete"] = len(votes) < need or tied
            r["upheld"] = not r["incomplete"] and len(upheld_votes) * 2 > len(votes)
            adj = [v.get("severity_adjustment", "none") for v in upheld_votes]
            final_adj = "downgrade" if adj.count("downgrade") * 2 > len(adj or [1]) else (
                "upgrade" if adj.count("upgrade") * 2 > len(adj or [1]) else "none")
            r["final_severity"] = (adjust_severity(f.get("severity"), final_adj)
                                   if r["upheld"] and f.get("category") != "drift" else None)
        out.append(r)
    with (adir / "verified.jsonl").open("w") as fh:
        for r in out:
            fh.write(json.dumps(r) + "\n")
    by = {}
    for r in out:
        b = by.setdefault(r["arm"], {"upheld": 0, "refuted": 0, "incomplete": 0})
        b["incomplete" if r["incomplete"] else ("upheld" if r["upheld"] else "refuted")] += 1
    print(f"verify {adir.name}: " + "; ".join(f"{a}: {b}" for a, b in sorted(by.items())))
    print(f"inspect: {adir}/verified.jsonl")
    return {"stage": "verify", "run": run["run_id"], "attempt": adir.name, "findings": len(out),
            "upheld": sum(1 for r in out if r["upheld"])}
