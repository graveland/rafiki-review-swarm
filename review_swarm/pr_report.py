# review-swarm: the run report - a finished run's verdicts as the markdown a
# session triages, and a friction rollup across a project's runs
"""The report is a read-only view of a run: it reads the records the stages
already wrote (verdicts, the checklist claims, questions/prior, friction,
run.json, the packet manifest's seat policy, the children's child.json) and
writes one markdown file to a path the operator names. It never writes into the
run directory, because a run is never edited after it finishes.

What is shown is what pr_verify decided. Confirmed and contested issues are
shown, and incomplete ones too: a vote that never arrived is a verdict, not a
dismissal, and an unverified cluster is not a dismissed one. A refuted issue
stays in the run for calibration and is not shown. Members are selected by
their OWN verdict, never by their bucket's, and rendered with their own votes:
a bucket is one row but holds several distinct issues, so a confirmed issue in a
bucket with an incomplete sibling is still confirmed, and one issue's votes
never stand in for another's. An always-on lens whose checked-nothing claim
ended incomplete is shown under Incomplete too; it is a claim nobody verified,
not a claim verified clean.

A confirmed finding is summarised, but the verifiers' severity adjustments are
not: each vote carries its own, and every one is reported per seat rather than
folded into one number (the design's "reported, not averaged away").

Two things the report needs are not on a verdict row. Severity lives on the
finding, so it is read from the review attempt's candidates.jsonl, and from the
verify attempt's claims.jsonl for a refuted checked-nothing claim (that finding
exists only there). Seat kind, which splits dollars from tokens, is not on
child.json either: it comes from packet/manifest.json, the seat policy the run
was built against, so no kind is decided in this module.
"""

import json
import os
from pathlib import Path

from .common import Fail
from .runs import Home

# Severity order, most severe first. A finding whose severity the run does not
# record sorts last and prints as "S?", so it is visible rather than ranked as
# if its severity were known.
SEVERITY_ORDER = {"S1": 0, "S2": 1, "S3": 2}
UNKNOWN_SEVERITY = "S?"

# The kind that splits the cost section. A seat whose kind the manifest does not
# record gets its own table instead of being assumed into either.
FUNDI, CLAUDE, UNKNOWN_KIND = "fundi", "claude", "unknown"


def load_json(path, what):
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise Fail(f"{path}: {what}: {e}") from e
    return doc


