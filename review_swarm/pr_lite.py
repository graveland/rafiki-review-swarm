# review-swarm: the lite PR stage - one gatherer, a panel that forks its
# transcript, one consolidator; gather -> review -> verify in a single stage
"""The lite tier reviews a PR with one exploration instead of one per seat.

A gatherer reads the change and what decides it into its own conversation. Each
fundi panel seat then starts from a FORK of that conversation, one cell per
focus (`general`, the whole-change panel task, plus every lens the run
resolves), so the pack is paid for once and every cell pays roughly its own
answer turns. A claude-kind seat cannot be forked to: it gets one independent
cell over the whole change with every focus on its checklist, and is the
panel's control against what the pack never read. One consolidator then
annotates every pooled finding; nothing is dropped.

The run layout is the full pipeline's, so `pr_report` and `compare-pr` read a
lite run unchanged: packet/ (intent, diff, threads, lenses, manifest),
review/NN/ (one dir per cell, candidates.jsonl, checklist.jsonl) and verify/NN/
(verdicts.jsonl, an empty claims.jsonl, state.json naming `stage: lite`).

Concurrency is what `max_children` bounds (the daemon counts live, non-exited
descendants), so every child is stopped as soon as it is collected and the
gatherer is stopped once it has settled: a fork reads its source's stored
nodes, never the live child. Fork cells are queued seat-major with one warm
cell per seat first, because a provider's prefix cache only serves requests
that arrive after the first one carrying the prefix has completed.
"""

import json
import os
import threading
import time

from rafiki import ConnectError

from . import pr_gather, pr_review, pr_state, pr_verify
from .analysis import hallucination_checks
from .common import LENSES, AtCap, Fail, last_json_block, now_iso, render_prompt
from .presets import live_seat, seat_kind
from .rafiki_io import final_answer
from .runs import next_attempt, run_cwd, with_tools
from .stages import AT_CAP, child_metrics, settled

STAGE = "lite"
CLAIM = "lite.lock"
GENERAL = "general"
DEFAULT_TIMEOUT = 1800
DEFAULT_PARALLEL = 6
GATHER_TIMEOUT = 1800
POLL = 3.0
# The pack may take this share of the smallest fork consumer's window: the
# fork's own task, tool calls and answer need the rest.
PACK_FRACTION = pr_gather.CONTEXT_FRACTION
# What the consolidator's pooled findings may add on top of the pack. Its
# window is charged for this before the smallest window is taken, so a pack
# sized for the panel never leaves the consolidator with no room to read.
CONSOLIDATE_RESERVE_TOKENS = 40_000
# The budget stated to the gatherer when no fork consumer reports a window.
# Stating none would let a 1M-window gatherer read past every panel seat.
FALLBACK_PACK_TOKENS = 100_000
INDEPENDENT_FILES = {"pr": ["packet/intent.md", "packet/diff.patch"], "design": ["packet/intent.md"]}
MODES = ("pr", "design")
_WINDOW_KEYS = ("context_window",)


# --- configuration --------------------------------------------------------------


def lite_config(rdir):
    """The run's lite seats from project/pr.json: {"gather", "panel", "consolidate"}.
    Validated whole before anything spawns: a missing seat would otherwise
    surface as a spawn failure after the gatherer has been paid for."""
    f = rdir / "project" / "pr.json"
    doc = pr_review.read_json(f, "the run's seat config (start the run again)")
    cfg = doc.get("lite")
    if not isinstance(cfg, dict):
        raise Fail(f"{f}: no `lite` section; name its gather, panel and consolidate seats")
    gather, consolidate, panel = cfg.get("gather"), cfg.get("consolidate"), cfg.get("panel")
    if not isinstance(gather, str) or not gather:
        raise Fail(f"{f}: lite.gather must name a seat")
    if not isinstance(consolidate, str) or not consolidate:
        raise Fail(f"{f}: lite.consolidate must name a seat")
    if not isinstance(panel, list) or not panel or not all(isinstance(p, str) and p for p in panel):
        raise Fail(f"{f}: lite.panel must be a non-empty list of seat names")
    if len(set(panel)) != len(panel):
        raise Fail(f"{f}: lite.panel lists a seat twice; a seat is one column of cells")
    return {"gather": gather, "panel": list(panel), "consolidate": consolidate,
            "lens_dir": doc.get("lens_dir")}


def strip_route(model):
    """A model id without its routing spec: `x[only=y]` is catalogued as `x`."""
    return str(model or "").split("[", 1)[0]


def model_windows(rk):
    """{model id: context window} from the daemon's catalog. A row that carries
    no window is left out: an unknown window is not a small one."""
    fn = getattr(rk.c, "list_models", None)
    if not callable(fn):
        return {}
    out = {}
    for row in fn() or []:
        mid = row.get("id") if isinstance(row, dict) else getattr(row, "id", None)
        win = row.get("context_window") if isinstance(row, dict) else getattr(row, "context_window", None)
        if isinstance(mid, str) and isinstance(win, int) and win > 0:
            out[mid] = win
    return out


