# review-swarm: the markdown report of a whole-repo audit run (map, find, verify)
import json
from pathlib import Path

from .common import Fail, as_line
from .stages import SEV_RANK, cluster_findings

EXCERPT = 600
MERGED_NOTE = ("Findings from several lenses at one spot (same file, within a few lines) are merged; "
               "the most severe report leads.")


def load_verdicts(rdir):
    """Every verified finding, the latest verify attempt's verdict winning: a
    later attempt re-judges a subset (say, the quality findings with better
    instructions) and the earlier verdicts stand for the rest."""
    rows = {}
    for vf in sorted((rdir / "verify").glob("*/verified.jsonl")):
        for line in vf.open():
            r = json.loads(line)
            rows[r["id"]] = {**r, "verify_attempt": vf.parent.name}
    return list(rows.values())


def stage_costs(rdir):
    out = {}
    for stage in ("map", "find", "verify"):
        costs = [json.loads(c.read_text()).get("cost_usd") or 0 for c in (rdir / stage).rglob("child.json")]
        if costs:
            out[stage] = (len(costs), sum(costs))
    return out


def outcome(r):
    if r.get("incomplete"):
        return "incomplete"
    if r.get("upheld"):
        return "upheld"
    # An attempt from before rows carried "real" has it only in the verdict.
    real = r.get("real", bool((r.get("verdicts") or [{}])[0].get("real")))
    if r.get("category") == "quality" and real:
        return "real-not-worth"
    return "refuted"


def reasoning(r):
    v = next((v for v in r.get("verdicts") or [] if v.get("refuted") is False or v.get("real")),
             (r.get("verdicts") or [{}])[0])
    text = " ".join(str(v.get("reasoning") or "").split())
    return text[:EXCERPT] + ("..." if len(text) > EXCERPT else "")


def grouped(rows):
    """Rows merged into one entry per issue (same file, nearby lines, same
    category), most severe report first."""
    by_id = {r["id"]: r for r in rows}
    groups = [[by_id[m] for m in members] for members in cluster_findings(rows).values()]
    for g in groups:
        g.sort(key=lambda r: (SEV_RANK.get(r.get("final_severity") or r.get("severity"), 3), r["id"]))
    return sorted(groups, key=lambda g: (SEV_RANK.get(g[0].get("final_severity") or g[0].get("severity"), 3),
                                         str(g[0].get("file")), as_line(g[0].get("line"))))


def entry(g, sev=False):
    head = g[0]
    lenses = ", ".join(dict.fromkeys(r["concern"] for r in g))
    tag = f"**{head.get('final_severity')}** " if sev else ""
    effort = f" (effort {head.get('effort')})" if head.get("effort") else ""
    doc = f" Doc: `{head['doc']}`." if head.get("doc") else ""
    lines = [f"- {tag}`{head.get('file')}:{head.get('line')}` {head.get('title')}{effort}",
             f"  - Lenses: {lenses}.{doc}",
             f"  - Claim: {' '.join(str(head.get('claim') or '').split())}",
             f"  - Verifier: {reasoning(head)}"]
    if len(g) > 1:
        lines.append("  - Also reported as: " + "; ".join(f"[{r['concern']}] {r.get('title')}" for r in g[1:]))
    return "\n".join(lines)


def contested(rows):
    """Defects and drift whose verifiers split: never dropped, shown with both sides."""
    out = []
    seen = set()
    for r in rows:
        votes = [v for v in r.get("verdicts") or [] if "refuted" in v]
        key = r.get("cluster") or r["id"]
        if key in seen:
            continue  # one entry per cluster: its members share the votes
        if len(votes) > 1 and len({bool(v["refuted"]) for v in votes}) > 1:
            out.append(r)
            seen.add(key)
    return out


def write_audit_report(rdir: Path, out: Path) -> Path:
    run = json.loads((rdir / "run.json").read_text())
    rows = load_verdicts(rdir)
    if not rows:
        raise Fail(f"{rdir}: no verify attempt has finished; nothing to report")
    by = {}
    for r in rows:
        by.setdefault((r.get("category"), outcome(r)), []).append(r)

    def section(title, key, sev=False, blurb=""):
        groups = grouped(by.get(key, []))
        body = [f"## {title} ({len(groups)})", ""]
        if blurb:
            body += [blurb, ""]
        body += [entry(g, sev) for g in groups] or ["None."]
        return "\n".join(body) + "\n"

    costs = stage_costs(rdir)
    ts = run["target_state"]
    parts = [
        f"# Audit: {run['project']['name']} @ {ts['commit'][:12]}{' (' + ts['ref'] + ')' if ts.get('ref') else ''}",
        "",
        f"Run `{run['run_id']}`. {len(rows)} findings verified. Cost: "
        + ", ".join(f"{s} {n} children ${c:.2f}" for s, (n, c) in costs.items())
        + f"; total ${sum(c for _, c in costs.values()):.2f}.",
        "",
        MERGED_NOTE,
        "",
        section("Defects, upheld", ("defect", "upheld"), sev=True),
        "## Contested (verifiers split)\n\n"
        + ("\n".join(entry([r]) + "\n  - Votes: " + "; ".join(
            f"refuted={v.get('refuted')}: {' '.join(str(v.get('reasoning') or '').split())[:300]}"
            for v in r["verdicts"]) for r in contested(rows)) or "None.") + "\n",
        section("Drift, upheld", ("drift", "upheld"),
                blurb="A claim in the project's docs that the code contradicts, does not enforce, or says a test "
                      "pins when none does."),
        section("Quality, upheld", ("quality", "upheld")),
        section("Quality, real but judged not worth fixing", ("quality", "real-not-worth"),
                blurb="Real, and kept: whether it is worth fixing is the reader's call, particularly for "
                      "inventory lenses where one copy is small but the set is the point."),
    ]
    refuted = [r for (cat, o), rs in by.items() if o == "refuted" for r in rs]
    incomplete = [r for (cat, o), rs in by.items() if o == "incomplete" for r in rs]
    parts.append(f"## Refuted ({len(refuted)})\n\n" + ("\n".join(
        f"- `{r.get('file')}:{r.get('line')}` [{r['concern']}] {r.get('title')}" for r in
        sorted(refuted, key=lambda r: (r["concern"], str(r.get("file"))))) or "None.") + "\n")
    parts.append(f"## Incomplete (no verdict) ({len(incomplete)})\n\n" + ("\n".join(
        f"- `{r.get('file')}:{r.get('line')}` [{r['concern']}] {r.get('title')}" for r in incomplete) or "None.")
        + "\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(parts))
    return out
