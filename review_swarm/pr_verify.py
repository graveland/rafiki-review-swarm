# review-swarm: PR cross-validation - cluster the pooled findings, route each
# issue to fresh verifier seats, and turn checked-nothing claims into findings
"""What a review ends up showing is decided here, so the rules are explicit.

The 30-line bucket groups a row of `verdicts.jsonl` and picks verifier seats; it
never decides what is verified. Every distinct issue in a bucket is verified in
its own right, by fresh children of the panel seats that did not raise it (two
seats of different families when only a finder raised it), refute-by-default, and
a verifier is never told which model raised the claim. One issue's votes can
never stand in for another's.

A bucket is confirmed only if every issue in it is confirmed by all its
verifiers, contested if any issue's votes split (or two issues disagree, which is
the same thing at bucket level: never dropped, always shown), refuted only if
every issue's votes are all-refute, and incomplete if any issue's resolution is
incomplete. All four verdicts get a row; a refuted row stays in the run for
calibration and is not shown.

Every "checked: nothing, because" answer is a claim of the same kind, and a
refuted one always becomes a finding: the verifier's own if it gave one, and
otherwise a synthesized lens-unanswered finding, because a refuted claim that
becomes nothing is a finding dropped between the seats. A "findings" answer is
not a claim: it is checked against the candidates, and one this seat raised no
finding for is an instruction breach, not a free pass past every attack.
"""

import json
import os
import re
import sys
import time

from . import analysis, pr_review
from .common import AtCap, Fail, fanout, last_json_block, now_iso, render_prompt
from .pr_state import (
    append_friction,
    friction_attempt,
    friction_stage,
    load_state,
    save_state,
)
from .presets import live_seat, seat_kind
from .rafiki_io import final_answer
from .runs import next_attempt, run_cwd, run_text, with_tools
from .stages import AT_CAP, child_metrics, model_labels, settled

# The only checklist answer that is not a claim, and the canonical spelling of
# the one that is. Anything else is still a claim (an answer that is neither is
# not an answer), but it is a spelling the prompt does not offer, so it is also
# recorded as friction: nothing falls through, and nothing falls through quietly.
# "findings" is not a claim, but it is not trusted either: verify_run checks it
# against the candidates, and an answer nothing backs is an instruction breach.
FINDINGS = "findings"
NOTHING = "nothing"

# The stage's claim on the run, held for as long as a verify is running. It is a
# file rather than the state.json marker (which stays as the visible mirror)
# because creating it is the atomic step (O_EXCL): two invocations that both read
# `verifying` cannot both claim, while they could both read-then-write the same
# marker.
CLAIM = "verify.lock"

# Job directories and spawn names are observable (a reviewer reads the run), so
# they are built in one place: a bucket key is "file:bucket", which is neither a
# safe directory name nor short.
_SLUG = re.compile(r"[^A-Za-z0-9._-]")


def safe_slug(text):
    return _SLUG.sub("_", str(text))[:40]


def job_key(kind, index, tag, seat):
    """The directory one verifier job writes under verify/NN/. `index` is the
    job's position in the deterministic order, so a job dir means the same thing
    on every run."""
    short = str(seat).split(":")[-1]
    if kind == "claim":
        return f"claim-{index:02d}-{short}"
    return f"{index:02d}-{safe_slug(tag)}-{short}"


def child_name(run_id, attempt, key):
    return f"prverify-{run_id}-{attempt}-{key}"[:120]


def cluster(candidates: list[dict]) -> list[dict]:
    """Pool findings into buckets: same file and 30-line bucket
    (`analysis.issue_key`). Members are sorted by id and buckets by key, so the
    grouping is the same on every run. The representative is the member with the
    longest evidence (ties to the lowest id): the bucket's headline is the
    best-argued claim in it, and it does not drift between runs. Every member
    keeps its own id, so the bucket never loses one behind the other."""
    groups: dict[str, list[dict]] = {}
    for f in candidates:
        groups.setdefault(analysis.issue_key(f), []).append(f)
    out = []
    for key in sorted(groups):
        members = sorted(groups[key], key=lambda f: str(f.get("id") or ""))
        out.append({
            "key": key,
            "members": members,
            "raised_by": sorted({str(m["raised_by"]) for m in members if m.get("raised_by")}),
            "source": sorted({str(m["source"]) for m in members if m.get("source")}),
            "representative": max(members, key=lambda f: len(str(f.get("evidence") or ""))),
        })
    return out


def member_units(cl: dict) -> list[dict]:
    """One verification unit per distinct finding id in a bucket, in id order.

    Two seats that raised the same issue keep one unit (a second copy is not a
    second opinion to check) and the unit carries every seat that raised it, so
    none of them can be picked to check it. The claim put to the verifiers is the
    best-argued of that id's copies.
    """
    by_id: dict[str, list[dict]] = {}
    for m in cl["members"]:
        by_id.setdefault(str(m.get("id") or ""), []).append(m)
    return [{"id": mid, "member": max(by_id[mid], key=lambda f: len(str(f.get("evidence") or ""))),
             "raised_by": sorted({str(m["raised_by"]) for m in by_id[mid] if m.get("raised_by")})}
            for mid in sorted(by_id)]