def read_jsonl(path, what):
    """Rows of a JSONL file, one json object per line. A file that is not there
    is no rows (a run with no friction, no questions is a normal run); a broken
    line is a Fail naming the file and the line rather than a bare json error."""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for n, line in enumerate(p.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise Fail(f"{p}: {what} line {n}: {e}") from e
        if not isinstance(row, dict):
            # A row that is not an object cannot be carried or shown, and a report
            # that quietly skipped it would undercount what the run did.
            raise Fail(f"{p}: {what} line {n} is a {type(row).__name__}, not an object")
        out.append(row)
    return out


def latest_attempt(stage_dir, what):
    """The newest NN under a stage dir, the attempt the run's status was set for."""
    d = Path(stage_dir)
    dirs = sorted((p for p in d.iterdir() if p.is_dir() and p.name.isdigit()),
                  key=lambda p: int(p.name)) if d.is_dir() else []
    if not dirs:
        raise Fail(f"no {what} attempt under {d}; the run has not been {what}ed yet")
    return dirs[-1]


def review_attempt_dir(rdir, vstate):
    """The review attempt the verdicts were computed from. The verify state names
    it, so questions/prior are read from the same attempt the panel wrote them
    in rather than from whichever attempt happens to be newest."""
    name = str(vstate.get("review") or "")
    if name:
        d = rdir / "review" / name
        if not d.is_dir():
            raise Fail(f"{rdir / 'verify'}: the verified attempt names review/{name}, which is not there")
        return d
    reviews = rdir / "review"
    dirs = sorted((p for p in reviews.iterdir() if p.is_dir() and p.name.isdigit()),
                  key=lambda p: int(p.name)) if reviews.is_dir() else []
    return dirs[-1] if dirs else None


def severity_map(rdir, vstate, claims):
    """{finding id: severity}. A verdict row carries the id and the anchor, not
    the severity, so the severity comes from the finding it names: the review
    attempt's candidates.jsonl for a pooled finding, and the claims rows for a
    refuted checked-nothing claim, which is a finding only there. A candidate
    wins a collision, being the pooled finding both describe.
    """
    sev = {}
    src = str(vstate.get("source") or "")
    if src:
        path = rdir / src
        if not path.exists():
            raise Fail(f"{rdir}: the verify state names {src}, which is not there")
        for f in read_jsonl(path, "candidates"):
            if f.get("id"):
                sev[str(f["id"])] = str(f.get("severity") or "")
    for row in claims:
        for f in row.get("findings") or []:
            if isinstance(f, dict) and f.get("id"):
                sev.setdefault(str(f["id"]), str(f.get("severity") or ""))
    return sev


def seat_kinds(rdir):
    """{seat preset: kind} from packet/manifest.json: the seat policy the packet
    was built against, recorded in the run. Kinds are policy, retuned by
    calibration, so this reads them from the run rather than deciding here which
    seat is which. A seat the manifest does not name is left out, and the caller
    books it as unknown, counted apart: never fundi, which would put a child of
    an unknown kind's tokens in a dollars column (the ruling on F8, so this and
    compare_pr's run_spend give one answer for one run)."""
    manifest = load_json(rdir / "packet" / "manifest.json", "packet manifest")
    seats = manifest.get("seats") if isinstance(manifest, dict) else None
    if not isinstance(seats, dict):
        raise Fail(f"{rdir / 'packet' / 'manifest.json'}: no seats recorded; the run has no packet")
    # A named entry with no kind is still a fundi seat: that is seat_kind's rule
    # for a preset whose policy omits the field, and gather writes the default.
    return {str(name): str((spec or {}).get("kind") or FUNDI) if isinstance(spec, dict) else FUNDI
            for name, spec in seats.items()}


def members_of(row):
    """The distinct issues in a bucket, in the row's own order. A row written
    before members existed still renders: its representative's fields stand in
    for the one issue it carries."""
    members = [m for m in (row.get("members") or []) if isinstance(m, dict)]
    if members:
        return members
    return [{"id": "", "verdict": str(row.get("verdict") or ""), "seats": [],
             "title": str(row.get("title") or ""), "file": str(row.get("file") or ""),
             "line": row.get("line"), "claim": str(row.get("claim") or ""),
             "raised_by": row.get("raised_by") or [], "source": str(row.get("source") or "")}]


def anchor(member):
    line = member.get("line")
    line = line if isinstance(line, int) and not isinstance(line, bool) else 0
    return f"{member.get('file') or ''}:{line}"


def display_severity(sev):
    s = str(sev or "")
    return s if s in SEVERITY_ORDER else UNKNOWN_SEVERITY


def severity_rank(sev):
    return SEVERITY_ORDER.get(sev, len(SEVERITY_ORDER))


def member_severity(member, severities):
    """The finding's own severity, by the id the row carries. An id the run does
    not record is unknown and shown as such: defaulting it would rank a finding
    on a severity nobody wrote."""
    return display_severity(severities.get(str(member.get("id") or "")))


def member_votes(row, member):
    """The votes that belong to one member of a bucket. Votes carry the issue id,
    so a bucket holding two issues never hands both their votes to one of them;
    a row whose votes predate the id belongs to its single member."""
    votes = [v for v in (row.get("votes") or []) if isinstance(v, dict)]
    if not any("id" in v for v in votes):
        return votes
    return [v for v in votes if str(v.get("id") or "") == str(member.get("id") or "")]


def sort_key(member, severities):
    return (severity_rank(member_severity(member, severities)), str(member.get("file") or ""),
            member.get("line") if isinstance(member.get("line"), int) else 0,
            str(member.get("id") or ""))


def members_with(rows, verdict, severities):
    """(bucket row, member) for every issue whose OWN verdict is this one,
    ordered by severity then file. A member is selected by its own verdict, never
    by its bucket's: a bucket is one row but holds several distinct issues, so a
    confirmed issue sharing a bucket with an incomplete sibling is still
    confirmed (shown, triaged, and in the severity line) and a refuted issue in a
    contested bucket is still refuted (never shown). The row rides along so a
    member's own votes are read from the bucket it came out of."""
    out = []
    for row in rows:
        out += [(row, m) for m in members_of(row) if str(m.get("verdict") or "") == verdict]
    return sorted(out, key=lambda pair: sort_key(pair[1], severities))


def plural(n, one, many):
    return f"{n} {one if n == 1 else many}"


def member_buckets(rows, verdict, severities):
    """(how many members, in how many buckets) carry this verdict. Both are
    reported: the member count is what a session acts on, the bucket count says
    how the row's clustering saw them."""
    members = members_with(rows, verdict, severities)
    return len(members), len({id(row) for row, _ in members})


def raised_text(member):
    raised = member.get("raised_by")
    seats = ", ".join(str(s) for s in raised) if isinstance(raised, list) else str(raised or "")
    source = str(member.get("source") or "")
    if not seats:
        return f"raised by: (unrecorded){' (source ' + source + ')' if source else ''}"
    return f"raised by: {seats}" + (f" (source {source})" if source else "")


def vote_label(vote):
    r = vote.get("refuted")
    if r is True:
        return "refuted"
    if r is False:
        return "upheld"
    # Neither: the vote never arrived or did not carry a boolean. It is a verdict
    # the report shows, not one it drops.
    return "no verdict (the vote is missing or unparseable)"


def vote_line(vote):
    label = vote_label(vote)
    adj = str(vote.get("severity_adjustment") or "")
    suffix = f" [severity: {adj}]" if adj else ""
    reason = str(vote.get("reasoning") or "").strip()
    return f"  - {vote.get('seat') or '?'!s}: {label}{suffix}" + (f" - {reason}" if reason else "")


def vote_summary(votes):
    if not votes:
        return "no votes"
    up = sum(1 for v in votes if v.get("refuted") is False)
    ref = sum(1 for v in votes if v.get("refuted") is True)
    un = len(votes) - up - ref
    parts = [f"{len(votes)} vote(s), {up} upheld, {ref} refuted"]
    if un:
        parts.append(f"{un} with no verdict")
    seats = ", ".join(str(v.get("seat") or "?") for v in votes)
    return ", ".join(parts) + (f" ({seats})" if seats else "")


def severity_adjustments(votes):
    """Each verifier's severity adjustment, one per seat, sorted by seat.

    The design says severity adjustments are reported, not averaged away: a
    confirmed finding is summarised, but its tier still carries what every
    verifier said about it. A vote that carried no adjustment is "not reported",
    never "none": "none" is the verifier's own ruling that the tier stands, and
    reading an absent field as that ruling would invent it. Sorted so two
    readings of the same run are the same bytes.
    """
    if not votes:
        return ""
    parts = []
    for v in sorted(votes, key=lambda v: str(v.get("seat") or "")):
        adj = str(v.get("severity_adjustment") or "")
        parts.append(f"{v.get('seat') or '?'!s}: {adj or 'not reported'}")
    return ", ".join(parts)


def finding_block(member, votes, severities, *, show_reasoning):
    """One finding as a markdown block: its severity, its anchor and claim, who
    raised it, and its votes. A contested or incomplete finding spells out every
    vote's reasoning (both sides, never one side), because that is what the
    session has to weigh; a confirmed one summarises them, but still reports each
    verifier's severity adjustment, one per seat, because the design says
    adjustments are reported rather than averaged away. Those adjustments are
    also on the per-vote lines of a contested or incomplete finding."""
    title = str(member.get("title") or "").strip() or anchor(member)
    verdict = str(member.get("verdict") or "")
    lines = [f"### {member_severity(member, severities)} {title}"
             + (f" ({verdict})" if verdict else ""), ""]
    lines.append(f"- `{anchor(member)}` {str(member.get('claim') or '').strip()}".rstrip())
    lines.append(f"- {raised_text(member)}")
    if show_reasoning:
        lines.append("- votes:")
        if votes:
            lines += [vote_line(v) for v in votes]
        else:
            lines.append("  - no votes: no fresh child could check this issue")
    else:
        lines.append(f"- votes: {vote_summary(votes)}")
        adjustments = severity_adjustments(votes)
        if adjustments:
            lines.append(f"- severity adjustments: {adjustments}")
    lines.append("")
    return lines


def shown_section(heading, rows, verdict, severities, *, show_reasoning, empty=None):
    members = members_with(rows, verdict, severities)
    lines = [f"## {heading}", ""]
    if not members:
        lines += [empty or "(none)", ""]
        return lines
    for row, m in members:
        lines += finding_block(m, member_votes(row, m), severities, show_reasoning=show_reasoning)
    return lines


def claim_vote_line(vote):
    """One vote on a checked-nothing claim. A claim vote says whether the claim
    holds (the verifier could not refute it), so it is spelled in the claim's own
    terms rather than as an upheld finding."""
    holds = vote.get("holds")
    if holds is True:
        label = "held (not refuted)"
    elif holds is False:
        label = "refuted the claim"
    else:
        # Neither: the vote never arrived or carried no boolean. It is a verdict
        # the report shows, not one it drops.
        label = "no verdict (the vote is missing or unparseable)"
    reason = str(vote.get("reasoning") or "").strip()
    return f"  - {vote.get('seat') or '?'!s}: {label}" + (f" - {reason}" if reason else "")


def claim_block(claim):
    """An always-on lens answer that ended incomplete: nobody verified the
    \"checked: nothing\", so it is shown as unresolved rather than left out. It
    is not in verdicts.jsonl (only a refuted claim becomes a finding there), so
    this is the one place the lens appears."""
    lens = str(claim.get("lens") or "(no lens)")
    lines = [f"### claim: {lens} (incomplete)", "",
             f"- raised by: {claim.get('raised_by') or '(unrecorded)'!s}",
             f"- reason: {str(claim.get('reason') or '').strip() or '(none given)'}"]
    votes = [v for v in (claim.get("votes") or []) if isinstance(v, dict)]
    lines.append("- votes:")
    if votes:
        lines += [claim_vote_line(v) for v in votes]
    else:
        lines.append("  - no votes: no verifier answered the claim")
    lines.append("")
    return lines


def incomplete_section(rows, severities, claims):
    """Everything the run could not settle: issues whose own verdict is
    incomplete (a vote missing or unparseable), and checked-nothing claims the
    same happened to. Neither is a dismissal, so neither is left out."""
    members = members_with(rows, "incomplete", severities)
    incomplete_claims = sorted((c for c in claims if str(c.get("verdict") or "") == "incomplete"),
                               key=lambda c: (str(c.get("lens") or ""),
                                              str(c.get("raised_by") or ""), str(c.get("key") or "")))
    lines = ["## Incomplete", ""]
    if not members and not incomplete_claims:
        lines += ["(none: every cluster and every checked-nothing claim got a verdict)", ""]
        return lines
    for row, m in members:
        lines += finding_block(m, member_votes(row, m), severities, show_reasoning=True)
    for claim in incomplete_claims:
        lines += claim_block(claim)
    return lines


def summarize(rows, severities):
    """The verdict line. It counts members, not just buckets: a bucket can hold
    several issues with different verdicts, so a bucket count alone would say
    nothing about how many findings are confirmed, contested or unresolved."""
    parts = []
    for verdict, label in (("confirmed", "Confirmed"), ("contested", "contested"),
                           ("incomplete", "incomplete")):
        n_members, n_buckets = member_buckets(rows, verdict, severities)
        parts.append(f"{label} {plural(n_members, 'member', 'members')} in "
                     f"{plural(n_buckets, 'bucket', 'buckets')}")
    best = None
    for _, m in members_with(rows, "confirmed", severities):
        sev = member_severity(m, severities)
        if best is None or severity_rank(sev) < severity_rank(best):
            best = sev
    line = f"{', '.join(parts)}; highest confirmed severity {best or 'none'}."
    n_refuted = len(members_with(rows, "refuted", severities))
    if n_refuted:
        line += (f" Not shown: {plural(n_refuted, 'refuted member', 'refuted members')} "
                 f"(kept in the run for calibration).")
    return line


def triage_section(rows, severities):
    """An empty table for the session to fill: one row per confirmed and
    contested finding, the two the session has to act on."""
    lines = ["## Triage", "",
             "| finding | disposition | deciding lines |",
             "| --- | --- | --- |"]
    for verdict in ("confirmed", "contested"):
        for _, m in members_with(rows, verdict, severities):
            title = str(m.get("title") or "").strip() or anchor(m)
            lines.append(f"| {member_severity(m, severities)} `{anchor(m)}` {title} |  |  |")
    lines.append("")
    return lines


def questions_section(rows):
    lines = ["## Questions", ""]
    if not rows:
        lines += ["(none)", ""]
        return lines
    for q in sorted(rows, key=lambda r: (str(r.get("raised_by") or ""), str(r.get("question") or ""))):
        lines.append(f"- {q.get('question') or ''!s} (raised by {q.get('raised_by') or '?'})")
    lines.append("")
    return lines


def prior_section(rows):
    """The delta panel's re-judgement of each prior finding: fixed, not-fixed or
    incomplete, with its reason."""
    lines = ["## Prior findings", ""]
    if not rows:
        lines += ["(none)", ""]
        return lines
    for p in sorted(rows, key=lambda r: (str(r.get("id") or ""), str(r.get("raised_by") or ""))):
        lines.append(f"- `{p.get('id') or '?'}`: {p.get('status') or '?'}"
                     f" - {str(p.get('reason') or '').strip()} (raised by {p.get('raised_by') or '?'})")
    lines.append("")
    return lines


def process_section(friction):
    """Friction grouped by kind then seat: what pushed the driver off its path,
    with the seat it happened at, so a run's rough edges are one list."""
    lines = ["## Process", ""]
    if not friction:
        lines += ["(no friction recorded)", ""]
        return lines
    groups = {}
    for e in friction:
        groups.setdefault(str(e.get("kind") or "(no kind)"), []).append(e)
    for kind in sorted(groups):
        lines += [f"### {kind}", ""]
        for e in sorted(groups[kind], key=lambda r: (str(r.get("seat") or ""), str(r.get("at") or ""),
                                                     str(r.get("note") or ""))):
            lines.append(f"- {str(e.get('seat') or '') or '(stage)'}: {e.get('note') or ''!s}")
        lines.append("")
    return lines


def money(row):
    """A seat's dollars. Nothing reported is not $0: a seat whose cost was never
    reported has an unknown cost, and printing 0 would read as free."""
    return f"${row['cost']:.4f}" if row["cost_reported"] else "not reported"


def token_volume(row):
    return f"{row['tin']} in / {row['tout']} out" if row["token_reported"] else "not reported"


def child_rows(rdir):
    """Every child.json under the run, in path order: review cells and verify
    jobs each write one, and what a run cost is all of them together."""
    return [(p, load_json(p, "child metrics")) for p in sorted(rdir.glob("**/child.json"))]


def cost_rows(children, kinds):
    """One row per seat: children, dollars, token volume, and how many children
    reported each. A not-reported number is counted, never summed as zero."""
    seats = {}
    for path, c in children:
        preset = str(c.get("preset") or "")
        row = seats.setdefault(preset, {
            "seat": preset, "kind": kinds.get(preset) or UNKNOWN_KIND, "children": 0,
            "cost": 0.0, "cost_reported": 0, "cost_missing": 0,
            "tin": 0, "tout": 0, "token_reported": 0, "token_missing": 0})
        row["children"] += 1
        cost = c.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            row["cost"] += float(cost)
            row["cost_reported"] += 1
        else:
            row["cost_missing"] += 1
        tin, tout = c.get("input_tokens"), c.get("output_tokens")
        if any(isinstance(t, (int, float)) and not isinstance(t, bool) for t in (tin, tout)):
            row["tin"] += int(tin or 0)
            row["tout"] += int(tout or 0)
            row["token_reported"] += 1
        else:
            row["token_missing"] += 1
    return seats


def cost_section(rdir, kinds):
    """Dollars per fundi seat, token volume per claude-kind seat: the same split
    gather estimated against, now with what the children actually reported."""
    seats = cost_rows(child_rows(rdir), kinds)
    by_kind = {}
    for row in seats.values():
        by_kind.setdefault(row["kind"], []).append(row)
    for rows in by_kind.values():
        rows.sort(key=lambda r: r["seat"])
    lines = ["## Cost", "",
             ("Fundi dollars: reported `cost_usd` summed per seat. Children whose cost was not "
              "reported are counted in their own column, never read as $0."), "",
             "| seat | children | cost_usd | cost not reported |",
             "| --- | --- | --- | --- |"]
    for row in by_kind.get(FUNDI, []):
        lines.append(f"| {row['seat'] or '(no seat)'} | {row['children']} | {money(row)} "
                     f"| {row['cost_missing']} |")
    if not by_kind.get(FUNDI):
        lines.append("| (none) | 0 | | |")
    lines += ["", "Claude-kind seats report token volume, not OpenRouter dollars:", "",
              "| seat | children | input_tokens | output_tokens | tokens not reported |",
              "| --- | --- | --- | --- | --- |"]
    for row in by_kind.get(CLAUDE, []):
        lines.append(f"| {row['seat'] or '(no seat)'} | {row['children']} | {row['tin']} "
                     f"| {row['tout']} | {row['token_missing']} |")
    if not by_kind.get(CLAUDE):
        lines.append("| (none) | 0 | | | |")
    else:
        for row in by_kind[CLAUDE]:
            if not row["token_reported"]:
                # A claude seat that reported no usage is unknown, not zero.
                lines.append(f"seat {row['seat']}: tokens not reported by {row['token_missing']} "
                             f"child(ren), so the volume is not shown as 0")
    if by_kind.get(UNKNOWN_KIND):
        lines += ["", ("Seats whose kind the run does not record (neither dollars nor tokens are "
                       "assumed):"), "",
                  "| seat | children | cost_usd | token volume |",
                  "| --- | --- | --- | --- |"]
        for row in by_kind[UNKNOWN_KIND]:
            lines.append(f"| {row['seat'] or '(no seat)'} | {row['children']} | {money(row)} "
                         f"| {token_volume(row)} |")
    lines.append("")
    return lines


def target_label(run):
    """What was reviewed: the PR number, an explicit base..head, or the design
    doc. The title says what the run pinned, not what someone remembered."""
    if str(run.get("mode") or "") == "design":
        return str(run.get("doc") or "(no doc)")
    if run.get("pr") is not None:
        return f"#{run['pr']}"
    base, head = str(run.get("base") or ""), str(run.get("head") or "")
    return f"{base[:12]}..{head[:12]}"


def project_name(run):
    project = run.get("project")
    if isinstance(project, dict):
        return str(project.get("name") or "?")
    return str(project or "?")


def inside_run(out, rdir):
    """Would writing `out` land inside the run?

    `out` is normalised first: `..` is collapsed and the existing prefix is
    resolved (realpath). That order matters. `..` after a component that does not
    exist yet cannot be traversed by the kernel, so an un-normalised walk stops at
    the wrong ancestor: `T/nope/../runs/<p>/<id>/r.md` (with `T/nope` missing)
    looks like it lands under `T/nope`, and the mkdir that follows would create
    `nope`, after which it IS the run.

    The comparison is then by file identity for `out`'s nearest existing ancestor
    and each of its parents, never by path string: on a case-insensitive
    filesystem (APFS) a case variant of the run's path is the same directory, and
    realpath does not canonicalise the case of a path that does not exist yet, so
    a string comparison would let `.../RUNS/<p>/<id>/` through. os.stat follows
    symlinks, so a symlink to the run is covered too."""
    out, rdir = Path(os.path.realpath(out)), Path(os.path.realpath(rdir))
    if out == rdir or rdir in out.parents:
        return True   # cheap pre-check; the identity walk below decides the rest
    try:
        ancestor = out
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        run = os.stat(rdir)
        while True:
            st = os.stat(ancestor)
            if (st.st_dev, st.st_ino) == (run.st_dev, run.st_ino):
                return True
            if ancestor == ancestor.parent:
                return False
            ancestor = ancestor.parent
    except OSError as e:
        # A path this cannot be checked must not be written to on the chance it
        # is inside the run: the guard failing open is the bug it exists to stop.
        raise Fail(f"cannot check where the report would land ({out}): {e}") from e


def write_report(rdir: Path, out: Path) -> Path:
    """Render the run at `rdir` to the markdown file at `out`, and return `out`.

    `out` is a path the operator names and must be outside the run: a run is
    never edited after it finishes, and a report written into one would make the
    run's own bytes depend on who read it. Everything the report needs is read
    from the run's records; nothing here writes to the run.
    """
    rdir, out = Path(rdir), Path(out)
    if not rdir.is_dir():
        raise Fail(f"no run directory at {rdir}")
    if inside_run(out, rdir):
        raise Fail(f"the report must be written outside the run: {out} is inside {rdir} "
                   f"(a run is never edited after it finishes)")
    run = load_json(rdir / "run.json", "run document")
    if not isinstance(run, dict):
        raise Fail(f"{rdir / 'run.json'}: expected an object")

    vdir = latest_attempt(rdir / "verify", "verify")
    vfile = vdir / "verdicts.jsonl"
    if not vfile.exists():
        raise Fail(f"{vfile} not found; the verify did not finish")
    rows = read_jsonl(vfile, "verdicts")
    # The claim rows are read once: an incomplete one is rendered under Incomplete
    # (it is not a finding, so it is not in verdicts.jsonl), and a refuted one's
    # finding supplies a severity no candidate carries.
    claims = read_jsonl(vdir / "claims.jsonl", "claims")
    vstate = load_json(vdir / "state.json", "verify state") if (vdir / "state.json").exists() else {}
    severities = severity_map(rdir, vstate, claims)
    friction = read_jsonl(rdir / "friction.jsonl", "friction")
    radir = review_attempt_dir(rdir, vstate)
    mode = str(run.get("mode") or "")

    lines = [f"# Review: {project_name(run)} {mode} {target_label(run)}", "",
             summarize(rows, severities), ""]
    # The order is fixed and the sections never move: a session re-reading a
    # report after a fix looks in the same place every time.
    lines += shown_section("Confirmed", rows, "confirmed", severities, show_reasoning=False)
    lines += shown_section("Contested", rows, "contested", severities, show_reasoning=True)
    lines += incomplete_section(rows, severities, claims)
    if mode == "design":
        lines += questions_section(read_jsonl(radir / "questions.jsonl", "questions") if radir else [])
    if mode == "delta":
        lines += prior_section(read_jsonl(radir / "prior.jsonl", "prior") if radir else [])
    lines += triage_section(rows, severities)
    lines += process_section(friction)
    lines += cost_section(rdir, seat_kinds(rdir))
    lines += ["## Disposition", ""]

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines).rstrip("\n") + "\n")
    return out


