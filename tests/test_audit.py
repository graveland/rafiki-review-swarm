# tests: the whole-repo audit pipeline's project-chosen seats, lens-compiled
# concerns, glob maps, cross-cutting cells and the drift verifier. Runs are
# built by runs.run_new over a real git repo, the path a live run takes.
import json
import subprocess
from types import SimpleNamespace

import pytest

from review_swarm import lenses, runs, stages
from review_swarm.common import Fail

LENS = "---\nname: {name}\ncategory: {category}\napplies_to: [{modes}]\n{extra}---\n\nHunt {name}.\n"


def lens_file(d, name, category="defect", modes="audit", extra=""):
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(LENS.format(name=name, category=category, modes=modes, extra=extra))
    return p


def git_repo(path, files):
    path.mkdir()
    for rel, text in files.items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    for cmd in (["init", "-q"], ["add", "."],
                ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                 "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(path), *cmd], check=True)
    return path


FILES = {
    "pkg/cli/search.go": "package cli\n" * 3,
    "pkg/cli/fwd.go": "package cli\n",
    "pkg/kube/kube.go": "package kube\n",
    "docs/commands/sc_search.md": "--yes\n",
    "docs/architecture.md": "the front end holds nothing\n",
}


@pytest.fixture
def project(tmp_home, tmp_path):
    """An audit project over a five-file repo, with one lens of each placement
    in its repo layer: per component, path-triggered, cross-cutting, drift."""
    target = git_repo(tmp_path / "repo", FILES)
    ldir = tmp_path / "lenses"
    lens_file(ldir, "front-end", "quality", extra="triggers:\n  paths: ['pkg/cli/*']\n")
    lens_file(ldir, "flags", "quality", extra="packet: ['docs/commands/*']\n")
    lens_file(ldir, "rules", "drift")
    pdir = tmp_home.projects / "sc"
    pdir.mkdir(parents=True)
    (pdir / "project.json").write_text(json.dumps({
        "name": "sc", "target": str(target), "include": ["**"], "must_cover": ["pkg/**"],
        "context_files": ["docs/architecture.md"]}))
    (pdir / "audit.json").write_text(json.dumps({
        "lens_dir": str(ldir),
        "exclude_lenses": ["transitions"],
        "seats": {"mapper": "audit:mapper", "finder": "audit:finder", "verifier": "audit:verifier"}}))
    return pdir


def new_run(tmp_home):
    rid = runs.run_new(tmp_home, "sc", None, {"target": "test"})
    return tmp_home.load_run(rid)


def promote(rdir, components, seams=()):
    (rdir / "map").mkdir(exist_ok=True)
    (rdir / "map" / "final.json").write_text(json.dumps({"components": components, "seams": list(seams)}))


@pytest.fixture
def seats_ok(monkeypatch):
    """check_seats compares the daemon's presets with this checkout; the fake
    has no daemon, so the comparison is the one thing stubbed."""
    monkeypatch.setattr(stages, "check_seats", lambda rk, names: dict.fromkeys(names, 1))


# --- lenses ----------------------------------------------------------------------


def test_a_drift_lens_outside_audit_is_refused(tmp_path):
    p = lens_file(tmp_path, "d", "drift", modes="audit, pr")
    with pytest.raises(Fail, match="drift lens applies only to audit"):
        lenses.load_lens(p)


def test_a_packet_must_be_a_list_of_globs(tmp_path):
    p = lens_file(tmp_path, "x", extra="packet: docs/*\n")
    with pytest.raises(Fail, match="packet must be"):
        lenses.load_lens(p)
    ok = lens_file(tmp_path, "y", extra="packet: ['docs/*']\n")
    assert lenses.load_lens(ok).packet == ("docs/*",)


def test_audit_concerns_keep_audit_lenses_only_and_refuse_an_unknown_exclusion(tmp_path):
    lens_file(tmp_path, "a")
    lens_file(tmp_path, "p", modes="pr")
    loaded = lenses.load_layers(tmp_path, None)
    assert [c["name"] for c in lenses.audit_concerns(loaded)] == ["a"]
    assert lenses.audit_concerns(loaded, ["a"]) == []
    with pytest.raises(Fail, match="unknown lens"):
        lenses.audit_concerns(loaded, ["nope"])


# --- run new -----------------------------------------------------------------------


def test_run_new_compiles_the_audit_lenses_into_the_runs_concerns(tmp_home, project):
    rdir, _ = new_run(tmp_home)
    names = [c["name"] for c in runs.run_concerns(rdir)]
    assert {"front-end", "flags", "rules", "security"} <= set(names)
    assert "transitions" not in names
    flags = next(c for c in runs.run_concerns(rdir) if c["name"] == "flags")
    assert flags["packet"] == ["docs/commands/*"]
    # The project dir itself is untouched: the compile lands in the run's snapshot.
    assert not (project / "concerns.json").exists()


def test_run_new_refuses_a_lens_dir_beside_a_hand_written_concerns_json(tmp_home, project):
    (project / "concerns.json").write_text('{"concerns": []}')
    with pytest.raises(Fail, match="keep one"):
        new_run(tmp_home)


# --- map ---------------------------------------------------------------------------


def test_a_glob_path_covers_the_files_it_matches_and_one_matching_nothing_is_a_problem(tmp_home, project):
    _, run = new_run(tmp_home)
    good = {"components": [{"component": "cli", "paths": ["pkg/cli/*.go"]},
                           {"component": "kube", "paths": ["pkg/kube/kube.go"]}]}
    assert stages.validate_map(good, run) == []
    bad = {"components": [{"component": "cli", "paths": ["pkg/cli/*.go", "pkg/nope/*"]},
                          {"component": "all", "paths": ["pkg/*"]}]}
    problems = stages.validate_map(bad, run)
    assert "cli: glob pkg/nope/* matches no file" in problems
    assert any(p.startswith("pkg/cli/fwd.go: in 2 components") for p in problems)


def test_a_repo_over_the_prefill_bound_goes_to_the_mapper_as_a_listing(tmp_home, project, monkeypatch):
    _, run = new_run(tmp_home)
    tree = stages.run_cwd(run)
    files, listing = stages.map_packet(tree, run)
    assert files == run["files"] and listing == ""
    monkeypatch.setattr(stages, "MAP_PREFILL_MAX_BYTES", 10)
    files, listing = stages.map_packet(tree, run)
    assert files == ["docs/architecture.md"]
    assert "pkg/cli/search.go 3" in listing


def test_map_spawns_the_projects_mapper_seat_with_the_packet_and_the_listing(
        tmp_home, project, fake_rafiki, seats_ok, monkeypatch):
    rdir, run = new_run(tmp_home)
    monkeypatch.setattr(stages, "MAP_PREFILL_MAX_BYTES", 10)
    fake_rafiki.presets["audit:mapper"] = {"kind": "claude"}
    every = [c["name"] for c in runs.run_concerns(rdir) if not c.get("packet")]
    fake_rafiki.answers["map-"] = "```json\n" + json.dumps(
        {"components": [{"component": "all", "paths": ["pkg/**"], "lenses": every}], "seams": []}) + "\n```"
    out = stages.stage_map(fake_rafiki, tmp_home, SimpleNamespace(
        run=run["run_id"], collect=None, mappers=1, model=None, timeout=5))
    assert out["valid"] == 1
    (spawn,) = fake_rafiki.seat_spawns
    assert spawn["preset"] == "audit:mapper"
    assert spawn["packet_files"] == ["docs/architecture.md"]
    assert "FILES (path, lines)" in spawn["instruction"]
    # A cross-cutting lens is not the mapper's to place.
    concerns_line = next(l for l in spawn["instruction"].splitlines() if l.startswith("CONCERNS:"))
    assert "flags" not in concerns_line and "front-end" in concerns_line
    assert fake_rafiki.prompts == []  # the instruction rides the spawn


# --- find cells --------------------------------------------------------------------


def test_cells_place_each_lens_by_its_trigger_and_packet(tmp_home, project):
    rdir, run = new_run(tmp_home)
    promote(rdir, [{"component": "cli", "paths": ["pkg/cli/*"]},
                   {"component": "kube", "paths": ["pkg/kube/*"]}])
    cells = {c["key"]: c for c in stages.find_cells(rdir, run, {"swarm", "cross"}, {})}
    assert "swarm__cli__front-end" in cells
    assert "swarm__kube__front-end" not in cells  # its trigger names pkg/cli only
    assert "swarm__kube__rules" in cells
    assert not any(k.endswith("__flags") and not k.startswith("cross__") for k in cells)
    cross = cells["cross__flags__flags"]
    assert cross["files"] == ["docs/commands/sc_search.md"]
    assert cross["prefill"] == ["docs/commands/sc_search.md", "docs/architecture.md"]
    assert cells["swarm__cli__front-end"]["files"] == ["pkg/cli/fwd.go", "pkg/cli/search.go"]


def test_a_packet_matching_nothing_fails_the_cells_rather_than_skipping_the_lens(tmp_home, project, tmp_path):
    lens_file(tmp_path / "lenses", "ghost", extra="packet: ['nowhere/*']\n")
    rdir, run = new_run(tmp_home)
    promote(rdir, [{"component": "all", "paths": ["pkg/**"]}])
    with pytest.raises(Fail, match="ghost: packet"):
        stages.find_cells(rdir, run, {"cross"}, {})


def test_find_spawns_the_projects_finder_seat(tmp_home, project, fake_rafiki, seats_ok):
    rdir, run = new_run(tmp_home)
    promote(rdir, [{"component": "all", "paths": ["pkg/**"]}])
    fake_rafiki.presets["audit:finder"] = {"kind": "claude"}
    fake_rafiki.answers["find-"] = '```json\n{"findings": []}\n```'
    stages.stage_find(fake_rafiki, tmp_home, SimpleNamespace(
        run=run["run_id"], collect=None, arm=["cross"], only=None, context="component", seat=None,
        variant=None, model=None, rounds=1, parallel=4, timeout=5))
    assert {s["preset"] for s in fake_rafiki.seat_spawns} == {"audit:finder"}
    assert fake_rafiki.prompts == []


# --- verify ------------------------------------------------------------------------


DRIFT = {"id": "d1", "category": "drift", "title": "rule is dead", "file": "pkg/cli/search.go", "line": 2,
         "doc": "docs/architecture.md", "claim": "the front end holds nothing", "evidence": "search.go fetches",
         "arm": "swarm", "subject": "cli", "concern": "rules"}


def test_a_drift_finding_is_judged_against_its_doc_by_the_drift_verifier(tmp_home, project):
    rdir, run = new_run(tmp_home)
    tree = stages.run_cwd(run)
    (job,) = stages.verify_jobs(DRIFT)
    assert job["kind"] == "drift"
    assert stages.verify_prefill(tree, DRIFT, []) == ["pkg/cli/search.go", "docs/architecture.md"]
    task = stages.verify_task(rdir, DRIFT, job)
    assert "DRIFT" in task and "docs/architecture.md" in task and "the front end holds nothing" in task


def test_verify_upholds_drift_on_a_non_refuting_vote_without_a_severity(
        tmp_home, project, fake_rafiki, seats_ok):
    rdir, run = new_run(tmp_home)
    fdir = rdir / "find" / "01"
    fdir.mkdir(parents=True)
    # A finder may attach a severity to drift anyway; it must not survive verify.
    (fdir / "candidates.jsonl").write_text(json.dumps({**DRIFT, "severity": "S2"}) + "\n")
    fake_rafiki.presets["audit:verifier"] = {"kind": "claude"}
    fake_rafiki.answers["verify-"] = '```json\n{"refuted": false, "kind": "contradicted", "reasoning": "r"}\n```'
    out = stages.stage_verify(fake_rafiki, tmp_home, SimpleNamespace(
        run=run["run_id"], collect=None, find="01", only=None, model=None, parallel=2, timeout=5))
    assert out["upheld"] == 1
    (spawn,) = fake_rafiki.seat_spawns
    assert spawn["preset"] == "audit:verifier"
    (row,) = [json.loads(l) for l in (rdir / "verify" / "01" / "verified.jsonl").open()]
    assert row["upheld"] and row["final_severity"] is None


def test_a_project_without_audit_json_keeps_the_review_seats(tmp_home, project):
    (project / "audit.json").unlink()
    (project / "concerns.json").write_text('{"concerns": [{"name": "c", "category": "defect", "prompt": "p"}]}')
    rdir, _ = new_run(tmp_home)
    assert [stages.audit_seat(rdir, r) for r in ("mapper", "finder", "verifier")] == [
        "review:mapper", "review:reviewer", "review:verifier"]
    assert stages.find_seat(rdir, "oneshot") == "review:oneshot"


# --- the map's lens choices ---------------------------------------------------------


def defect_names(rdir):
    return [c["name"] for c in runs.run_concerns(rdir) if c["category"] == "defect" and not c.get("packet")]


def test_a_map_that_places_a_defect_lens_nowhere_is_invalid_unless_find_ignores_choices(
        tmp_home, project):
    rdir, run = new_run(tmp_home)
    doc = {"components": [{"component": "all", "paths": ["pkg/**"], "lenses": ["rules"]}],
           "seams": [{"seam": "s", "components": ["all"], "what": "w", "lenses": ["nope"]}]}
    problems = stages.validate_map(doc, run, *stages.map_lens_names(rdir))
    assert "defect lens security is on no component" in problems
    assert "seam s: unknown concern nope" in problems
    cfg = json.loads((rdir / "project" / "audit.json").read_text())
    (rdir / "project" / "audit.json").write_text(json.dumps({**cfg, "honor_map_lenses": False}))
    problems = stages.validate_map(doc, run, *stages.map_lens_names(rdir))
    assert not any(p.startswith("defect lens") for p in problems)


def test_find_runs_only_the_lenses_the_map_chose_and_records_what_it_cut(tmp_home, project):
    rdir, run = new_run(tmp_home)
    promote(rdir, [{"component": "cli", "paths": ["pkg/cli/*"], "lenses": ["security", "front-end"]},
                   {"component": "kube", "paths": ["pkg/kube/*"], "lenses": defect_names(rdir)}],
            [{"seam": "cli-kube", "components": ["cli", "kube"], "what": "w", "lenses": ["rules"]}])
    cells = stages.find_cells(rdir, run, {"swarm", "seam"}, {})
    keys = {c["key"] for c in cells}
    assert {k for k in keys if k.startswith("swarm__cli__")} == {"swarm__cli__security", "swarm__cli__front-end"}
    assert {k for k in keys if k.startswith("seam__")} == {"seam__cli-kube__rules"}
    skipped = stages.skipped_lenses(rdir, run, cells)
    assert "concurrency" in skipped["swarm/cli"] and "rules" in skipped["swarm/cli"]
    # front-end's trigger names pkg/cli only: off kube by trigger, not by the map.
    assert "front-end" not in skipped["swarm/kube"]
    assert "security" in skipped["seam/cli-kube"]


def test_a_project_can_run_every_lens_regardless_of_the_map(tmp_home, project):
    rdir, run = new_run(tmp_home)
    cfg = json.loads((rdir / "project" / "audit.json").read_text())
    (rdir / "project" / "audit.json").write_text(json.dumps({**cfg, "honor_map_lenses": False}))
    promote(rdir, [{"component": "cli", "paths": ["pkg/cli/*"], "lenses": ["security"]}])
    cells = stages.find_cells(rdir, run, {"swarm"}, {})
    assert len(cells) == len([c for c in runs.run_concerns(rdir) if not c.get("packet")])
    assert stages.skipped_lenses(rdir, run, cells) == {}


# --- per-lens packet shaping and seats --------------------------------------------


def configure(rdir, **extra):
    f = rdir / "project" / "audit.json"
    f.write_text(json.dumps({**json.loads(f.read_text()), **extra}))


def test_tests_leave_the_packet_unless_the_lens_judges_them_and_a_lens_adds_its_context(
        tmp_home, project, tmp_path):
    lens_file(tmp_path / "lenses", "fixtures", extra="tests: true\n")
    lens_file(tmp_path / "lenses", "drift-arch", "drift", extra="context: ['docs/commands/*']\n")
    (tmp_path / "repo" / "pkg" / "cli" / "search_test.go").write_text("package cli\n")
    subprocess.run(["git", "-C", str(tmp_path / "repo"), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path / "repo"), "-c", "user.name=t", "-c", "user.email=t@t",
                    "-c", "commit.gpgsign=false", "commit", "-qm", "t"], check=True)
    rdir, run = new_run(tmp_home)
    configure(rdir, test_globs=["*_test.go"])
    promote(rdir, [{"component": "cli", "paths": ["pkg/cli/*"]}, {"component": "kube", "paths": ["pkg/kube/*"]}])
    cells = {c["key"]: c for c in stages.find_cells(rdir, run, {"swarm"}, {})}
    sec = cells["swarm__cli__security"]
    assert "pkg/cli/search_test.go" not in sec["prefill"]
    assert stages.TESTS_OMITTED in sec["notes"]
    assert "pkg/cli/search_test.go" in cells["swarm__cli__fixtures"]["prefill"]
    assert "docs/commands/sc_search.md" in cells["swarm__cli__drift-arch"]["prefill"]
    assert "docs/commands/sc_search.md" not in sec["prefill"]
    # kube has no tests to drop, so its finder is not told any were dropped.
    assert stages.TESTS_OMITTED not in cells["swarm__kube__security"]["notes"]