def assign_verifiers(cl: dict, panel: list[str], verifiers: dict[str, str],
                     families: dict[str, str], *, exclude_family: str | None = None,
                     shortfalls: list | None = None) -> list[str]:
    """The verifier seats for one issue, in panel order.

    A seat that raised the issue never verifies it, and neither does the verifier
    twin of a panel seat that raised it: a claim the review-pr:verify-2 seat
    refuted comes back as raised_by review-pr:verify-2, and comparing panel seat
    names alone would hand that same child its own finding to check. Every seat
    named in `raised` stays out of the fallback list too, so no path out of this
    function returns a seat that raised the issue.

    `exclude_family` is the finder's own family: a finder-raised issue must not be
    checked by the model that raised it. Every place the two-families rule has to
    be weakened is appended to `shortfalls` (the caller records it as friction):
    a weaker check that is recorded is not the same as a silently weaker one.
    """

    def note(msg):
        if shortfalls is not None:
            shortfalls.append(msg)

    def two_families(seats):
        """The first two of `seats` whose families differ, in the given order."""
        chosen, fams = [], set()
        for s in seats:
            fam = families.get(s) or s
            if fam in fams:
                continue
            chosen.append(s)
            fams.add(fam)
            if len(chosen) == 2:
                break
        return chosen

    raised = set(cl.get("raised_by") or [])
    excluded = {s for s in panel if s in raised or verifiers.get(s) in raised}
    other = [s for s in panel if s not in excluded]
    if not other:
        seats = []
    elif raised & (set(panel) | set(verifiers.values())):
        seats = other
    else:
        # Raised by finders only: two seats whose families differ, so the
        # cross-check is not two samples of one model. The finder's own family is
        # the last resort, not a bar: excluding it first is what turned a panel of
        # three into a Fail.
        cands = list(other)
        outside = [s for s in cands
                   if not exclude_family or (families.get(s) or s) != exclude_family]
        if exclude_family and not outside and cands:
            note(f"{cl.get('key')}: every seat left runs the finder's own family `{exclude_family}`")
        chosen = two_families(outside)
        if len(chosen) < 2 and len(outside) != len(cands):
            # One outside seat is not a pair, so the finder's family comes back in:
            # two different families matter more than the exact pair, and the
            # weakening is recorded.
            chosen = two_families(cands)
            if len(chosen) == 2:
                note(f"{cl.get('key')}: only {len(outside)} seat(s) outside the finder's family "
                     f"`{exclude_family}`; the pair includes one of those seats")
        if len(chosen) < 2:
            if len(cands) < 2:
                raise Fail(f"issue {cl.get('key')!r}: finder-raised with {len(cands)} panel seat(s) "
                           f"able to check it; two fresh children are required")
            note(f"{cl.get('key')}: the seats left are all of one family "
                 f"({sorted({families.get(s) or s for s in cands})}); two seats of that family check it")
            chosen = cands[:2]
        seats = chosen
    out, seen = [], set()
    for s in seats:
        v = verifiers.get(s)
        if v and v not in seen and v not in raised:
            seen.add(v)
            out.append(v)
    if not out:
        # No panel seat is left, so the verifier twins stand in: every verifier
        # seat whose panel seat did not raise the issue, and when there is none,
        # every verifier seat that did not itself raise it (the twins are fresh
        # children of the panel, and a unanimous panel is still checked by fresh
        # children rather than taken on trust).
        out = [v for v in sorted(set(verifiers.values())) if v not in raised]
    if not out and not any(verifiers.values()):
        # Only a config with no verifier at all is a failure here: it cannot check
        # anything, and nothing the stage does can fix it. An issue whose every
        # verifier raised it returns empty instead, and the caller records it as
        # incomplete: a dead stage loses every other verdict.
        raise Fail(f"issue {cl.get('key')!r}: pr.json maps no panel seat to a verifier seat "
                   f"(verifiers {verifiers})")
    return out


def verdict(votes: list[dict]) -> str:
    """Refute-by-default, over the votes one issue actually got. A vote that is
    absent or carries no boolean `refuted` makes the issue incomplete: an
    unverified issue is not a dismissed one. Every vote a boolean and all uphold
    -> confirmed; all refute -> refuted; anything in between -> contested.
    """
    if not votes:
        return "incomplete"
    refuted = []
    for v in votes:
        r = v.get("refuted") if isinstance(v, dict) else None
        if not isinstance(r, bool):
            return "incomplete"
        refuted.append(r)
    if all(r is False for r in refuted):
        return "confirmed"
    if all(r is True for r in refuted):
        return "refuted"
    return "contested"


def cluster_verdict(member_verdicts: list[str]) -> str:
    """A bucket's verdict from its issues'. Incomplete first: one issue whose
    resolution never arrived leaves the bucket unproven, whatever its neighbours
    said. Then all-confirmed and all-refuted; everything else (one issue split, or
    two issues disagreeing) is contested and shown with both sides."""
    if any(v == "incomplete" for v in member_verdicts):
        return "incomplete"
    if member_verdicts and all(v == "confirmed" for v in member_verdicts):
        return "confirmed"
    if member_verdicts and all(v == "refuted" for v in member_verdicts):
        return "refuted"
    return "contested"


