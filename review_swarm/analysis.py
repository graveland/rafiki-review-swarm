# review-swarm: grading, comparison, promotion and status over a run's attempts
import datetime
import hashlib
import itertools
import json
import re

from .common import Fail
from .runs import run_cwd
from .stages import validate_map

# Counterfactual pricing (USD per token). BATCH is OpenRouter's :batch variant of
# the one model the pilot uses; FW_* is a Fireworks B300 dedicated deployment
# (tasks/fireworks.md): $15/h, aggregate decode tok/s at batch=32 by context.
BATCH_PRICE = {"in": 0.06e-6, "out": 0.20e-6}
FW_HOURLY = 15.0
FW_B300_AGG_TPS = [(1_000, 1260), (8_000, 1247), (32_000, 1041), (64_000, 890), (128_000, 517), (131_000, 527)]


def fw_agg_tps(ctx):
    pts = FW_B300_AGG_TPS
    if ctx <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in itertools.pairwise(pts):
        if ctx <= x1:
            return y0 + (y1 - y0) * (ctx - x0) / (x1 - x0)
    return pts[-1][1]


def verify_dirs(rdir, spec):
    out = []
    for nn in spec.split(","):
        vdir = rdir / "verify" / nn.strip()
        st = json.loads((vdir / "state.json").read_text())
        fdir = rdir / st["source"].split("/candidates.jsonl")[0]
        fst = json.loads((fdir / "state.json").read_text())
        out.append({"verify": vdir, "find": fdir, "find_state": fst,
                    "variant": fst.get("variant") or fdir.name,
                    "rows": [json.loads(l) for l in (vdir / "verified.jsonl").open()]})
    return out


def issue_key(f):
    return f"{f.get('file')}:{int(f.get('line') or 0) // 30}"


def gold_match(cluster, gold, window=30):
    """The gold entry a cluster's findings point at: same file, a finding line
    within `window` lines of a known location. Ambiguous (two different bugs
    match) returns None so a human decides."""
    hits = {}
    for f in cluster["findings"]:
        file, line = cluster["issue"].rsplit(":", 1)[0], int(f.get("line") or 0)
        for e in gold:
            for loc in e["locations"]:
                lf, _, ll = loc.rpartition(":")
                if lf == file and abs(int(ll or 0) - line) <= window:
                    hits[e["bug"]] = e
    if len(hits) <= 1:
        return next(iter(hits.values()), None)
    # Several known bugs sit near these lines: break the tie on vocabulary
    # (finding titles vs each candidate's titles + description), and accept
    # only a clear winner.
    def words(t):
        return {w for w in re.findall(r"[a-z_][a-z0-9_]{2,}", t.lower())
                if w not in {"the", "and", "with", "without", "that", "for", "when", "from", "silently"}}

    fw = words(" ".join(f["title"] for f in cluster["findings"]))
    scored = sorted(((len(fw & words(" ".join(e["titles"]) + " " + e["description"] + " " + e["bug"])), e)
                     for e in hits.values()), key=lambda x: -x[0])
    (best, e1), (second, _) = scored[0], scored[1]
    return e1 if best >= 3 and best >= 2 * max(second, 1) else None