def test_a_lens_seat_overrides_the_finder_for_that_lens_only(tmp_home, project, fake_rafiki, seats_ok):
    rdir, run = new_run(tmp_home)
    configure(rdir, lens_seats={"flags": "audit:light"})
    promote(rdir, [{"component": "all", "paths": ["pkg/**"], "lenses": ["security"]}])
    for p in ("audit:finder", "audit:light"):
        fake_rafiki.presets[p] = {"kind": "claude"}
    fake_rafiki.answers["find-"] = '```json\n{"findings": []}\n```'
    stages.stage_find(fake_rafiki, tmp_home, SimpleNamespace(
        run=run["run_id"], collect=None, arm=["swarm", "cross"], only=None, context="component", seat=None,
        variant=None, model=None, rounds=1, parallel=4, timeout=5))
    by = {s["labels"]["review/concern"]: s["preset"] for s in fake_rafiki.seat_spawns}
    assert by == {"flags": "audit:light", "security": "audit:finder"}
    configure(rdir, lens_seats={"nope": "audit:light"})
    with pytest.raises(Fail, match="lens_seats: unknown"):
        stages.find_cells(rdir, run, {"swarm"}, {})


def test_lens_context_and_tests_are_validated(tmp_path):
    with pytest.raises(Fail, match="context must be"):
        lenses.load_lens(lens_file(tmp_path, "c", extra="context: docs/*\n"))
    with pytest.raises(Fail, match="tests must be"):
        lenses.load_lens(lens_file(tmp_path, "t", extra="tests: yes please\n"))