def seat_specs(rk, cfg):
    """{seat: {kind, model, window, version}} read live, once, for every seat the
    stage will spawn. Kind is seat policy and decides delivery; it is read here,
    never assumed, so a preset edit between runs takes effect on the next run."""
    windows = model_windows(rk)
    out = {}
    for name in dict.fromkeys([cfg["gather"], *cfg["panel"], cfg["consolidate"]]):
        kind = seat_kind(rk, name)  # Fails when the preset is missing
        live = live_seat(rk, name)
        spec = (live.get("spec") or live) if isinstance(live, dict) else live
        get = spec.get if isinstance(spec, dict) else lambda k, s=spec: getattr(s, k, None)
        model = get("model")
        win = get("context_window")
        out[name] = {"kind": kind, "model": model,
                     "window": win if isinstance(win, int) and win > 0 else windows.get(strip_route(model)),
                     "version": live.get("version") if isinstance(live, dict) else getattr(live, "version", None)}
    if out[cfg["gather"]]["kind"] != "fundi":
        raise Fail(f"lite.gather {cfg['gather']} is kind {out[cfg['gather']]['kind']}; the gatherer is "
                   f"forked from, which needs a fundi conversation")
    for name, s in out.items():
        if s["kind"] not in ("fundi", "claude"):
            raise Fail(f"preset {name}: kind {s['kind']!r} is not a review seat kind")
    return out


def focus_list(lenses, only=None):
    """[{name, prompt}] with `general` first, then every resolved lens. `only`
    narrows the set for an experiment; a name it gives that the run does not
    resolve is refused rather than ignored."""
    by_name = {}
    for l in lenses:
        name = str(l.get("name") or "")
        if not name:
            raise Fail("packet/lenses.json: a lens has no name")
        by_name[name] = str(l.get("prompt") or "")
    out = [{"name": GENERAL, "prompt": ""}] + [{"name": n, "prompt": p} for n, p in by_name.items()]
    if only:
        unknown = [n for n in only if n != GENERAL and n not in by_name]
        if unknown:
            raise Fail(f"--focus names {', '.join(unknown)}, which this run does not resolve "
                       f"(resolved: {', '.join([GENERAL, *by_name])})")
        out = [f for f in out if f["name"] in only]
    return out


def plan_cells(panel, specs, focuses):
    """Every cell of the panel, in seat-major order. A fundi seat is one column
    of fork cells, one per focus; a claude seat is ONE independent cell over the
    whole change with every focus on its checklist. Narrowing the independent
    seat per focus would correlate the control with the forks and pay one full
    exploration per focus."""
    cells = []
    lens_names = [f["name"] for f in focuses if f["name"] != GENERAL]
    for seat in panel:
        short = pr_review.slug(seat.rsplit(":", 1)[-1])
        if specs[seat]["kind"] == "claude":
            cells.append({"key": f"{short}--whole", "seat": seat, "delivery": "independent",
                          "focus": GENERAL, "lens": None, "always": lens_names})
            continue
        for f in focuses:
            general = f["name"] == GENERAL
            cells.append({"key": f"{short}--{pr_review.slug(f['name'])}", "seat": seat, "delivery": "fork",
                          "focus": f["name"], "lens": None if general else f["name"],
                          "lens_prompt": f["prompt"], "always": lens_names if general else [f["name"]]})
    return cells


def required_children(cells, parallel):
    """The live-children grant the stage needs: every independent cell runs from
    the start, the gatherer alongside them, then at most `parallel` fork cells at
    once, and the consolidator alone at the end."""
    independent = sum(1 for c in cells if c["delivery"] == "independent")
    return independent + max(1, parallel)


def pack_budget(specs, cfg, cells):
    """(tokens the gatherer may read, {consumer seat: its usable window}).
    The budget is PACK_FRACTION of the smallest window among the seats that will
    fork the pack, the consolidator's charged for its findings reserve. A seat
    whose window nothing reports is left out of the minimum (and recorded by the
    caller); with no window known at all the fallback budget applies."""
    consumers = {c["seat"] for c in cells if c["delivery"] == "fork"}
    if specs[cfg["consolidate"]]["kind"] == "fundi":
        consumers.add(cfg["consolidate"])
    usable = {}
    for seat in sorted(consumers):
        win = specs[seat]["window"]
        if win:
            usable[seat] = win - (CONSOLIDATE_RESERVE_TOKENS if seat == cfg["consolidate"] else 0)
    if not usable:
        return FALLBACK_PACK_TOKENS, usable
    return int(min(usable.values()) * PACK_FRACTION), usable


def pack_tokens(conv):
    """The gatherer's context at its last request: input + cache reads of the
    last turn that reported them, plus that turn's output. None when no turn
    reported usage, which is unknown, not empty."""
    for t in reversed((conv or {}).get("turns") or []):
        if t.get("input_tokens") is not None:
            return int(t["input_tokens"]) + int(t.get("cache_read_tokens") or 0) + int(t.get("output_tokens") or 0)
    return None


# --- prompts --------------------------------------------------------------------


def preamble(delivery, mode):
    if delivery == "fork":
        return render_prompt("lite-preamble-fork.md").strip()
    diff_line = "`packet/diff.patch` is the change, " if mode == "pr" else ""
    return render_prompt("lite-preamble-independent.md", diff_line=diff_line).strip()