def grade(home, args):
    """One grading sheet across verify attempts: findings clustered by file +
    30-line bucket (across concerns and variants), graded once per cluster."""
    rdir, run = home.load_run(args.run)
    vds = verify_dirs(rdir, args.verify)
    clusters = {}
    for vd in vds:
        for r in vd["rows"]:
            c = clusters.setdefault(issue_key(r), {"issue": issue_key(r), "grade": "", "note": "", "findings": []})
            c["findings"].append({"variant": vd["variant"], "id": r["id"], "arm": r["arm"], "concern": r["concern"],
                                  "severity": r.get("final_severity") or r.get("severity"),
                                  "verdict": "incomplete" if r["incomplete"] else ("upheld" if r["upheld"] else "refuted"),
                                  "title": r["title"], "line": r.get("line")})
    gfile = rdir / "grades.jsonl"
    old = {}
    if gfile.exists():
        old = {g["issue"]: g for g in map(json.loads, gfile.open())}
    gold = []
    ref = run["target_state"].get("ref")
    gfile_gold = rdir / "project" / "gold" / f"{ref}.jsonl" if ref else None
    if gfile_gold is not None and gfile_gold.exists():
        gold = [json.loads(l) for l in gfile_gold.open()]
    auto = 0
    for k, c in clusters.items():
        if k in old:
            for fld in ("grade", "note", "bug", "also", "graded_by", "disposition"):
                if old[k].get(fld):
                    c[fld] = old[k][fld]
            continue
        match = gold_match(c, gold)
        if match:
            c["grade"] = match["grade"]
            c["bug"] = match["bug"]
            c["note"] = f"gold {ref}: {match['description']}"
            c["graded_by"] = f"gold:{ref}"
            auto += 1
    if gold:
        print(f"gold {ref}: {auto} cluster(s) matched a known bug; the rest need grading")
    ordered = sorted(clusters.values(), key=lambda c: c["issue"])
    with gfile.open("w") as fh:
        for c in ordered:
            fh.write(json.dumps(c) + "\n")
    lines = [f"# Grading sheet: run {run['run_id']}, verify {args.verify}", "",
             ("Grade each ISSUE once in grades.jsonl: set \"grade\" to real | nit | wrong | mixed "
              "(mixed = the cluster holds distinct issues; say which in \"note\")."), ""]
    for c in ordered:
        lines.append(f"## {c['issue']}  grade: {c['grade'] or '___'}")
        for f in c["findings"]:
            lines.append(f"- [{f['variant']}] {f['verdict']:10} {f['severity'] or '-':3} {f['arm']}/{f['concern']} "
                         f"L{f['line']}: {f['title']}")
        lines.append("")
    (rdir / "grades.md").write_text("\n".join(lines))
    graded = sum(1 for c in ordered if c["grade"])
    print(f"{len(ordered)} issues ({graded} graded) from {sum(len(v['rows']) for v in vds)} findings")
    print(f"grade in: {gfile}   (readable: {rdir / 'grades.md'})")


_TREE_TEXT = {}


def tree_text(tree):
    """Every text file under the frozen tree concatenated, for quote lookups."""
    key = str(tree)
    if key not in _TREE_TEXT:
        parts = []
        for p in tree.rglob("*"):
            if p.is_file() and p.stat().st_size < 2_000_000:
                try:
                    parts.append(p.read_text())
                except (UnicodeDecodeError, OSError):
                    pass
        _TREE_TEXT[key] = "\n".join(parts)
    return _TREE_TEXT[key]


def hallucination_checks(tree, f):
    """Mechanical reference checks: the cited file exists, the line is inside
    it, and every backtick-quoted code fragment in the claim/evidence exists
    SOMEWHERE in the tree (evidence routinely quotes callers in other files,
    so only a fragment found nowhere counts as invented). Whitespace is
    collapsed on both sides; fragments shorter than 8 chars are ignored."""
    path = tree / str(f.get("file", "")).removeprefix("./")
    if not path.is_file():
        return {"file_missing": True}
    n = path.read_text(errors="replace").count("\n") + 1
    out = {"line_out_of_range": not (1 <= int(f.get("line") or 0) <= n)}
    quotes = code_quotes(str(f.get("evidence", "")) + " " + str(f.get("claim", "")))
    if quotes:
        hay = "".join(tree_text(tree).split())
        out["quotes"] = len(quotes)
        out["quotes_found"] = sum(1 for q in quotes
                                  if all("".join(piece.split()) in hay for piece in quote_pieces(q)))
    return out


def code_quotes(text):
    """Backtick spans that look like code: at least one code character and few
    spaces (an odd backtick elsewhere shifts regex pairing onto prose; this
    drops those spans)."""
    out = []
    for q in re.findall(r"`([^`\n]+)`", text):
        if len(q) >= 8 and re.search(r"[(){}\[\];=]|::|\.\w|\w_\w", q) and q.count(" ") <= 6:
            out.append(q)
    return out