# --- seams review their boundary, not both sides whole ---------------------------


def test_a_seam_reads_its_own_paths_and_one_without_paths_over_the_cap_is_invalid(
        tmp_home, project, monkeypatch):
    rdir, run = new_run(tmp_home)
    comps = [{"component": "cli", "paths": ["pkg/cli/*"], "lenses": defect_names(rdir)},
             {"component": "kube", "paths": ["pkg/kube/*"], "lenses": ["security"]}]
    bounded = {"seam": "b", "components": ["cli", "kube"], "what": "w", "lenses": ["security"],
               "paths": ["pkg/kube/kube.go", "pkg/cli/fwd.go"]}
    whole = {"seam": "w", "components": ["cli", "kube"], "what": "w", "lenses": ["security"]}
    promote(rdir, comps, [bounded, whole])
    cells = {c["key"]: c for c in stages.find_cells(rdir, run, {"seam"}, {})}
    assert cells["seam__b__security"]["files"] == ["pkg/kube/kube.go", "pkg/cli/fwd.go"]
    assert set(cells["seam__w__security"]["files"]) == {"pkg/cli/fwd.go", "pkg/cli/search.go", "pkg/kube/kube.go"}
    args = stages.map_lens_names(rdir)
    assert stages.validate_map({"components": comps, "seams": [bounded, whole]}, run, *args) == []
    monkeypatch.setattr(stages, "SEAM_UNION_MAX_BYTES", 10)
    problems = stages.validate_map({"components": comps, "seams": [bounded, whole]}, run, *args)
    assert any(p.startswith("seam w: its components hold") for p in problems)
    assert not any(p.startswith("seam b:") for p in problems)
    bad = {**bounded, "paths": ["pkg/nope.go", "pkg/zzz/*"]}
    problems = stages.validate_map({"components": comps, "seams": [bad]}, run, *args)
    assert "seam b: unknown path pkg/nope.go" in problems and "seam b: glob pkg/zzz/* matches no file" in problems