def cell_instruction(rdir, cell, mode):
    """The panel task, preceded by the cell's delivery preamble. A lens cell's
    checklist is its one lens with the lens's own text; a general cell's is every
    resolved lens by name, as the full pipeline's panel gets."""
    if cell["lens"]:
        checklist = f"- {cell['lens']}\n\n{cell['lens_prompt'].strip()}"
    else:
        checklist = "\n".join(f"- {n}" for n in cell["always"]) or "(no lenses resolved for this run)"
    body = render_prompt("task-pr-panel.md", mode=mode, checklist=checklist,
                         mode_rules=pr_review.MODE_RULES[mode])
    return with_tools(rdir, preamble(cell["delivery"], mode) + "\n\n" + body)


def gather_instruction(rdir, run, intent, budget):
    mode = run["mode"]
    if mode == "pr":
        diff_line = "`packet/diff.patch` is the change, "
        first = "`packet/intent.md` and `packet/diff.patch` are already read at the top of this conversation"
        base_line = ", `base_tree/` the tree before it"
    else:
        diff_line, base_line = "", ""
        first = ("the design in `packet/intent.md` is already read at the top of this conversation; the change "
                 "to gather for is the code its claims rest on")
    return with_tools(rdir, render_prompt(
        "task-lite-gather.md", intent=intent,
        changed_files="\n".join(run.get("files_changed") or []) or "(no changed files in this run)",
        diff_line=diff_line, base_line=base_line, first_read=first, budget_tokens=str(budget)))


# --- consolidation --------------------------------------------------------------


def consolidation_input(clusters, tree, delivery_of):
    """What the consolidator judges: every distinct finding id, grouped by bucket,
    with its mechanical checks and the delivery of each seat that raised it. The
    raising seat's NAME is left out: the consolidator judges the claim, not the
    model behind it."""
    out = []
    for cl in clusters:
        units = []
        for u in pr_verify.member_units(cl):
            m = u["member"]
            units.append({
                "id": u["id"], "title": m.get("title"), "file": m.get("file"), "line": m.get("line"),
                "severity": m.get("severity"), "category": m.get("category"), "lens": m.get("lens"),
                "claim": m.get("claim"), "evidence": m.get("evidence"), "repro": m.get("repro"),
                "checks": hallucination_checks(tree, m),
                "delivery": sorted({delivery_of.get(s, "unknown") for s in u["raised_by"]}),
                "raised_by_count": len(u["raised_by"]),
            })
        out.append({"bucket": cl["key"], "findings": units})
    return out


def consolidator_votes(doc, seat):
    """{finding id: vote} from the consolidator's answer, in verify-pr's vote
    shape. `uncertain` is a vote with no boolean (so the issue is incomplete:
    unsettled, never dismissed) marked uncertain so the report can say which."""
    votes, notes = {}, []
    for i, v in enumerate(pr_review.as_list(doc or {}, "verdicts")):
        if not isinstance(v, dict) or not v.get("id"):
            notes.append(f"verdicts[{i}]: not an object with an id; dropped")
            continue
        word = str(v.get("verdict") or "").strip().lower()
        if word not in ("confirmed", "refuted", "uncertain"):
            notes.append(f"verdicts[{i}] {v.get('id')}: verdict {v.get('verdict')!r} is not "
                         f"confirmed/refuted/uncertain; the finding stays incomplete")
        vid = str(v["id"])
        if vid in votes:
            notes.append(f"verdicts[{i}] {vid}: a second verdict for one id; the first is kept")
            continue
        votes[vid] = {"id": vid, "seat": seat,
                      "refuted": True if word == "refuted" else False if word == "confirmed" else None,
                      "uncertain": word == "uncertain",
                      "reasoning": str(v.get("reasoning") or ""),
                      "severity_adjustment": v.get("severity_adjustment") or None}
    return votes, notes


def verdict_rows(clusters, votes, seat):
    """verify-pr's verdicts.jsonl rows, from one consolidator's votes. A finding
    the consolidator did not answer has no vote and is incomplete."""
    rows = []
    for cl in clusters:
        members, all_votes = [], []
        for u in pr_verify.member_units(cl):
            m = u["member"]
            vote = votes.get(u["id"])
            mv = [vote] if vote else []
            members.append({"id": u["id"], "verdict": pr_verify.verdict(mv), "seats": [seat] if vote else [],
                            "title": str(m.get("title") or ""), "file": str(m.get("file") or ""),
                            "line": m.get("line"), "claim": str(m.get("claim") or ""),
                            "raised_by": u["raised_by"], "source": str(m.get("source") or "")})
            all_votes += mv
        rep = cl["representative"]
        rows.append({"key": cl["key"], "title": str(rep.get("title") or ""), "file": str(rep.get("file") or ""),
                     "line": rep.get("line"), "claim": str(rep.get("claim") or ""),
                     "verdict": pr_verify.cluster_verdict([m["verdict"] for m in members]),
                     "votes": all_votes, "members": members,
                     "raised_by": cl["raised_by"], "source": cl["source"]})
    return rows


# --- the stage ------------------------------------------------------------------