def quote_pieces(q):
    """Split a quote on elisions (`...`, `…`, `(...)`); pieces under 6 chars
    carry too little to check and are dropped."""
    if re.fullmatch(r"[\w:]+", q) and "::" in q:
        q = q.rsplit("::", 1)[1]   # tree::sanitize_label -> sanitize_label (paths aren't in the source)
    return [p.strip() for p in re.split(r"\(\.\.\.\)|\.\.\.|…", q) if len(p.strip()) >= 6] or [q]


def conv_turn_costs(conv):
    """Per model turn with reported usage: input and output tokens."""
    return [{"in": t["input_tokens"], "out": t.get("output_tokens") or 0}
            for t in conv.get("turns") or []
            if t.get("role") == "assistant" and t.get("input_tokens") is not None]


def compare(rk, home, args):
    rdir, run = home.load_run(args.run)
    tree = run_cwd(run)
    vds = verify_dirs(rdir, args.verify)
    grades, bugs, also = {}, {}, {}
    gfile = rdir / "grades.jsonl"
    if gfile.exists():
        for g in map(json.loads, gfile.open()):
            grades[g["issue"]] = g.get("grade", "")
            bugs[g["issue"]] = g.get("bug") or g["issue"]
            for a in g.get("also") or []:
                for v in a["variants"]:
                    also.setdefault(v, set()).add(a["bug"])
    real_by_variant = {}
    rows = []
    for vd in vds:
        kids = []
        for stage_dir, st in ((vd["find"], vd["find_state"]),
                              (vd["verify"], json.loads((vd["verify"] / "state.json").read_text()))):
            for cid, ch in st["children"].items():
                kids.append((cid, stage_dir / (ch.get("dir") or ch.get("job"))))
        cost = {"live": 0.0, "batch": 0.0, "fw": 0.0}
        turns_n, tools, provs = [], {}, {}
        for cid, cdir in kids:
            cj = cdir / "child.json"
            c = json.loads(cj.read_text()) if cj.exists() else {}
            live_child = c.get("cost_usd") or 0
            if c:
                cost["live"] += live_child
                turns_n.append(c.get("model_turns", 0))
                for k2, v in (c.get("tool_calls") or {}).items():
                    tools[k2] = tools.get(k2, 0) + v
            conv = rk.export(cid)
            tlist = conv_turn_costs(conv)
            live_tokens = sum(t["in"] + t["out"] for t in tlist) or 1
            for i, t in enumerate(tlist):
                share = live_child * (t["in"] + t["out"]) / live_tokens
                cost["batch"] += (t["in"] * BATCH_PRICE["in"] + t["out"] * BATCH_PRICE["out"]) if i == 0 else share
                cost["fw"] += t["out"] / fw_agg_tps(t["in"]) * FW_HOURLY / 3600
            if args.providers:
                for t in conv["turns"]:
                    if t["role"] == "assistant" and t["input_tokens"] is not None:
                        prov = t["served_provider"] or "?"
                        provs[prov] = provs.get(prov, 0) + 1
        findings = vd["rows"]
        up = [r for r in findings if r["upheld"]]
        hc = [hallucination_checks(tree, r) for r in findings]
        issues = {issue_key(r) for r in up}
        g = {k: grades.get(k, "") for k in issues}
        # Real = distinct BUGS (a grade's `bug` slug), not clusters: the
        # file+30-line clustering splits one bug across several clusters.
        real = {bugs[k] for k, v in g.items() if v in ("real", "mixed")} | also.get(vd["variant"], set())
        real_by_variant[vd["variant"]] = real
        refuted_keys = {issue_key(r) for r in findings if not r["upheld"] and not r["incomplete"]} - issues
        verifier_false_upheld = sum(1 for r in up if grades.get(issue_key(r)) == "wrong")
        verifier_false_refuted = sum(1 for k in refuted_keys if grades.get(k) in ("real", "mixed"))
        rows.append({
            "variant": vd["variant"], "find": vd["find"].name, "verify": vd["verify"].name,
            "seat": vd["find_state"].get("seat", "reviewer"), "context": vd["find_state"].get("context", "component"),
            "children": len(kids), "candidates": len(findings), "upheld": len(up),
            "refuted": sum(1 for r in findings if not r["upheld"] and not r["incomplete"]),
            "upheld_issues": len(issues),
            "graded": sum(1 for v in g.values() if v), "real": len(real),
            "nit": sum(1 for v in g.values() if v == "nit"), "wrong": sum(1 for v in g.values() if v == "wrong"),
            "verifier_false_upheld": verifier_false_upheld, "verifier_false_refuted": verifier_false_refuted,
            "file_missing": sum(1 for h in hc if h.get("file_missing")),
            "line_out_of_range": sum(1 for h in hc if h.get("line_out_of_range")),
            "unverifiable_quotes": sum(h.get("quotes", 0) - h.get("quotes_found", 0) for h in hc),
            "median_turns": sorted(turns_n)[len(turns_n) // 2] if turns_n else 0,
            "tool_calls": sum(tools.values()),
            "cost_live": round(cost["live"], 4), "cost_batch_first_call": round(cost["batch"], 4),
            "cost_fw_b300_est": round(cost["fw"], 4),
            "providers": provs,
        })
    for r in rows:
        others = set().union(*(v for k, v in real_by_variant.items() if k != r["variant"])) if len(real_by_variant) > 1 else set()
        r["real_only_here"] = len(real_by_variant[r["variant"]] - others)
        for c in ("live", "batch_first_call", "fw_b300_est"):
            r[f"per_real_{c}"] = round(r[f"cost_{c}"] / r["real"], 4) if r["real"] else None
    out = rdir / f"compare-{args.verify.replace(',', '_')}.json"
    out.write_text(json.dumps(rows, indent=2) + "\n")
    cols = ["variant", "seat", "context", "children", "candidates", "upheld", "refuted", "upheld_issues",
            "real", "nit", "wrong", "real_only_here", "verifier_false_upheld", "verifier_false_refuted", "file_missing", "line_out_of_range", "unverifiable_quotes",
            "median_turns", "tool_calls", "cost_live", "cost_batch_first_call", "cost_fw_b300_est",
            "per_real_live", "per_real_batch_first_call", "per_real_fw_b300_est"]
    for c in cols:
        print(f"{c:26}" + "".join(f"{r[c]!s:>14}" for r in rows))
    if args.providers:
        for r in rows:
            print(f"providers [{r['variant']}]: {r['providers']}")
    print(f"written: {out}")


# --- PR runs: recall and precision against a project's gold set -----------------
#
# A PR run is compared across configurations rather than across variants: what
# changes from run to run is the lens set and the resolved seats, and both are
# pinned in the packet manifest the gather stage wrote. Recall is measured
# against the gold bugs that name the run's own head revision, never against the
# whole gold set: a bug fixed in a later revision, or found in an earlier one, is
# neither credited nor charged to this run.

# Grades that mean "this is a defect". A sheet grade of `mixed` is a bucket that
# holds distinct issues of which at least one is real, which is what `real` means
# here; `nit` and `wrong` are not.
REAL_GRADES = ("real", "mixed")

# The member verdicts that show an issue. A refuted or incomplete issue was not
# shown, so it cannot match a gold bug: counting it would credit the run with a
# bug it did not report.
SHOWN_VERDICTS = ("confirmed", "contested")

# Seat kinds as the run report also splits them (`pr_report.cost_rows`): a seat
# whose preset the run's own manifest does not name has no recorded kind, and
# neither dollars nor tokens are assumed for it. A named preset that says no kind
# is a fundi seat, which is the rule the stages spawn by (presets.seat_kind).
FUNDI, CLAUDE, UNKNOWN_KIND = "fundi", "claude", "unknown"


def _int_line(f):
    """A finding's line as an int, 0 when it reported none or something that is
    not a number. The PR stages coerce lines on the way into the pool, so this is
    a guard against a hand-written row, not the normal path: a comparison must not
    fail on one finding whose line is text."""
    try:
        return int(f.get("line") or 0)
    except (TypeError, ValueError):
        return 0


def _number(v):
    """A reported metric as a number, or None when nothing was reported or what
    arrived is not a number. `True` is not a token count and not a cost, and
    treating it as 1 is exactly the null-read-as-zero mistake this guard exists for."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v


def member_gold(member, gold):
    """The gold entry one verified issue points at, or None.

    The 30-line bucket only groups the row: a bucket can hold several distinct
    issues, so the gold bug may match a member rather than the bucket's headline
    claim. Each member is offered on its own, under `gold_match`'s rule (same
    file, a finding line within 30 lines of a known location, ambiguous matches
    left to a human).
    """
    m = dict(member, line=_int_line(member), title=str(member.get("title") or ""))
    return gold_match({"issue": issue_key(m), "findings": [m]}, gold)


def config_label(manifest):
    """The configuration a PR run reviewed under, as 12 hex characters: sha256
    over the sorted lens sha256s the packet recorded plus the sorted resolved
    `{seat: version}` from the same manifest. Two runs of the same lenses and the
    same resolved presets share a label whatever else the packet holds (packet
    sizes and models are not part of it); a lens file that changed, or a seat
    re-synced to a new version, changes it. Preset versions sort by name, and the
    payload is dumped with sort_keys, so the same inputs always give the same
    label.
    """
    lens_shas = sorted(str(l.get("sha256") or "") for l in manifest.get("lenses") or []
                       if isinstance(l, dict))
    seats = manifest.get("seats") or {}
    versions = {}
    for name in sorted(seats):
        preset = seats[name] if isinstance(seats[name], dict) else {}
        versions[str(name)] = preset.get("version")
    payload = {"lenses": lens_shas, "seats": versions}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]


def read_json_or_none(path, what):
    """`path` parsed, or None when it is not there. A file that exists but does
    not parse is a Fail naming it: a comparison over a corrupt manifest would
    quietly report the wrong configuration."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise Fail(f"{path}: {what}: {e}") from e