def vote_tally(votes: list[dict]) -> dict:
    """{refuted value: how many votes}, unparseable votes left out."""
    counts: dict[bool, int] = {True: 0, False: 0}
    for v in votes:
        r = v.get("refuted")
        if isinstance(r, bool):
            counts[r] += 1
    return counts


def minority_side(votes: list[dict]) -> set:
    """The `refuted` value(s) held by strictly fewer votes. An even split has no
    minority side (neither side lost), so nothing is reported for it."""
    counts = {r: n for r, n in vote_tally(votes).items() if n}
    if len(counts) < 2 or min(counts.values()) == max(counts.values()):
        return set()
    low = min(counts.values())
    return {r for r, n in counts.items() if n == low}


def seat_family(kind: str, model: str | None) -> str:
    """The provider family of a seat, from its own model id: the segment after
    "openrouter/" (`openai`, `z-ai`, `~deepseek`), and `anthropic` for a
    claude-kind seat. An unknown model gives "": the caller treats that as a
    family of its own rather than as equal to every other unknown."""
    if kind == "claude":
        return "anthropic"
    m = str(model or "")
    m = m.removeprefix("openrouter/")
    return m.split("/")[0]


def _model_of(live):
    """The model id on a live preset row. The daemon's PresetRow carries the
    fields directly; the test fake wraps them under "spec", the same two shapes
    presets.seat_kind accepts."""
    spec = (live.get("spec") or live) if isinstance(live, dict) else live
    if isinstance(spec, dict):
        return spec.get("model")
    return getattr(spec, "model", None)


def preset_family(rk, preset: str) -> str:
    """The family of one preset, from its live kind and model. A preset that does
    not exist is a Fail naming it: the finder's family decides which seats may
    check a finder-raised issue, so a guess would change who verifies what."""
    kind = seat_kind(rk, preset)  # Fails when the preset is missing
    return seat_family(kind, _model_of(live_seat(rk, preset)))


def verifier_families(rk, panel: list[str], verifiers: dict[str, str]) -> dict[str, str]:
    """{panel seat: the family of ITS VERIFIER SEAT}. The verifier preset is what
    runs, so the family has to come from it, not from the panel twin it mirrors.

    Reading them here also validates the whole mapping before a single job is paid
    for: a panel seat with no verifier cannot be checked at all, two panel seats
    sharing one verifier seat would narrow a check to one child, and a verifier
    preset that is not on the daemon cannot be spawned.
    """
    if not panel:
        raise Fail("pr.json: no panel seats to verify with")
    missing = [s for s in panel if not verifiers.get(s)]
    if missing:
        raise Fail(f"pr.json: no verifier seat for panel seat(s) {', '.join(missing)}")
    owner, shared = {}, []
    for s in panel:
        if verifiers[s] in owner:
            shared.append(f"{owner[verifiers[s]]} and {s} both use {verifiers[s]}")
        owner[verifiers[s]] = s
    if shared:
        raise Fail(f"pr.json: distinct verifier seats are required, so each panel seat's claim gets its "
                   f"own child; {'; '.join(shared)}")
    return {s: preset_family(rk, verifiers[s]) for s in panel}


def review_attempt(rdir, want=None):
    """The review attempt to verify: the newest NN, which is the one the run's
    `verifying` status was set for. `want` names an older attempt explicitly."""
    reviews = rdir / "review"
    if want:
        d = reviews / str(want)
        if not (d / "candidates.jsonl").exists():
            raise Fail(f"{d}/candidates.jsonl not found")
        return d
    dirs = sorted((p for p in reviews.iterdir() if p.is_dir() and p.name.isdigit()),
                  key=lambda p: int(p.name)) if reviews.is_dir() else []
    if not dirs:
        raise Fail(f"no review attempt under {reviews}; review the run first")
    return dirs[-1]


def _read(path):
    """An input this stage needs, kept raw when it is not json: every one of them
    is written by an earlier stage, so an absent file means the run is not ready
    for this stage, not that something should be skipped."""
    if not path.exists():
        raise Fail(f"{path} not found; the run is not ready to verify")
    return path.read_text()