# --- clustering, verifier context, the audit report -------------------------------


def finding(fid, line, category="defect", severity="S3", concern="security", file="pkg/cli/fwd.go"):
    return {"id": fid, "category": category, "severity": severity, "concern": concern, "file": file,
            "line": line, "title": f"t-{fid}", "claim": f"c-{fid}", "arm": "swarm", "subject": "cli"}


def test_findings_near_each_other_in_one_file_and_category_are_one_cluster_led_by_the_most_severe():
    fs = [finding("a", 10), finding("b", 18, severity="S2", concern="partial-failure"), finding("c", 40),
          finding("d", 12, category="quality"), finding("e", 11, file="pkg/kube/kube.go")]
    clusters = stages.cluster_findings(fs)
    assert clusters["b"] == ["a", "b"]
    assert {"c", "d", "e"} <= set(clusters)
    assert len(clusters) == 4


def test_a_non_numeric_line_never_crashes_clustering_and_still_parses_its_leading_number():
    clusters = stages.cluster_findings([finding("a", "12-14"), finding("b", "40")])
    assert clusters["a"] == ["a"] and clusters["b"] == ["b"]   # 12 and 40 are far apart
    clusters = stages.cluster_findings([finding("a", "L20"), finding("b", "25")])
    assert clusters["a"] == ["a", "b"]                          # 20 and 25 are one cluster