def friction_rollup(home: Home, *, run: str | None = None, project: str | None = None) -> list[dict]:
    """{seat, kind, count, runs} per (seat, kind), from one run or every PR run
    of a project, sorted by seat then kind. `count` is the entries seen and
    `runs` the sorted run ids they came from, so the same pair in two runs is
    one row that says where it happened. Only PR runs are rolled up: an older
    kind of run has its own friction shape.
    """
    if bool(run) == bool(project):
        raise Fail("friction_rollup: name exactly one of run= or project=")
    if run:
        rdirs = [home.load_run(run)[0]]
    else:
        base = home.runs / project
        if not base.is_dir():
            raise Fail(f"no runs for project {project!r} under {home.runs}")
        rdirs = []
        for d in sorted(p for p in base.iterdir() if p.is_dir()):
            rj = d / "run.json"
            if not rj.exists():
                continue  # not a run directory: something else lives under runs/<project>/
            if load_json(rj, "run document").get("kind") == "pr":
                rdirs.append(d)
    rows = {}
    for d in rdirs:
        for entry in read_jsonl(d / "friction.jsonl", "friction"):
            key = (str(entry.get("seat") or ""), str(entry.get("kind") or ""))
            row = rows.setdefault(key, {"seat": key[0], "kind": key[1], "count": 0, "runs": []})
            row["count"] += 1
            if d.name not in row["runs"]:
                row["runs"].append(d.name)
    out = sorted(rows.values(), key=lambda r: (r["seat"], r["kind"]))
    for row in out:
        row["runs"].sort()
    return out