def claim_lite(rdir, run):
    """Take the run for a lite review (O_EXCL), then check and move its status
    under the claim. A fresh run is `gathering`; lite builds its own packet, so
    a run another stage has gathered is refused rather than reviewed twice."""
    path = rdir / CLAIM
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        raise Fail(f"run {run['run_id']}: a lite review is already in progress; if that process is gone, "
                   f"remove {path}") from None
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"stage": STAGE, "pid": os.getpid(), "at": now_iso()}, sort_keys=True) + "\n")
    try:
        state = pr_state.load_state(rdir)
        if state.get("status") != "gathering":
            raise Fail(f"run {run['run_id']} is {state.get('status')}; lite reviews a fresh run (start one "
                       f"with `run new`): it writes its own packet and never reuses another stage's")
        state["status"] = "lite-running"
        pr_state.save_state(rdir, state)
    except BaseException:
        release_lite(rdir)
        raise
    return state


def release_lite(rdir):
    try:
        os.unlink(rdir / CLAIM)
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"lite: could not release {rdir / CLAIM}: {e}")


class StopWatch:
    """The script child's stop signal. A stage that never reads its inbox is
    only killable, not stoppable: Receive's Stop row is how the daemon asks it
    to wind down, so a thread watches for it and the scheduler checks it."""

    def __init__(self, rk):
        self.stopped = threading.Event()
        fn = getattr(rk.c, "receive", None)
        if callable(fn):
            threading.Thread(target=self._watch, args=(fn,), daemon=True).start()

    def _watch(self, receive):
        try:
            for msg in receive():
                if getattr(msg, "stop", None) is not None:
                    break
        except Exception as e:  # noqa: BLE001 - any end of the stream is a stop
            print(f"lite: inbox stream ended ({e}); treating it as a stop")
        self.stopped.set()

    def check(self):
        if self.stopped.is_set():
            raise Fail("stop requested")


def friction(rdir, seat, kind, note, cell=None):
    entry = {"seat": seat or "", "kind": kind, "note": note}
    if cell:
        entry["cell"] = cell
    pr_state.append_friction(rdir, entry)