def test_a_quality_finding_real_but_not_worth_fixing_is_not_upheld(
        tmp_home, project, fake_rafiki, seats_ok):
    rdir, run = new_run(tmp_home)
    fdir = rdir / "find" / "01"
    fdir.mkdir(parents=True)
    (fdir / "candidates.jsonl").write_text(json.dumps(
        {**finding("q", 50, category="quality"), "effort": "S"}) + "\n")
    fake_rafiki.presets["audit:verifier"] = {"kind": "claude"}
    fake_rafiki.answers["verify-"] = '```json\n{"real": true, "worth_it": false, "reasoning": "small"}\n```'
    stages.stage_verify(fake_rafiki, tmp_home, SimpleNamespace(
        run=run["run_id"], collect=None, find="01", only=None, model=None, parallel=1, timeout=5))
    (row,) = [json.loads(l) for l in (rdir / "verify" / "01" / "verified.jsonl").open()]
    assert row["real"] is True and row["upheld"] is False


def test_report_routes_an_audit_run_to_the_audit_report_and_a_pr_run_to_the_pr_report(
        tmp_home, project, monkeypatch):
    from review_swarm import audit_report, cli, pr_report
    rdir, run = new_run(tmp_home)
    calls = []
    monkeypatch.setattr(audit_report, "write_audit_report", lambda r, o: calls.append("audit") or o)
    monkeypatch.setattr(pr_report, "write_report", lambda r, o: calls.append("pr") or o)
    out = tmp_home.path / "r.md"
    cli.report_cmd(tmp_home, SimpleNamespace(run=run["run_id"], out=str(out)))
    assert calls == ["audit"]
    doc = json.loads((rdir / "run.json").read_text())
    (rdir / "run.json").write_text(json.dumps({**doc, "mode": "pr"}))
    cli.report_cmd(tmp_home, SimpleNamespace(run=run["run_id"], out=str(out)))
    assert calls == ["audit", "pr"]