def read_gold(pdir):
    """Every entry the project's gold sets name, in file order. A line that does
    not parse stops the comparison: gold is the answer key, and skipping a row
    would change recall silently."""
    out = []
    for f in sorted((pdir / "gold").glob("*.jsonl")):
        for n, line in enumerate(f.open(), 1):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise Fail(f"{f}:{n}: {e}") from e
    return out


def gold_for_head(gold, head):
    """The gold entries that pin the revision under review. Each entry carries the
    commit its bug is present at, as the short sha of that PR revision, while a
    run records the full sha its head resolves to: a match is the entry's commit
    being that head's prefix. An entry with no commit pins nothing and is left
    out rather than counted against every run."""
    h = str(head or "")
    return [e for e in gold
            if str(e.get("commit") or "") and (h == str(e["commit"]) or h.startswith(str(e["commit"])))]


def latest_verdicts(rdir):
    """(attempt name, verdict rows) for the newest verify attempt that wrote
    verdicts.jsonl, or (None, []). A rerun makes a new attempt and a finished run
    is never edited, so the newest attempt is what the run currently shows."""
    attempts = sorted(p for p in (rdir / "verify").glob("*")
                      if p.is_dir() and (p / "verdicts.jsonl").exists())
    if not attempts:
        return None, []
    vdir = attempts[-1]
    rows = []
    for n, line in enumerate((vdir / "verdicts.jsonl").open(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise Fail(f"{vdir / 'verdicts.jsonl'}:{n}: {e}") from e
        if not isinstance(row, dict) or not isinstance(row.get("members"), list):
            raise Fail(f"{vdir / 'verdicts.jsonl'}:{n}: a verdict row must be an object with a members "
                       f"list, got {type(row).__name__}: {json.dumps(row)[:200]}")
        rows.append(row)
    return vdir.name, rows


# The triage outcomes a human records against a finding (design section 7). They
# are not calibration grades: a grade says whether the bot was right, a
# disposition says what was decided about the finding.
DISPOSITIONS = ("agree", "disagree", "need-info")


def sheet_records(rdir):
    """Every record in the run's grading sheet, in file order. A sheet that
    exists but does not parse, or a line that is not an object, is a Fail naming
    it: the sheet is the human annotation a comparison and a delta both read, and
    skipping a row would change precision or drop a disposition silently."""
    gfile = rdir / "grades.jsonl"
    if not gfile.exists():
        return []
    out = []
    for n, line in enumerate(gfile.open(), 1):
        if not line.strip():
            continue
        try:
            g = json.loads(line)
        except json.JSONDecodeError as e:
            raise Fail(f"{gfile}:{n}: {e}") from e
        if not isinstance(g, dict):
            raise Fail(f"{gfile}:{n}: a grading sheet record must be an object, got {type(g).__name__}")
        out.append(g)
    return out


def grades_by_issue(rdir):
    """{bucket key: grade} from the run's grading sheet, {} while a human has not
    graded it. The sheet is keyed the same way a verdict row is (`issue_key`),
    which is what lets a graded bucket count toward precision."""
    return {str(g.get("issue")): str(g.get("grade") or "") for g in sheet_records(rdir)}


def dispositions_by_issue(rdir):
    """{bucket key: disposition} from the run's grading sheet, {} while a human
    has recorded none. A disposition is the triage outcome agree/disagree/need-info
    (see `set_disposition`), which is what a delta carries forward: a calibration
    grade answers a different question (whether the bot was right), so a graded but
    undisposed issue is not reported as decided."""
    out = {}
    for g in sheet_records(rdir):
        d = str(g.get("disposition") or "")
        if d:
            out[str(g.get("issue"))] = d
    return out


def set_disposition(rdir, issue, disposition, note=None):
    """Record a human disposition for one issue in the run's grading sheet, and
    return the record. The sheet is the one sanctioned edit to a finished run
    (design section 6: the model grades stay marked until a human confirms them),
    so this appends to or updates grades.jsonl and never touches the run's packet,
    verdicts or state.

    `issue` is the sheet's key, the same `analysis.issue_key` the run report and
    prior.json key a finding by (file plus a 30-line bucket). An existing record
    keeps its calibration `grade` and every other field; a missing one is created.
    The sheet is rewritten sorted by issue and with sorted keys, so the same
    dispositions always give the same bytes. A sheet that exists but does not parse
    is a Fail naming it: overwriting it would drop grades a human already made."""
    if disposition not in DISPOSITIONS:
        raise Fail(f"disposition {disposition!r} is not one of {', '.join(DISPOSITIONS)}")
    key = str(issue or "")
    if not key:
        raise Fail("disposition: an issue key is required")
    records = {}
    for g in sheet_records(rdir):
        k = str(g.get("issue") or "")
        if not k:
            raise Fail(f"{rdir / 'grades.jsonl'}: a grading sheet record has no issue key: "
                       f"{json.dumps(g, sort_keys=True)[:200]}")
        records[k] = g
    rec = dict(records.get(key) or {"issue": key})
    rec["disposition"] = disposition
    if note is not None:
        rec["note"] = str(note)
    records[key] = rec
    gfile = rdir / "grades.jsonl"
    try:
        with gfile.open("w") as fh:
            for k in sorted(records):
                fh.write(json.dumps(records[k], sort_keys=True) + "\n")
    except OSError as e:
        raise Fail(f"{gfile}: {e}") from e
    return rec


def graded_chain(rdir, verify_attempt):
    """{stage dir: attempt} for the attempts a run's grade comes from: the verify
    attempt whose verdicts are graded and the review attempt it verified. A rerun
    leaves earlier attempts in the run, and their children and friction belong to
    no grade. With no verdicts yet, the newest review attempt stands alone."""
    if verify_attempt:
        st = read_json_or_none(rdir / "verify" / verify_attempt / "state.json", "the verify state") or {}
        review = st.get("review")
        return {"verify": verify_attempt, **({"review": str(review)} if review else {})}
    attempts = sorted(p.name for p in (rdir / "review").glob("*") if p.is_dir())
    return {"review": attempts[-1]} if attempts else {}


def run_spend(rdir, manifest, chain):
    """(fundi dollars, claude-kind tokens, children of an unrecorded kind) over
    the children of the graded chain's attempts (see graded_chain). A child's
    kind comes from the packet manifest's resolved seats, read exactly as the run
    report reads them.

    A preset the manifest does not name has no recorded kind: it is counted in
    `children_unknown_kind` and neither its cost nor its tokens are booked, because
    assuming fundi would price a seat the run never said was a fundi seat. The run
    report renders the same child in its own "kind the run does not record" table,
    so both readers give one answer for one child. A named preset with no kind is a
    fundi seat, which is how the stages spawn it.

    A child that reported no cost is not a child that cost nothing: it
    is counted in `children_no_cost` and its missing dollars are not in the sum,
    and the total stays None when no fundi child reported one at all. Tokens are
    summed over the fields that reported them for the same reason: a claude seat's
    tokens are what it read and wrote, which is not priced in fundi dollars.
    """
    seats = manifest.get("seats") if isinstance(manifest, dict) else None
    by_name = seats if isinstance(seats, dict) else {}
    usd, tokens, no_cost, unknown = 0.0, 0, 0, 0
    fundi_reported = claude_reported = 0
    children = []
    for stage, attempt in sorted(chain.items()):
        children += sorted((rdir / stage / attempt).rglob("child.json"))
    for cj in children:
        try:
            child = json.loads(cj.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise Fail(f"{cj}: child metrics: {e}") from e
        preset = str(child.get("preset") or "")
        row = by_name.get(preset)
        if not isinstance(row, dict):
            unknown += 1
            continue
        kind = str(row.get("kind") or FUNDI)
        if kind == CLAUDE:
            for key in ("input_tokens", "output_tokens"):
                n = _number(child.get(key))
                if n is not None:
                    tokens += n
                    claude_reported += 1
        else:
            cost = _number(child.get("cost_usd"))
            if cost is None:
                no_cost += 1
            else:
                usd += cost
                fundi_reported += 1
    return {
        "fundi_dollars": round(usd, 4) if fundi_reported else None,
        "claude_tokens": tokens if claude_reported else None,
        "children": len(children),
        "children_no_cost": no_cost,
        "children_unknown_kind": unknown,
    }


# friction.jsonl tags each entry with the stage it came from; the stage name
# differs from its attempt directory for verify.
FRICTION_STAGE_DIR = {"review": "review", "verify-pr": "verify"}


def friction_count(rdir, chain):
    """(entries from gather and the graded chain's attempts, untagged entries).
    No file means none were recorded, which is a measured zero: unlike
    a cost, an unwritten friction log is not an unreported one. An untagged entry
    (an operator note, or one written before entries carried their stage) cannot
    be placed in an attempt, so it is counted apart rather than in the grade."""
    f = rdir / "friction.jsonl"
    if not f.exists():
        return 0, 0
    graded = untagged = 0
    for line in f.open():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise Fail(f"{f}: {e}") from e
        stage = row.get("stage") if isinstance(row, dict) else None
        if not stage:
            untagged += 1
        elif stage == "gather" or chain.get(FRICTION_STAGE_DIR.get(stage, "")) == row.get("attempt"):
            graded += 1
    return graded, untagged


def compare_pr(home, runs) -> list[dict]:
    """One row per PR run, sorted by run id: the configuration it ran under, its
    recall and precision against the project's gold set, what it cost and what
    friction it recorded.

    Recall is gold bugs matched over the gold real bugs that pin the run's head
    (see `gold_for_head`); it is null, never 0, when no verdicts were written or
    the head pins no real gold bug: an unmeasured recall and a measured zero are
    different claims. Precision is the confirmed rows that matched gold or that a
    grading sheet called real, over the confirmed rows, and is null when nothing
    was confirmed. Both count a member of a bucket, never only the bucket's
    representative: a bucket can hold several distinct issues.
    """
    rows = []
    for run_id in runs:
        rdir, run = home.load_run(run_id)
        name = (run.get("project") or {}).get("name") if isinstance(run.get("project"), dict) else None
        if not name:
            raise Fail(f"{rdir / 'run.json'}: no project name, so no gold set to compare against")
        manifest = read_json_or_none(rdir / "packet" / "manifest.json", "the packet manifest")
        gold = gold_for_head(read_gold(home.project_dir(name, run.get("target"))), run.get("head"))
        real = [e for e in gold if str(e.get("grade") or "") in REAL_GRADES]
        real_slugs = {str(e.get("bug")) for e in real}
        attempt, vrows = latest_verdicts(rdir)
        grades = grades_by_issue(rdir)
        matched, confirmed, confirmed_real = set(), 0, 0
        for row in vrows:
            hits = [h for h in (member_gold(m, gold) for m in row["members"]
                                if str(m.get("verdict") or "") in SHOWN_VERDICTS) if h is not None]
            matched |= {str(h.get("bug")) for h in hits}
            if str(row.get("verdict") or "") == "confirmed":
                confirmed += 1
                if hits or grades.get(str(row.get("key") or ""), "") in REAL_GRADES:
                    confirmed_real += 1
        chain = graded_chain(rdir, attempt)
        spend = run_spend(rdir, manifest, chain)
        friction, friction_untagged = friction_count(rdir, chain)
        rows.append({
            "run": run_id, "project": name, "mode": run.get("mode"),
            "head": run.get("head"), "base": run.get("base"),
            "config": config_label(manifest) if manifest is not None else None,
            "verify": attempt, "review": chain.get("review"),
            "gold_entries": len(gold), "gold_real": len(real),
            "gold_matched": sorted(matched),
            "recall": round(len(matched & real_slugs) / len(real), 4) if real and vrows else None,
            "confirmed_rows": confirmed,
            "precision": round(confirmed_real / confirmed, 4) if confirmed else None,
            "fundi_dollars": spend["fundi_dollars"], "claude_tokens": spend["claude_tokens"],
            "children": spend["children"], "children_no_cost": spend["children_no_cost"],
            "children_unknown_kind": spend["children_unknown_kind"],
            "friction": friction, "friction_untagged": friction_untagged,
        })
    rows.sort(key=lambda r: r["run"])
    return rows


def promote(home, args):
    rdir, run = home.load_run(args.run)
    if args.stage != "map":
        raise Fail("only map promotion exists so far")
    src = rdir / "map" / args.attempt
    if src.is_dir() and not (src / "map.json").exists():
        cands = sorted(src.glob("mapper-*/map.json"))
        if len(cands) != 1:
            raise Fail(f"{src} has {len(cands)} mapper maps; name one, e.g. {args.attempt}/mapper-1")
        src = cands[0].parent
    src_map = src / "map.json"
    if not src_map.exists():
        raise Fail(f"{src_map} not found")
    doc = json.loads(src_map.read_text())
    problems = validate_map(doc, run)
    if problems and not args.force:
        raise Fail(f"{len(problems)} validation problems (see {src / 'validation.txt'}); --force to promote anyway")
    final = rdir / "map" / "final.json"
    if final.exists():
        final.rename(final.with_name(f"final.{datetime.datetime.now():%Y%m%d-%H%M%S}.json"))
    doc["_promoted_from"] = str(src.relative_to(rdir))
    final.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"promoted {src.relative_to(rdir)} -> map/final.json; hand-edit freely")


def status(home, args):
    rdir, run = home.load_run(args.run)
    ts = run["target_state"]
    # Runs before the split recorded the sandbox build as rafiki_commit.
    daemon = run.get("daemon") or {"target": "sandbox", "version": run.get("rafiki_commit", "?")[:12]}
    print(f"run {run['run_id']}: {run['project']['name']} @ {ts['commit'][:12]}; "
          f"rafiki {daemon['target']} {daemon.get('version', '?')}")
    for stage in ("map", "find", "verify"):
        sdir = rdir / stage
        if not sdir.exists():
            continue
        for a in sorted(p for p in sdir.iterdir() if p.is_dir() and p.name.isdigit()):
            kids = [json.loads(c.read_text()) for c in a.glob("**/child.json")]
            errs = len(list(a.glob("**/parse-error.txt")))
            cost = sum(k.get("cost_usd") or 0 for k in kids)
            print(f"  {stage}/{a.name}: {len(kids)} children, ${cost:.4f}, {errs} parse failures")
        final = sdir / "final.json"
        if final.exists():
            print(f"  {stage}: promoted from {json.loads(final.read_text()).get('_promoted_from')}")