@pr_state.friction_stage(STAGE)
def stage_lite(rk, home, args):
    rdir, run = home.load_run(args.run)
    mode = run.get("mode")
    if mode not in MODES:
        raise Fail(f"lite reviews {' and '.join(MODES)} runs; this run is {mode!r} (use the full pipeline)")
    cfg = lite_config(rdir)
    specs = seat_specs(rk, cfg)
    parallel = max(1, int(getattr(args, "parallel", DEFAULT_PARALLEL) or DEFAULT_PARALLEL))
    timeout = int(getattr(args, "timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)

    state = claim_lite(rdir, run)
    spawned = {}
    try:
        result = _run(rk, home, args, rdir, run, cfg, specs, parallel, timeout, state, spawned)
    except BaseException as e:
        for cid in spawned.values():
            rk.stop(cid)
        try:
            friction(rdir, "", "stage-error", f"lite {run['run_id']}: {e}")
            st = pr_state.load_state(rdir)
            st["status"] = "lite-failed"
            st["lite_error"] = {"at": now_iso(), "error": str(e), "type": type(e).__name__}
            pr_state.save_state(rdir, st)
        except Fail as e2:
            print(f"lite: could not record the failure: {e2}")
        raise
    finally:
        release_lite(rdir)
    return result


def _run(rk, home, args, rdir, run, cfg, specs, parallel, timeout, state, spawned):
    started = time.time()
    phases = {}  # name -> (start, end), wall clock; per-child metrics cannot show overlap
    stop = StopWatch(rk)
    intent, _diff, selected = pr_gather.packet_basics(home, rdir, run, cfg["lens_dir"], lens_generic=LENSES)
    lenses = [{"name": l.name, "prompt": l.prompt} for l in selected]
    focuses = focus_list(lenses, getattr(args, "focus", None))
    cells = {c["key"]: c for c in plan_cells(cfg["panel"], specs, focuses)}

    need = required_children(cells.values(), parallel)
    budget, usable = pack_budget(specs, cfg, cells.values())
    for seat, s in specs.items():
        if s["kind"] == "fundi" and seat != cfg["gather"] and not s["window"]:
            friction(rdir, seat, "context-window",
                     "no context window reported; the pack is not checked against this seat")

    adir = next_attempt(rdir / "review")
    pr_state.friction_attempt(adir.name)
    tree = run_cwd(run)
    labels_base = {"review/run": run["run_id"], "review/stage": STAGE, "review/attempt": adir.name}
    manifest = {"mode": run["mode"], "stage": STAGE, "target": run["target"], "base": run.get("base"),
                "head": run["head"], "pack_budget_tokens": budget, "pack_tokens": None,
                "lenses": [{"name": l.name, "sha256": l.sha256, "always": l.triggers == "always"} for l in selected],
                "seats": {n: {"kind": s["kind"], "model": s["model"], "context_window": s["window"],
                              "version": s["version"]} for n, s in specs.items()},
                "cells": {k: {"seat": c["seat"], "delivery": c["delivery"], "focus": c["focus"]}
                          for k, c in cells.items()}}
    write_manifest(rdir, manifest)
    print(f"lite {adir.name}: {len(cells)} cells over {len(focuses)} focuses, pack budget {budget} tokens, "
          f"needs max_children {need}")

    def spawn_independent(key, seat, instruction, labels):
        cdir = adir / key
        cdir.mkdir(exist_ok=True)
        (cdir / "task.md").write_text(instruction)
        cid, sid, delivery = rk.spawn_seat(f"lite-{run['run_id']}-{key}"[:120], preset=seat, cwd=rdir,
                                           packet_files=INDEPENDENT_FILES[run["mode"]],
                                           instruction=instruction, labels=labels)
        spawned[key] = cid
        return cid, sid, delivery

    def spawn_fork(key, seat, source, instruction, labels):
        cdir = adir / key
        cdir.mkdir(exist_ok=True)
        (cdir / "task.md").write_text(instruction)
        cid, sid = rk.spawn_fork(f"lite-{run['run_id']}-{key}"[:120], preset=seat, source=source, cwd=rdir,
                                 prompt=instruction, labels=labels)
        spawned[key] = cid
        return cid, sid

    def cell_labels(c):
        out = {**labels_base, "review/seat": c["seat"], "review/cell": c["key"], "review/delivery": c["delivery"]}
        if c["lens"]:
            out["review/lens"] = c["lens"]
        return out

    # Phase 1: the gatherer and every independent cell start together.
    gather_seat = cfg["gather"]
    gcid = None
    try:
        gcid, _, _ = spawn_independent("gather", gather_seat, gather_instruction(rdir, run, intent, budget),
                                       {**labels_base, "review/seat": gather_seat, "review/cell": "gather"})
    except AtCap as e:
        raise Fail(f"spawn refused at the live-children cap: this stage needs max_children >= {need}; "
                   f"launch with --max-children {need} ({e})") from e
    except Fail as e:
        friction(rdir, gather_seat, "spawn-error", str(e), "gather")
    gather_started = time.time()
    running, records = {}, {}
    for c in cells.values():
        if c["delivery"] != "independent":
            continue
        try:
            cid, sid, _ = spawn_independent(c["key"], c["seat"], cell_instruction(rdir, c, run["mode"]),
                                                   cell_labels(c))
        except AtCap as e:
            raise Fail(f"spawn refused at the live-children cap: this stage needs max_children >= {need} "
                       f"(independent cells + --parallel); launch with --max-children {need} ({e})") from e
        except Fail as e:
            friction(rdir, c["seat"], "spawn-error", str(e), c["key"])
            continue
        c.update({"child_id": cid, "conversation_id": sid, "spawned_at": time.time()})
        running[cid] = c

    def collect_ready():
        for cid in list(running):
            c = running[cid]
            got = settled(rk, cid, timed_out=time.time() - c["spawned_at"] > timeout)
            if got is None:
                continue
            records[c["key"]] = collect(rk, adir, rdir, tree, run["mode"], c, *got)
            del running[cid]

    # Phase 2: wait for the gatherer, collecting independent cells meanwhile.
    gather_ok, pack = False, None
    if gcid:
        while True:
            stop.check()
            got = settled(rk, gcid, timed_out=time.time() - gather_started > GATHER_TIMEOUT)
            if got is not None:
                break
            collect_ready()
            time.sleep(POLL)
        info, conv = got
        gdir = adir / "gather"
        m = child_metrics(info, conv, set())
        m["wall_s"] = round(time.time() - gather_started, 1)
        (gdir / "child.json").write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")
        answer = final_answer(conv) if conv else None
        (gdir / "raw.md").write_text(answer or "")
        rk.stop(gcid)  # forks read its stored nodes; the live child only holds a slot
        phases["gather"] = (gather_started, time.time())
        pack = pack_tokens(conv)
        if not answer:
            friction(rdir, gather_seat, "gather-failed",
                     f"no final answer (status {info.get('status')}); no fork cell is spawned", "gather")
        else:
            gather_ok = True
            if pack is not None and pack > budget:
                friction(rdir, gather_seat, "pack-over-budget",
                         f"the pack is {pack} tokens against a budget of {budget}", "gather")
    manifest["pack_tokens"] = pack
    write_manifest(rdir, manifest)

    # Which fork seats can hold the pack. A seat the pack does not fit is
    # skipped whole: trimming would elide the middle of the pack silently.
    fork_seats = list(dict.fromkeys(c["seat"] for c in cells.values() if c["delivery"] == "fork"))
    usable_seats = []
    for seat in fork_seats:
        if not gather_ok:
            friction(rdir, seat, "no-pack", "the gatherer produced no pack; this seat's fork cells are not spawned")
            continue
        win = usable.get(seat)
        if pack is not None and win and pack > win * PACK_FRACTION:
            friction(rdir, seat, "pack-exceeds-window",
                     f"the pack is {pack} tokens; {PACK_FRACTION:.0%} of this seat's {win}-token window is "
                     f"{int(win * PACK_FRACTION)}; its fork cells are not spawned")
            continue
        usable_seats.append(seat)

    # Phase 3: fork cells, seat-major, one warm cell per seat before the rest.
    queues = {s: [c for c in cells.values() if c["seat"] == s and c["delivery"] == "fork"] for s in usable_seats}
    warm = {}  # seat -> "pending" | "warm" | "failed"
    forks_running = {}

    def start_fork(c):
        try:
            cid, sid = spawn_fork(c["key"], c["seat"], gcid, cell_instruction(rdir, c, run["mode"]), cell_labels(c))
        except AtCap:
            return AT_CAP
        except Fail as e:
            friction(rdir, c["seat"], "spawn-error", str(e), c["key"])
            return None
        c.update({"child_id": cid, "conversation_id": sid, "spawned_at": time.time()})
        running[cid] = c
        forks_running[cid] = c
        return cid

    def next_fork():
        """Seat-major: the earliest seat whose cells may go. A seat's warm cell
        goes first; its other cells wait until the warm cell's first turn has
        completed, so they arrive at a provider cache that already holds the
        pack (the ChatGPT backend serves a prefix only once the request that
        carried it is done; one still streaming leaves every sibling cold)."""
        for s in usable_seats:
            q = queues[s]
            if not q:
                continue
            state_ = warm.get(s)
            if state_ is None:
                return s, q[0]
            if state_ == "warm":
                return s, q[0]
        return None, None

    def update_warm():
        for s in usable_seats:
            if warm.get(s) != "pending":
                continue
            wc = next(c for c in cells.values() if c.get("warm_for") == s)
            if wc["key"] in records:
                if records[wc["key"]]["parse_error"] is None or turn_landed(rk, wc):
                    warm[s] = "warm"
                else:
                    warm[s] = "failed"
                    friction(rdir, s, "warm-failed",
                             f"the warm cell {wc['key']} ended before its first turn landed; this seat's "
                             f"other {len(queues[s])} cell(s) are not spawned", wc["key"])
                    queues[s] = []
            elif turn_landed(rk, wc):
                warm[s] = "warm"

    while any(queues.values()) or running:
        stop.check()
        update_warm()
        while len(forks_running) < parallel:
            s, c = next_fork()
            if c is None:
                break
            got = start_fork(c)
            if got is AT_CAP:
                if not running:
                    raise Fail(f"spawn refused at the live-children cap with nothing of ours running; "
                               f"this stage needs max_children >= {need}")
                break
            queues[s].pop(0)
            if s in warm:
                continue
            if got is None:
                # The seat's first spawn failed, and every other cell of it would
                # fail the same way: the seat is wrong, not the cell.
                warm[s] = "failed"
                friction(rdir, s, "warm-failed", f"the warm cell {c['key']} did not spawn; this seat's other "
                                                 f"{len(queues[s])} cell(s) are not spawned", c["key"])
                queues[s] = []
                continue
            warm[s] = "pending"
            c["warm_for"] = s
        before = set(running)
        collect_ready()
        for cid in before - set(running):
            forks_running.pop(cid, None)
        if running or any(queues.values()):
            time.sleep(POLL)
    update_warm()

    phases["cells"] = (phases.get("gather", (started, started))[1], time.time())
    if not records:
        raise Fail(f"no panel cell answered; see {adir} and friction.jsonl")

    # Pool, exactly as the review stage pools.
    pooled = {"findings": [], "checklist": []}
    for key in sorted(records):
        for field in pooled:
            pooled[field] += records[key][field]
    pr_review.write_jsonl(adir / "candidates.jsonl", pooled["findings"])
    pr_review.write_jsonl(adir / "checklist.jsonl", pooled["checklist"])

    # Phase 4: one consolidator over the clustered findings.
    vdir = next_attempt(rdir / "verify")
    notes = []
    candidates = [pr_verify.normalize(f, notes) for f in pooled["findings"]]
    pr_verify.record_line_notes(rdir, notes)
    clusters = pr_verify.cluster(candidates)
    delivery_of = {c["seat"]: c["delivery"] for c in cells.values()}
    cseat = cfg["consolidate"]
    votes = {}
    if clusters:
        stop.check()
        consolidate_started = time.time()
        votes = consolidate(rk, rdir, run, vdir, tree, cseat, specs, gcid if gather_ok else None,
                            consolidation_input(clusters, tree, delivery_of), usable, pack, timeout,
                            {**labels_base, "review/seat": cseat, "review/cell": "consolidate"}, spawned)
        phases["consolidate"] = (consolidate_started, time.time())
    rows = verdict_rows(clusters, votes, cseat)
    pr_review.write_jsonl(vdir / "verdicts.jsonl", rows)
    pr_review.write_jsonl(vdir / "claims.jsonl", [])
    counts = {k: 0 for k in ("confirmed", "contested", "refuted", "incomplete")}
    for row in rows:
        for m in row["members"]:
            counts[m["verdict"]] += 1
    (vdir / "state.json").write_text(json.dumps(
        {"stage": STAGE, "source": str((adir / "candidates.jsonl").relative_to(rdir)), "review": adir.name,
         "consolidator": cseat, "delivery": delivery_of}, indent=1, sort_keys=True) + "\n")

    seat_of = {k: c["seat"] for k, c in cells.items()}
    seat_of.update({"gather": cfg["gather"], "consolidate": cseat})
    stats = lite_stats(adir, vdir, specs, seat_of, delivery_of, phases, started)
    (adir / "stats.json").write_text(json.dumps(stats, indent=1, sort_keys=True) + "\n")
    print(stats_table(adir.name, stats))

    st = pr_state.load_state(rdir)
    st["status"] = "done"
    st["lite"] = {"review": adir.name, "verify": vdir.name, "cells": len(records), "candidates": len(candidates),
                  "pack_tokens": pack, "pack_budget_tokens": budget}
    st["verify"] = {"attempt": vdir.name, "review": adir.name, "claims": 0, "clusters": len(rows),
                    "members": sum(len(r["members"]) for r in rows), **counts}
    pr_state.save_state(rdir, st)
    print(f"lite: {len(records)} cells answered, {len(candidates)} findings in {len(rows)} buckets; "
          + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return {"stage": STAGE, "run": run["run_id"], "review": adir.name, "verify": vdir.name,
            "cells": len(records), "candidates": len(candidates), "pack_tokens": pack, **counts,
            "stats": stats_summary(stats)}


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def lite_stats(adir, vdir, specs, seat_of, delivery_of, phases, started):
    """Cost, cache and timing per seat, from the child.json every collected
    child wrote, plus the stage's own phase clock. A number a child did not
    report is counted in `*_unreported`, never summed as zero. Cache WRITES are
    not here: the export the metrics are read from does not carry them, so
    `cache_pct` is cache reads over uncached input plus cache reads, and a seat
    that writes its cache (a claude seat's own turns) reads higher than its
    provider's own hit rate."""
    paths = sorted(adir.glob("*/child.json")) + sorted(vdir.glob("consolidate/child.json"))
    seats = {}
    for p in paths:
        try:
            m = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        # The stage knows which seat each child directory is; a child's preset
        # label is only the fallback for a directory it did not name.
        seat = str(seat_of.get(p.parent.name) or m.get("preset") or p.parent.name)
        role = "consolidate" if p.parent.name == "consolidate" else "gather" if p.parent.name == "gather" else "cell"
        row = seats.setdefault(seat, {
            "seat": seat, "model": (specs.get(seat) or {}).get("model") or m.get("model"),
            "delivery": "-" if role != "cell" else delivery_of.get(seat, "-"),
            "children": 0, "turns": 0, "input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0,
            "tokens_unreported": 0, "cost_usd": 0.0, "cost_unreported": 0, "wall_s": []})
        row["children"] += 1
        row["turns"] += int(m.get("model_turns") or 0)
        if _num(m.get("input_tokens")) is None:
            row["tokens_unreported"] += 1
        else:
            row["input_tokens"] += m["input_tokens"]
            row["cache_read_tokens"] += _num(m.get("cache_read_tokens")) or 0
            row["output_tokens"] += _num(m.get("output_tokens")) or 0
        if _num(m.get("cost_usd")) is None:
            row["cost_unreported"] += 1
        else:
            row["cost_usd"] += m["cost_usd"]
        if _num(m.get("wall_s")) is not None:
            row["wall_s"].append(m["wall_s"])
    for row in seats.values():
        prompt = row["input_tokens"] + row["cache_read_tokens"]
        row["cache_pct"] = round(100 * row["cache_read_tokens"] / prompt) if prompt else None
        walls = sorted(row.pop("wall_s"))
        row["wall_p50_s"] = walls[len(walls) // 2] if walls else None
        row["wall_max_s"] = walls[-1] if walls else None
        row["cost_usd"] = round(row["cost_usd"], 4)
    prompt = sum(r["input_tokens"] + r["cache_read_tokens"] for r in seats.values())
    return {
        "wall_s": round(time.time() - started, 1),
        "phases_s": {k: round(e - s, 1) for k, (s, e) in phases.items()},
        "cost_usd": round(sum(r["cost_usd"] for r in seats.values()), 4),
        "cost_unreported": sum(r["cost_unreported"] for r in seats.values()),
        "cache_pct": round(100 * sum(r["cache_read_tokens"] for r in seats.values()) / prompt) if prompt else None,
        "seats": [seats[k] for k in sorted(seats)],
    }


def _k(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _secs(v):
    if v is None:
        return "-"
    v = round(v)
    return f"{v // 60}m{v % 60:02d}s" if v >= 60 else f"{v}s"


def stats_table(attempt, stats):
    """The stats as the plain-text table the stage prints when it finishes."""
    phases = " · ".join(f"{k} {_secs(v)}" for k, v in stats["phases_s"].items())
    unrep = f" (+{stats['cost_unreported']} unreported)" if stats["cost_unreported"] else ""
    cache = "-" if stats["cache_pct"] is None else f"{stats['cache_pct']}%"
    title = f"lite {attempt}: {_secs(stats['wall_s'])} ({phases}), ${stats['cost_usd']:.2f}{unrep}, cache {cache}"
    header = (f"{'seat':26} {'deliv':11} {'n':>3} {'turns':>5} {'uncached':>9} {'cache_rd':>9} {'cache':>5} "
              f"{'out':>7} {'cost':>7} {'wall p50/max':>13}")
    lines = [title, header]
    for r in stats["seats"]:
        pct = "-" if r["cache_pct"] is None else f"{r['cache_pct']}%"
        lines.append(f"{r['seat']:26} {r['delivery']:11} {r['children']:>3} {r['turns']:>5} "
                     f"{_k(r['input_tokens']):>9} {_k(r['cache_read_tokens']):>9} {pct:>5} "
                     f"{_k(r['output_tokens']):>7} {'$' + format(r['cost_usd'], '.2f'):>7} "
                     f"{_secs(r['wall_p50_s']) + '/' + _secs(r['wall_max_s']):>13}")
    return "\n".join(lines)


def stats_summary(stats):
    """The part of the stats that rides the stage's result: small enough for
    the result cap whatever the panel size, the full record is stats.json."""
    return {"wall_s": stats["wall_s"], "phases_s": stats["phases_s"], "cost_usd": stats["cost_usd"],
            "cost_unreported": stats["cost_unreported"], "cache_pct": stats["cache_pct"],
            "seats": {r["seat"]: {"n": r["children"], "cache_pct": r["cache_pct"],
                                  "cost_usd": round(r["cost_usd"], 3), "wall_max_s": r["wall_max_s"]}
                      for r in stats["seats"]}}


def write_manifest(rdir, manifest):
    (rdir / "packet" / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")


def turn_landed(rk, cell):
    """Whether a cell's first model turn has COMPLETED: its export holds an
    assistant turn with reported output. Cost is no signal: it turns positive
    while the turn is still streaming, before a provider that caches on
    completion (the ChatGPT backend) can serve the prefix. A fork's export holds
    only its own turns, so this poll stays small however large the pack is."""
    try:
        conv = rk.export(cell["child_id"])
    except ConnectError as e:
        if e.code == "not_found":
            return False  # nothing captured yet
        raise
    return any(t.get("role") == "assistant" and t.get("output_tokens") is not None
               for t in (conv or {}).get("turns") or [])


def collect(rk, adir, rdir, tree, mode, cell, info, conv):
    """One cell through the review stage's collector: the same files, the same
    friction, the same pooled shape, with the cell's lens list as its checklist."""
    job = {"key": cell["key"], "seat": cell["seat"], "kind": "panel", "lens": cell["lens"],
           "always": cell["always"], "source": f"lite:{cell['focus']}", "delivery": cell["delivery"],
           "child_id": cell["child_id"], "spawned_at": cell["spawned_at"]}
    rec = pr_review.collect_child(rk, adir, rdir, tree, mode, job, info, conv, [])
    print(f"  {cell['key']}: {len(rec['findings'])} findings{', PARSE ERROR' if rec['parse_error'] else ''}")
    return rec


def consolidate(rk, rdir, run, vdir, tree, seat, specs, source, findings, usable, pack, timeout, labels, spawned):
    """Spawn the consolidator and return its votes. A fundi consolidator forks the
    gatherer when there is a pack it can hold, so it judges from the same reading
    the panel had; otherwise it reads the change itself. Its failure leaves every
    finding without a vote (incomplete), never dropped."""
    mode = run["mode"]
    fork = source is not None and specs[seat]["kind"] == "fundi"
    if fork and pack is not None and usable.get(seat) and pack > usable[seat] * PACK_FRACTION:
        friction(rdir, seat, "pack-exceeds-window", "the consolidator cannot hold the pack; it reads independently")
        fork = False
    instruction = with_tools(rdir, render_prompt(
        "task-lite-consolidate.md", preamble=preamble("fork" if fork else "independent", mode), mode=mode,
        findings=json.dumps(findings, indent=1, sort_keys=True)))
    cdir = vdir / "consolidate"
    cdir.mkdir(exist_ok=True)
    (cdir / "task.md").write_text(instruction)
    name = f"lite-{run['run_id']}-consolidate"
    try:
        if fork:
            cid, _ = rk.spawn_fork(name, preset=seat, source=source, cwd=rdir, prompt=instruction, labels=labels)
        else:
            cid, _, _ = rk.spawn_seat(name, preset=seat, cwd=rdir, packet_files=INDEPENDENT_FILES[mode],
                                      instruction=instruction, labels=labels)
    except (Fail, AtCap) as e:
        friction(rdir, seat, "spawn-error", str(e), "consolidate")
        return {}
    spawned["consolidate"] = cid
    started = time.time()
    while True:
        got = settled(rk, cid, timed_out=time.time() - started > timeout)
        if got is not None:
            break
        time.sleep(POLL)
    info, conv = got
    m = child_metrics(info, conv, set())
    m["wall_s"] = round(time.time() - started, 1)
    m["delivery"] = "fork" if fork else "independent"
    (cdir / "child.json").write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")
    answer = final_answer(conv) if conv else None
    (cdir / "raw.md").write_text(answer or "")
    rk.stop(cid)
    if answer is None:
        friction(rdir, seat, "parse-error", f"no final answer (status {info.get('status')})", "consolidate")
        return {}
    try:
        doc = last_json_block(answer)
    except (ValueError, json.JSONDecodeError) as e:
        friction(rdir, seat, "parse-error", str(e), "consolidate")
        return {}
    (cdir / "result.json").write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    votes, notes = consolidator_votes(doc, seat)
    for n in notes:
        friction(rdir, seat, "contract-violation", n, "consolidate")
    for n in pr_review.as_list(doc if isinstance(doc, dict) else {}, "process_notes"):
        friction(rdir, seat, "self", str(n), "consolidate")
    known = {u["id"] for cl in findings for u in cl["findings"]}
    for vid in sorted(set(votes) - known):
        friction(rdir, seat, "contract-violation", f"a verdict for unknown id {vid}; ignored", "consolidate")
        del votes[vid]
    for vid in sorted(known - set(votes)):
        friction(rdir, seat, "instruction-breach", f"no verdict for {vid}; it stays incomplete", "consolidate")
    return votes