def read_jsonl(path, what):
    """The JSONL arm of pr_review.read_json: a broken line is a Fail naming the
    file and the line, not a bare json error with no path."""
    out = []
    for n, line in enumerate(_read(path).splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise Fail(f"{path}: {what} line {n}: {e}") from e
    return out


def normalize(f: dict, notes: list | None = None) -> dict:
    """A pooled finding with an int line and its own id, whatever the model wrote
    in the line or id field: the bucket key and the id both need a value the stage
    controls, and a model-supplied id ("F1", or another finding's id) would tie two
    distinct issues together and leave one of them unverified.

    A line that was absent, null or text is pooled as 0, and every such coercion is
    appended to `notes` (the caller records it as friction): the line sets both the
    bucket and part of the id, so a silent 0 moves the finding and nothing says so.
    """
    out = dict(f)
    out["line"] = pr_review.int_line(f)
    out["id"] = pr_review.finding_id(out)
    if notes is not None:
        raw = f.get("line")
        if not isinstance(raw, int) or isinstance(raw, bool) or raw != out["line"]:
            notes.append(f"{out['id']}: line {raw!r} is not an int on the finding; pooled as "
                         f"{out['line']}, which is its bucket and part of its id")
    return out


def record_line_notes(rdir, notes, seat: str = "") -> None:
    """One friction entry per line `normalize` had to coerce, naming the finding.
    `seat` is the seat whose answer carried the finding, when there is one."""
    for note in notes:
        append_friction(rdir, {"seat": seat, "kind": "line-coerced", "note": note})


def claim_answer(answer) -> tuple[str, bool]:
    """(kind, canonical) for one checklist answer. Only "findings" is not a claim;
    everything else is one, including the prose spelling the panel prompt itself
    offers ("checked: nothing, because ..."), a different capitalisation and a
    null. `canonical` is False for anything but those two spellings, which the
    caller records as friction.
    """
    text = " ".join(str(answer).split()).lower() if answer is not None else ""
    if text == FINDINGS:
        return "findings", True
    return "claim", text == NOTHING


def claim_reason(c: dict) -> str:
    """The reason to put to the verifiers. The schema has a `reason` field, but a
    seat that follows the prompt's prose form writes the whole
    "checked: nothing, because <reason>" into `answer` and leaves `reason` empty,
    so the answer text stands in when there is nothing else."""
    reason = str(c.get("reason") or "").strip()
    if reason:
        return reason
    answer = c.get("answer")
    if isinstance(answer, str) and " ".join(answer.split()).lower() not in (NOTHING, FINDINGS):
        return answer.strip()
    return ""


def backed_lenses(candidates: list[dict]) -> set[tuple[str, str]]:
    """The (seat, lens) pairs the pooled findings back. A checklist answer of
    "findings" is honest only when the seat that gave it raised a finding naming
    that lens: the answer is a claim about that seat's own review, so another
    seat's finding cannot stand for it, and a finding that names no lens stands
    for nothing at all."""
    out = set()
    for f in candidates:
        lens = str(f.get("lens") or "").strip()
        if lens:
            out.add((str(f.get("raised_by") or ""), lens))
    return out


def refutation(lens: str, seat: str, reasoning: str, finding,
               notes: list | None = None) -> tuple[dict, str | None]:
    """The finding a refuted claim yields, and the friction note when the verifier
    owed one and did not give it.

    A refuted claim always becomes a finding: the verifier's own when it is a
    finding object, and otherwise the lens-unanswered finding its answer stands
    for. Synthesizing it here (no location, quality, S3) keeps the design's rule
    true: a refuted "checked: nothing" is a finding, never a hole. `notes` collects
    the line coercions `normalize` makes on the verifier's own finding, which the
    caller records: a verifier-supplied finding is the one kind that reaches here
    unnormalized, so it is where an absent line is still visible.
    """
    if isinstance(finding, dict):
        return normalize({**finding, "raised_by": seat, "source": f"claim:{lens}", "concern": lens},
                         notes=notes), None
    text = str(reasoning or "").strip()
    found = normalize({"title": f"{lens}: refuted with no finding (lens unanswered)",
                       "category": "quality", "file": "", "line": 0, "severity": "S3",
                       "claim": text, "evidence": text, "repro": "n/a",
                       "raised_by": seat, "source": f"claim:{lens}", "concern": lens})
    return found, (f"claim:{lens}: holds=false with no finding object ({finding!r}); "
                   f"synthesized the lens-unanswered finding")


def claim_task(lens, lens_prompt, reason):
    return render_prompt("task-pr-verify-claim.md", lens=lens, lens_prompt=lens_prompt or "(none given)",
                         reason=reason or "(none given)")


def verify_task(rdir, f, lenses=None):
    """The verifier's task for one finding. A quality finding is judged against
    the purpose of the lens that raised it (real, and worth the change), not
    against the defect severity rubric: under that rubric every quality finding is
    refuted as "style", so a quality lens could never produce a confirmed finding.
    Both tasks answer in the same {"refuted": ...} shape, so voting is unchanged."""
    fields = {k: str(f.get(k, "") or "(none given)") for k in
              ("title", "file", "line", "severity", "claim", "evidence", "repro")}
    if f.get("category") == "quality":
        lens = (lenses or {}).get(str(f.get("lens") or "")) or {}
        return render_prompt("task-pr-verify-quality.md",
                             lens_prompt=str(lens.get("prompt") or "(none given)"), **fields)
    return render_prompt("task-pr-verify.md", severity_rubric=run_text(rdir, "severity.md"), **fields)


def claim_holder(path):
    """Who holds the claim, for the refusal message. An unreadable marker is
    reported as such rather than raised: the refusal is what matters."""
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return f"marker unreadable: {e}"
    return f"pid {doc.get('pid')} since {doc.get('at')}"


def claim_verify(rdir, run):
    """Take the run for this verify before a single job is paid for. Returns the
    claim file (to release) and the state read under it.

    `status == "verifying"` is set by review and stays set for the whole stage, so
    it is not a claim on the run: a second `verify` reads the same status, passes
    the same guard and spawns a full second attempt over the first one's children
    (and the first attempt's verdicts land under an NN the second never sees). The
    claim is the file created with O_EXCL, so exactly one of two concurrent starts
    can take it. The status is read only after the claim, so the mirror this writes
    is never a state that was read before the claim and could overwrite a
    concurrent change. The same claim is mirrored into state.json, where an
    operator can see it.
    """
    path = rdir / CLAIM
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        raise Fail(f"run {run['run_id']}: a verify is already in progress "
                   f"({claim_holder(path)}). Wait for it; if that process is gone, remove {path}") from None
    except OSError as e:
        raise Fail(f"run {run['run_id']}: cannot take the verify claim at {path}: {e}") from e
    at = now_iso()
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"stage": "verify", "pid": os.getpid(), "at": at}, sort_keys=True) + "\n")
    except OSError as e:
        # A claim file with nothing in it is worse than none: it would refuse the
        # next verify while naming no holder.
        release_verify(rdir, path)
        raise Fail(f"claim verify {path}: {e}") from e
    # Under the claim, and only now, read the status: nothing below may write a
    # state that was read before the claim.
    state = load_state(rdir)
    if state.get("status") != "verifying":
        release_verify(rdir, path)
        raise Fail(f"run is {state.get('status')}; review it first (status must be \"verifying\")")
    # A fresh claim supersedes whatever the last attempt left behind, so a rerun
    # never reads a stale error as this attempt's.
    state.pop("verify_error", None)
    state["verify_in_progress"] = {"at": at, "pid": os.getpid()}
    try:
        save_state(rdir, state)
    except BaseException:
        release_verify(rdir, path)
        raise
    return path, state