def test_the_verifier_is_told_why_the_lens_exists_and_what_else_was_reported_there(tmp_home, project):
    rdir, _ = new_run(tmp_home)
    f = {**finding("q", 3, category="quality", concern="front-end"), "effort": "M"}
    (job,) = stages.verify_jobs(f)
    task = stages.verify_task(rdir, f, job, [finding("o", 4, concern="flags")])
    assert "Hunt front-end." in task
    assert "[flags] t-o: c-o" in task


def test_verify_checks_each_cluster_once_and_gives_every_member_its_verdict(
        tmp_home, project, fake_rafiki, seats_ok):
    rdir, run = new_run(tmp_home)
    fdir = rdir / "find" / "01"
    fdir.mkdir(parents=True)
    rows = [finding("a", 10), finding("b", 12, concern="partial-failure"),
            {**finding("q", 50, category="quality"), "effort": "S"}]
    (fdir / "candidates.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    fake_rafiki.presets["audit:verifier"] = {"kind": "claude"}
    fake_rafiki.answers["verify-"] = '```json\n{"refuted": false, "severity_adjustment": "none", "reasoning": "r"}\n```'
    stages.stage_verify(fake_rafiki, tmp_home, SimpleNamespace(
        run=run["run_id"], collect=None, find="01", only=["category=defect"], model=None, parallel=2, timeout=5))
    assert len(fake_rafiki.seat_spawns) == 1  # q filtered out; a and b are one cluster
    out = {r["id"]: r for r in map(json.loads, (rdir / "verify" / "01" / "verified.jsonl").open())}
    assert set(out) == {"a", "b"}
    assert out["a"]["upheld"] and out["b"]["upheld"] and out["a"]["cluster"] == out["b"]["cluster"]


def test_the_audit_report_merges_lenses_keeps_real_not_worth_and_lets_a_later_attempt_win(tmp_home, project):
    from review_swarm import audit_report
    rdir, _ = new_run(tmp_home)
    up = {"refuted": False, "reasoning": "holds"}
    v1 = [{**finding("a", 10), "upheld": True, "final_severity": "S3", "verdicts": [up]},
          {**finding("b", 12, concern="partial-failure"), "upheld": True, "final_severity": "S3", "verdicts": [up]},
          {**finding("q", 50, category="quality"), "upheld": False,
           "verdicts": [{"real": True, "worth_it": False, "reasoning": "small"}]}]
    v2 = [{**finding("q", 50, category="quality"), "upheld": True, "real": True,
           "verdicts": [{"real": True, "worth_it": True, "reasoning": "the set matters"}]}]
    for n, rows in (("01", v1), ("02", v2)):
        d = rdir / "verify" / n
        d.mkdir(parents=True)
        (d / "verified.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    text = audit_report.write_audit_report(rdir, tmp_home.path / "r.md").read_text()
    assert "## Defects, upheld (1)" in text and "Also reported as: [partial-failure] t-b" in text
    assert "## Quality, upheld (1)" in text and "the set matters" in text
    (rdir / "verify" / "02" / "verified.jsonl").unlink()
    text = audit_report.write_audit_report(rdir, tmp_home.path / "r.md").read_text()
    assert "## Quality, real but judged not worth fixing (1)" in text


def test_the_mapper_is_told_the_project_s_tools(tmp_home, project, fake_rafiki, seats_ok):
    rdir, run = new_run(tmp_home)
    (rdir / "project" / "tools.md").write_text("Use mcp__codescan__code_trees.\n")
    fake_rafiki.presets["audit:mapper"] = {"kind": "claude"}
    fake_rafiki.answers["map-"] = '```json\n{"components": [], "seams": []}\n```'
    stages.stage_map(fake_rafiki, tmp_home, SimpleNamespace(
        run=run["run_id"], collect=None, mappers=1, model=None, timeout=5))
    (spawn,) = fake_rafiki.seat_spawns
    assert "Use mcp__codescan__code_trees." in spawn["instruction"]