def release_verify(rdir, path):
    """Give the claim back. Best effort and loud about it: it runs in a finally,
    so a failure here must not replace the error that brought us here, and a lock
    left behind is what refuses the next verify, so it must not go unsaid."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"warning: {path} not removed ({e}); the next verify will refuse until it is", file=sys.stderr)


def leave_verify_failed(rdir, state, exc) -> None:
    """A verify that dies mid-stage leaves the run defined and re-verifiable.

    The mirror marker goes, and the stage's `finally` releases the claim file, so
    the rerun a stranded run needs is not refused; the status
    is set to "verifying", the only status this stage accepts; and the cause is
    written to the run twice (an error record in state.json and friction), so a
    rerun can tell what happened to the attempt before it. A failure that left the
    run done, or left no trace at all, is what this closes.

    A stage that already finished is left alone: only its reporting can raise after
    the final save (a closed stdout, say), and flipping a done run back to
    "verifying" would send the next operator to re-verify verdicts that exist.
    """
    if state.get("status") == "done" and "verify_in_progress" not in state:
        append_friction(rdir, {"seat": "", "kind": "verify-failed",
                               "note": f"verify finished, but reporting it raised "
                                       f"{type(exc).__name__}: {exc}"})
        return
    state.pop("verify_in_progress", None)
    state["status"] = "verifying"
    state["verify_error"] = {"at": now_iso(), "error": str(exc), "type": type(exc).__name__}
    save_state(rdir, state)
    append_friction(rdir, {"seat": "", "kind": "verify-failed",
                           "note": f"verify attempt failed and left the run at \"verifying\": "
                                   f"{type(exc).__name__}: {exc}"})


@friction_stage("verify-pr")
def stage_verify_pr(rk, home, args) -> dict:
    """The guarded entry point: the run is claimed here (the file, not the status)
    and every way out of the stage either finishes it (status `done`, claim and
    mirror cleared) or records why it did not, rather than stranding `verifying`
    with no error and no rerun possible.

    BaseException, not Exception: a Ctrl-C or a kill mid-stage is exactly the way
    a run is left stranded, and the record has to be written for it too. The
    original exception is re-raised unchanged, so the caller still sees the cause.
    The claim is released in a finally, so an unwind that cannot reach the daemon
    (or cannot write its own record) still gives the run back rather than stranding
    it behind a lock.
    """
    rdir, run = home.load_run(args.run)
    claim, state = claim_verify(rdir, run)
    try:
        return verify_run(rk, args, rdir, run, state)
    except BaseException as e:
        leave_verify_failed(rdir, state, e)
        raise
    finally:
        release_verify(rdir, claim)


def verify_run(rk, args, rdir, run, state) -> dict:
    prd = pr_review.read_json(rdir / "project" / "pr.json", "pr config")
    panel = [str(s) for s in pr_review.as_list(prd, "panel")]
    if not isinstance(prd.get("verifiers"), dict):
        raise Fail(f"{rdir / 'project' / 'pr.json'}: verifiers must map each panel seat to a verifier seat")
    verifiers = {str(k): str(v) for k, v in prd["verifiers"].items()}
    fan = fanout(prd, str(rdir / "project" / "pr.json"))
    families = verifier_families(rk, panel, verifiers)
    finder = str(prd.get("finder") or "")
    # The finder's own family keeps it out of the seats that may check what it
    # raised, so it is read (and validated) up front with the rest of the config.
    finder_family = preset_family(rk, finder) if finder else ""

    radir = review_attempt(rdir, getattr(args, "review", None))
    candidate_notes = []
    candidates = [normalize(f, candidate_notes)
                  for f in read_jsonl(radir / "candidates.jsonl", "candidates")]
    record_line_notes(rdir, candidate_notes)
    cl_file = radir / "checklist.jsonl"
    # Required: review always writes it, and treating a missing pool file as "the
    # panel claimed nothing" would silently skip every claim job.
    checklist = read_jsonl(cl_file, "checklist")
    lens_docs = pr_review.as_list(pr_review.read_json(rdir / "packet" / "lenses.json", "lens packet"), "lenses")
    lenses = {str(l["name"]): l for l in lens_docs if isinstance(l, dict) and l.get("name")}
    packet = [l.strip() for l in _read(rdir / "packet" / "files.txt").splitlines() if l.strip()]
    if not packet:
        raise Fail(f"{rdir / 'packet' / 'files.txt'}: no packet files")
    tree = run_cwd(run)
    timeout = getattr(args, "timeout", 1800)

    adir = next_attempt(rdir / "verify")
    friction_attempt(adir.name)
    st = {"source": str((radir / "candidates.jsonl").relative_to(rdir)), "review": radir.name,
          "model": getattr(args, "model", None), "families": families, "finder_family": finder_family,
          "claims": [], "clusters": [], "jobs": {}, "children": {}}

    def save():
        (adir / "state.json").write_text(json.dumps(st, indent=2, sort_keys=True) + "\n")

    # The claim jobs first: a refuted claim becomes a finding, and findings are
    # clustered and verified in this same attempt, so the pool being clustered is
    # not known until the claims have answered.
    backed = backed_lenses(candidates)
    claimed = []
    for n, c in enumerate(checklist, 1):
        if not isinstance(c, dict):
            append_friction(rdir, {"seat": "", "kind": "instruction-breach",
                                   "note": f"checklist row {n} is not an object ({c!r}); no claim was read "
                                           f"from it"})
            continue
        kind, canonical = claim_answer(c.get("answer"))
        lens = str(c.get("lens") or "").strip()
        raised_by = str(c.get("raised_by") or "")
        if kind == FINDINGS:
            # A "findings" answer is not a claim verifiers can attack: it says this
            # seat raised a finding for this lens, and it is honest only when a
            # candidate backs that (its own finding, naming the lens). With no
            # backing the seat answered a lens it did not review, which is an
            # instruction breach naming the seat and the lens, never a finding
            # synthesized out of nothing.
            if (raised_by, lens) not in backed:
                append_friction(rdir, {"seat": raised_by, "kind": "instruction-breach",
                                       "note": f"lens {lens or '(none named)'}: answered findings with no "
                                               f"finding raised for it; nothing backs the answer"})
            continue
        if kind != "claim":
            continue
        if not canonical:
            append_friction(rdir, {"seat": raised_by, "kind": "instruction-breach",
                                   "note": f"lens {lens}: checklist answer {c.get('answer')!r} is neither "
                                           f"\"{FINDINGS}\" nor \"{NOTHING}\"; treated as a claim"})
        # Unverified, a claim stays in checklist.jsonl as the seat's answer;
        # verifying it is the widening verify_claims turns on.
        if fan["verify_claims"]:
            claimed.append({"lens": lens, "reason": claim_reason(c), "raised_by": raised_by})

    claim_keys = []
    for i, c in enumerate(claimed):
        keys = []
        for seat in assign_verifiers({"key": f"claim:{c['lens']}",
                                      "raised_by": [c["raised_by"]] if c["raised_by"] else []},
                                     panel, verifiers, families):
            key = job_key("claim", i, c["lens"], seat)
            st["jobs"][key] = {"kind": "claim", "seat": seat, "lens": c["lens"], "reason": c["reason"],
                               "raised_by": c["raised_by"], "done": False}
            keys.append(key)
        claim_keys.append(keys)
        st["claims"].append({"index": i, "lens": c["lens"], "raised_by": c["raised_by"], "jobs": keys})

    def spawn_job(key):
        job = st["jobs"][key]
        jdir = adir / key
        jdir.mkdir(exist_ok=True)
        if job["kind"] == "claim":
            lens = lenses.get(job["lens"]) or {}
            task = with_tools(rdir, claim_task(job["lens"], lens.get("prompt", ""), job["reason"]))
        else:
            task = with_tools(rdir, verify_task(rdir, job["member"], lenses))
        (jdir / "task.md").write_text(task)
        (jdir / "prefill.txt").write_text("\n".join(packet) + "\n")
        try:
            # The packet's paths are run-relative (packet/..., tree/...), so the
            # seats run in the run directory, not in the frozen tree.
            cid, sid, delivery = rk.spawn_seat(
                child_name(run["run_id"], adir.name, key), preset=job["seat"], cwd=rdir,
                packet_files=packet, instruction=task, model=st.get("model"),
                labels={**model_labels(st.get("model")), "review/run": run["run_id"],
                        "review/stage": "verify-pr", "review/attempt": adir.name, "review/job": key,
                        "review/cluster": job.get("cluster") or f"claim:{job.get('lens') or ''}"})
        except AtCap:
            return AT_CAP
        except Fail as e:
            (jdir / "spawn-error.txt").write_text(f"{e}\n")
            job["done"], job["error"] = True, "spawn"
            append_friction(rdir, {"seat": job["seat"], "kind": "spawn-error", "note": f"{key}: {e}"})
            save()
            print(f"  {key}: SPAWN FAILED {e}")
            return None
        st["children"][cid] = {"job": key, "conversation_id": sid, "spawned_at": time.time(),
                               "delivery": delivery, "collected": False}
        save()
        return cid

    def collect(cid, info, conv):
        ch = st["children"][cid]
        job = st["jobs"][ch["job"]]
        jdir = adir / ch["job"]
        m = child_metrics(info, conv, set(packet))
        m["wall_s"] = round(time.time() - ch["spawned_at"], 1)
        m["delivery"] = ch.get("delivery")
        (jdir / "child.json").write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")
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
        except (ValueError, json.JSONDecodeError) as e:
            # A vote that never arrived is not a vote: the issue it belongs to
            # comes out incomplete, which is why it is never silently dropped.
            job["error"] = "parse"
            (jdir / "parse-error.txt").write_text(f"{e}\n")
            append_friction(rdir, {"seat": job["seat"], "kind": "parse-error", "note": f"{ch['job']}: {e}"})
            return
        job["vote"] = v
        (jdir / "verdict.json").write_text(json.dumps(v, indent=2, sort_keys=True) + "\n")
        for note in pr_review.as_list(v, "process_notes"):
            append_friction(rdir, {"seat": job["seat"], "kind": "self", "note": str(note)})

    def run_jobs(order):
        """One batch at a time, args.parallel in flight; a spawn refused at the cap is
        requeued, never counted as a result."""
        pending = {cid: ch for cid, ch in st["children"].items()
                   if not ch["collected"] and ch["job"] in set(order)}
        queue = [k for k in order if not st["jobs"][k]["done"]
                 and not any(ch["job"] == k for ch in st["children"].values())]
        while queue or pending:
            while queue and len(pending) < args.parallel:
                key = queue.pop(0)
                cid = spawn_job(key)
                if cid is AT_CAP:
                    if not pending:
                        raise Fail("spawn refused at the subtree's live-children cap with nothing of ours "
                                   "running; launch grants max_children, or stop what holds the slots")
                    queue.insert(0, key)
                    break  # wait for a running child to settle
                if cid:
                    pending[cid] = st["children"][cid]
            for cid in list(pending):
                ch = pending[cid]
                got = settled(rk, cid, timed_out=time.time() - ch["spawned_at"] > timeout)
                if got is None:
                    continue
                collect(cid, *got)
                save()
                del pending[cid]
                j = st["jobs"][ch["job"]]
                print(f"  {ch['job']}: {json.dumps(j.get('vote'), sort_keys=True)[:120] if j.get('vote') else j.get('error')}")
            if pending:
                time.sleep(3)

    run_jobs([k for keys in claim_keys for k in keys])

    claim_rows, refuted_findings = [], []
    for i, c in enumerate(claimed):
        votes, new = [], []
        for key in claim_keys[i]:
            job = st["jobs"][key]
            v = job.get("vote") or {}
            holds = v.get("holds")
            votes.append({"seat": job["seat"], "holds": holds if isinstance(holds, bool) else None,
                          "reasoning": str(v.get("reasoning") or "")})
            if v and not isinstance(holds, bool):
                # A vote that parsed but carries no boolean is a contract breach,
                # and one friction line is what keeps it from being silent.
                append_friction(rdir, {"seat": job["seat"], "kind": "instruction-breach",
                                       "note": f"claim:{c['lens']}: vote from {job['seat']} has "
                                               f"holds={holds!r}, not true/false; the claim stays incomplete"})
            if holds is False:
                line_notes = []
                found, note = refutation(c["lens"], job["seat"], v.get("reasoning"), v.get("finding"),
                                         notes=line_notes)
                if note:
                    append_friction(rdir, {"seat": job["seat"], "kind": "instruction-breach", "note": note})
                record_line_notes(rdir, line_notes, seat=job["seat"])
                if found.get("file"):
                    # A synthesized refutation has no location by design, so only a
                    # located finding is worth a reference check.
                    hc = analysis.hallucination_checks(tree, found)
                    if hc.get("file_missing") or hc.get("line_out_of_range"):
                        append_friction(rdir, {"seat": job["seat"], "kind": "hallucination",
                                               "note": f"claim:{c['lens']} -> {found.get('file')}:"
                                                       f"{found.get('line')} {json.dumps(hc, sort_keys=True)}"})
                new.append(found)
        # Any holds=false is a refutation, whatever shape the answer came in: the
        # claim is refuted, never "incomplete", because a child answered it and
        # could not ground its "nothing".
        outcome = "refuted" if new else ("holds" if votes and all(v["holds"] is True for v in votes)
                                         else "incomplete")
        refuted_findings += new
        claim_rows.append({"key": f"claim:{i:02d}:{c['lens']}", "lens": c["lens"], "raised_by": c["raised_by"],
                           "reason": c["reason"], "verdict": outcome, "votes": votes, "findings": new})

    clusters = cluster(candidates + refuted_findings)
    units_by_key, jobs_order, index = {}, [], 0
    for cl in clusters:
        units, shortfalls = member_units(cl), []
        for unit in units:
            # Each issue gets its own assignment and its own votes: the bucket
            # only groups the row and picks the seats.
            unit["seats"] = assign_verifiers({"key": cl["key"], "raised_by": unit["raised_by"]},
                                             panel, verifiers, families, exclude_family=finder_family,
                                             shortfalls=shortfalls)
            if len(unit["seats"]) < 2:
                # Fewer than the two fresh children the cross-check wants, and always
                # named: none because every verifier seat raised this issue itself
                # (three seats can refute one lens three ways), or one because that is
                # all the panel and the fallback list left. The issue is emitted
                # either way rather than failing the run: the row is shown, and a dead
                # stage would lose every other verdict.
                if unit["seats"]:
                    why = f"only {unit['seats'][0]} checks it, so one child's vote settles it"
                else:
                    why = ("every verifier seat raised this issue; no fresh child could check it, so it "
                           "stays incomplete")
                append_friction(rdir, {"seat": "", "kind": "verifier-shortfall",
                                       "note": f"{cl['key']} {unit['id']}: {len(unit['seats'])} verifier "
                                               f"seat(s), fewer than two; {why}"})
            unit["jobs"] = []
            for seat in unit["seats"]:
                key = job_key("verify", index, unit["id"], seat)
                index += 1
                st["jobs"][key] = {"kind": "verify", "seat": seat, "cluster": cl["key"], "done": False,
                                   "member": unit["member"]}
                unit["jobs"].append(key)
                jobs_order.append(key)
        units_by_key[cl["key"]] = units
        for msg in shortfalls:
            append_friction(rdir, {"seat": "", "kind": "verifier-shortfall", "note": msg})
        st["clusters"].append({"key": cl["key"], "raised_by": cl["raised_by"],
                               "seats": sorted({s for u in units for s in u["seats"]}),
                               "members": [{"id": u["id"], "raised_by": u["raised_by"], "seats": u["seats"],
                                            "jobs": u["jobs"]} for u in units]})
    run_jobs(jobs_order)

    verdict_rows = []
    for cl in clusters:
        members, all_votes = [], []
        for unit in units_by_key[cl["key"]]:
            votes = []
            for key in unit["jobs"]:
                job = st["jobs"][key]
                v = job.get("vote") or {}
                r = v.get("refuted")
                if v and not isinstance(r, bool):
                    append_friction(rdir, {"seat": job["seat"], "kind": "instruction-breach",
                                           "note": f"{cl['key']} {unit['id']}: vote from {job['seat']} has "
                                                   f"refuted={r!r}, not true/false; the issue stays incomplete"})
                votes.append({"id": unit["id"], "seat": job["seat"],
                              "refuted": r if isinstance(r, bool) else None,
                              "reasoning": str(v.get("reasoning") or ""),
                              "severity_adjustment": v.get("severity_adjustment") or None})
            m = unit["member"]
            members.append({"id": unit["id"], "verdict": verdict(votes), "seats": unit["seats"],
                            "title": str(m.get("title") or ""), "file": str(m.get("file") or ""),
                            "line": m.get("line"), "claim": str(m.get("claim") or ""),
                            "raised_by": unit["raised_by"], "source": str(m.get("source") or "")})
            if members[-1]["verdict"] == "contested":
                losing = minority_side(votes)
                counts = vote_tally(votes)
                against = sum(n for r, n in counts.items() if r not in losing)
                for vote in votes:
                    if vote["refuted"] in losing:
                        append_friction(rdir, {"seat": vote["seat"], "kind": "contested-loss",
                                               "note": f"{cl['key']} {unit['id']}: voted "
                                                       f"refuted={vote['refuted']} against {against} vote(s)"})
            all_votes += votes
        rep = cl["representative"]
        verdict_rows.append({"key": cl["key"], "title": str(rep.get("title") or ""),
                             "file": str(rep.get("file") or ""), "line": rep.get("line"),
                             "claim": str(rep.get("claim") or ""),
                             "verdict": cluster_verdict([m["verdict"] for m in members]),
                             "votes": all_votes, "members": members,
                             "raised_by": cl["raised_by"], "source": cl["source"]})

    with (adir / "verdicts.jsonl").open("w") as fh:
        for row in verdict_rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    with (adir / "claims.jsonl").open("w") as fh:
        for row in claim_rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    # Issues are counted by their own verdict, as the report counts them: a
    # bucket is one row but holds several issues, so one refuted neighbour makes
    # a bucket of confirmed issues "contested". The buckets' own verdicts ride
    # along under "buckets" for anyone reading the clustering.
    counts = {k: 0 for k in ("confirmed", "contested", "refuted", "incomplete")}
    buckets = dict(counts)
    for row in verdict_rows:
        buckets[row["verdict"]] += 1
        for m in row["members"]:
            counts[m["verdict"]] += 1
    n_members = sum(len(u) for u in units_by_key.values())
    state["status"] = "done"
    state.pop("verify_in_progress", None)  # the claim is released with the finish
    state["verify"] = {"attempt": adir.name, "review": radir.name, "claims": len(claim_rows),
                       "clusters": len(verdict_rows), "members": n_members, **counts, "buckets": buckets}
    save_state(rdir, state)

    print(f"verify-pr {adir.name}: {len(verdict_rows)} buckets, {n_members} issues from {len(candidates)} "
          f"candidates + {len(refuted_findings)} claim findings; "
          + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + " (issues; buckets: "
          + ", ".join(f"{k} {v}" for k, v in sorted(buckets.items())) + ")")
    print(f"inspect: {adir}/verdicts.jsonl")
    return {"stage": "verify-pr", "run": run["run_id"], "attempt": adir.name,
            "clusters": len(verdict_rows), "members": n_members, "claims": len(claim_rows),
            "claim_findings": len(refuted_findings), "children": len(st["children"]), **counts,
            "buckets": buckets}
