# review-swarm: lens compiler tests - every load/build error rule, layering,
# byte-determinism of the compiled files, and trigger matching
import hashlib
import json
from pathlib import Path

import pytest

from review_swarm import runs
from review_swarm.common import LENSES as REPO_LENSES
from review_swarm.common import Fail
from review_swarm.lenses import Lens, build, load_layers, load_lens, select

GOOD_FRONT = ("name: {name}\ncategory: defect\napplies_to: [design, pr, delta]\n"
              "triggers: always\n")


def make(d, filename="lens.md", front=None, body="Body text."):
    """A lens file with frontmatter from `front` (default: a valid one named after
    the file). Returns the path so a failure message can be checked against it."""
    name = filename[:-3]
    text = "---\n" + (GOOD_FRONT.format(name=name) if front is None else front) + "---\n" + body
    p = Path(d) / filename
    p.write_text(text)
    return p


def fails(fn, path):
    with pytest.raises(Fail) as exc:
        fn()
    msg = str(exc.value)
    assert str(path) in msg, f"message does not name {path}: {msg}"
    return msg


def mk(name, applies_to=("design", "pr", "delta"), triggers="always"):
    return Lens(name=name, category="defect", applies_to=applies_to, triggers=triggers,
                catches=(), replaces=None, prompt="p", source=f"/x/{name}.md", sha256="0" * 64)


# --- load_lens: the happy path -------------------------------------------------

def test_load_lens_parses_frontmatter_and_hashes_bytes(tmp_path):
    p = make(tmp_path, "liveness.md", body="\n  Keep it simple.  \n",
             front=GOOD_FRONT.format(name="liveness") + "catches: [a-bug]\n")
    lens = load_lens(p)
    assert lens.name == "liveness"
    assert lens.category == "defect"
    assert lens.applies_to == ("design", "pr", "delta")
    assert lens.triggers == "always"
    assert lens.catches == ("a-bug",)
    assert lens.replaces is None
    assert lens.prompt == "Keep it simple."
    assert lens.source == str(p)
    assert lens.sha256 == hashlib.sha256(p.read_bytes()).hexdigest()


def test_load_lens_keeps_a_trigger_mapping(tmp_path):
    p = make(tmp_path, "guarded.md",
             front=GOOD_FRONT.format(name="guarded")
             + "triggers:\n  paths: [src/**]\n  diff_matches: ['retry']\n")
    lens = load_lens(p)
    assert lens.triggers == {"paths": ["src/**"], "diff_matches": ["retry"]}


def test_missing_triggers_defaults_to_always(tmp_path):
    p = make(tmp_path, "quiet.md",
             front="name: quiet\ncategory: quality\napplies_to: [design]\n")
    assert load_lens(p).triggers == "always"


# --- load_lens: one test per error rule ---------------------------------------

def test_missing_frontmatter(tmp_path):
    p = tmp_path / "lens.md"
    p.write_text("Just a body, no frontmatter.\n")
    fails(lambda: load_lens(p), p)


def test_unclosed_frontmatter(tmp_path):
    p = tmp_path / "lens.md"
    p.write_text("---\nname: lens\ncategory: defect\napplies_to: [design]\n\nbody\n")
    fails(lambda: load_lens(p), p)


def test_empty_frontmatter(tmp_path):
    p = make(tmp_path, front="")
    fails(lambda: load_lens(p), p)


def test_invalid_yaml(tmp_path):
    p = make(tmp_path, front="name: [unclosed\n")
    fails(lambda: load_lens(p), p)


def test_not_utf8(tmp_path):
    p = tmp_path / "lens.md"
    p.write_bytes(b"---\nname: lens\ncategory: defect\napplies_to: [design]\n---\n\xff\xfe\n")
    fails(lambda: load_lens(p), p)


def test_unknown_key(tmp_path):
    p = make(tmp_path, front=GOOD_FRONT.format(name="lens") + "severity: high\n")
    fails(lambda: load_lens(p), p)


def test_missing_name(tmp_path):
    p = make(tmp_path, front="category: defect\napplies_to: [design]\n")
    fails(lambda: load_lens(p), p)


def test_missing_category(tmp_path):
    p = make(tmp_path, front="name: lens\napplies_to: [design]\n")
    fails(lambda: load_lens(p), p)


def test_missing_applies_to(tmp_path):
    p = make(tmp_path, front="name: lens\ncategory: defect\n")
    fails(lambda: load_lens(p), p)


def test_bad_category(tmp_path):
    p = make(tmp_path, front="name: lens\ncategory: style\napplies_to: [design]\n")
    fails(lambda: load_lens(p), p)


def test_bad_mode_in_applies_to(tmp_path):
    p = make(tmp_path, front="name: lens\ncategory: defect\napplies_to: [design, review]\n")
    fails(lambda: load_lens(p), p)


def test_triggers_not_always_nor_mapping(tmp_path):
    p = make(tmp_path, front=GOOD_FRONT.format(name="lens") + "triggers: sometimes\n")
    fails(lambda: load_lens(p), p)


def test_triggers_unknown_key(tmp_path):
    p = make(tmp_path, front=GOOD_FRONT.format(name="lens") + "triggers:\n  symbols: [x]\n")
    fails(lambda: load_lens(p), p)


def test_triggers_paths_not_a_list_of_strings(tmp_path):
    p = make(tmp_path, front=GOOD_FRONT.format(name="lens") + "triggers:\n  paths: src/**\n")
    fails(lambda: load_lens(p), p)


def test_diff_matches_pattern_is_compiled_at_load(tmp_path):
    # A bad pattern must fail the build, not the run that first matches it.
    p = make(tmp_path, front=GOOD_FRONT.format(name="lens")
             + "triggers:\n  diff_matches: ['\\+.*retry', '(']\n")
    fails(lambda: load_lens(p), p)
    fails(lambda: build(tmp_path, None, set(), tmp_path / "out"), p)


def test_empty_body(tmp_path):
    p = make(tmp_path, body="   \n\n")
    fails(lambda: load_lens(p), p)


# --- load_layers: layering -----------------------------------------------------

def test_duplicate_name_in_one_layer(tmp_path):
    make(tmp_path, "a.md", front=GOOD_FRONT.format(name="dup"))
    p2 = make(tmp_path, "b.md", front=GOOD_FRONT.format(name="dup"))
    fails(lambda: load_layers(tmp_path), p2)


def test_repo_lens_without_replaces_collides(tmp_path):
    gdir, rdir = tmp_path / "g", tmp_path / "r"
    gdir.mkdir()
    rdir.mkdir()
    make(gdir, "liveness.md", front=GOOD_FRONT.format(name="liveness"))
    p = make(rdir, "liveness.md", front=GOOD_FRONT.format(name="liveness"))
    fails(lambda: load_layers(gdir, rdir), p)


def test_replaces_unknown_lens(tmp_path):
    gdir, rdir = tmp_path / "g", tmp_path / "r"
    gdir.mkdir()
    rdir.mkdir()
    make(gdir, "liveness.md", front=GOOD_FRONT.format(name="liveness"))
    p = make(rdir, "here.md", front=GOOD_FRONT.format(name="here") + "replaces: nope\n")
    fails(lambda: load_layers(gdir, rdir), p)


def test_override_with_replaces_takes_its_place(tmp_path):
    gdir, rdir = tmp_path / "g", tmp_path / "r"
    gdir.mkdir()
    rdir.mkdir()
    make(gdir, "liveness.md", front=GOOD_FRONT.format(name="liveness"), body="generic")
    make(gdir, "transitions.md", front=GOOD_FRONT.format(name="transitions"))
    make(rdir, "liveness.md", front=GOOD_FRONT.format(name="liveness") + "replaces: liveness\n",
         body="repo")
    got = load_layers(gdir, rdir)
    assert [l.name for l in got] == ["liveness", "transitions"]
    assert [l.prompt for l in got if l.name == "liveness"] == ["repo"]


def test_replacement_under_a_new_name(tmp_path):
    gdir, rdir = tmp_path / "g", tmp_path / "r"
    gdir.mkdir()
    rdir.mkdir()
    make(gdir, "old.md", front=GOOD_FRONT.format(name="old"))
    make(rdir, "new.md", front=GOOD_FRONT.format(name="new") + "replaces: old\n", body="new body")
    got = load_layers(gdir, rdir)
    assert [l.name for l in got] == ["new"]
    assert got[0].prompt == "new body"


def test_replacement_lands_when_the_replacer_sorts_first(tmp_path):
    # The replacer is a.md and the lens it replaces is b.md, so nothing is in the
    # merged set yet when a.md is read: the replaced name has to be dropped from
    # both layers up front or both lenses ship.
    gdir, rdir = tmp_path / "g", tmp_path / "r"
    gdir.mkdir()
    rdir.mkdir()
    make(gdir, "transitions.md", front=GOOD_FRONT.format(name="transitions"))
    make(rdir, "a.md", front=GOOD_FRONT.format(name="new") + "replaces: old\n", body="new body")
    make(rdir, "b.md", front=GOOD_FRONT.format(name="old"), body="old body")
    got = load_layers(gdir, rdir)
    assert [l.name for l in got] == ["new", "transitions"]
    assert [l.prompt for l in got if l.name == "new"] == ["new body"]


def test_generic_replaced_by_a_replacer_that_sorts_first(tmp_path):
    gdir, rdir = tmp_path / "g", tmp_path / "r"
    gdir.mkdir()
    rdir.mkdir()
    make(gdir, "old.md", front=GOOD_FRONT.format(name="old"))
    make(rdir, "a.md", front=GOOD_FRONT.format(name="new") + "replaces: old\n", body="new body")
    assert [l.name for l in load_layers(gdir, rdir)] == ["new"]


def test_two_repo_lenses_replacing_one_target_fail_naming_both(tmp_path):
    # Two replacers of one target would both ship and the second would silently
    # win in a run; the design makes build the gate that fails on the duplicate.
    gdir, rdir = tmp_path / "g", tmp_path / "r"
    gdir.mkdir()
    rdir.mkdir()
    make(gdir, "old.md", front=GOOD_FRONT.format(name="old"))
    a = make(rdir, "a.md", front=GOOD_FRONT.format(name="new-a") + "replaces: old\n", body="a")
    b = make(rdir, "b.md", front=GOOD_FRONT.format(name="new-b") + "replaces: old\n", body="b")
    msg = fails(lambda: load_layers(gdir, rdir), b)
    assert "old" in msg
    assert str(a) in msg, f"message does not name the earlier replacer {a}: {msg}"
    fails(lambda: build(gdir, rdir, set(), tmp_path / "out"), b)


def test_load_layers_without_a_repo_layer_is_sorted(tmp_path):
    make(tmp_path, "b.md", front=GOOD_FRONT.format(name="bee"))
    make(tmp_path, "a.md", front=GOOD_FRONT.format(name="aye"))
    assert [l.name for l in load_layers(tmp_path, None)] == ["aye", "bee"]


# --- the home layer ------------------------------------------------------------

def test_home_layer_adds_a_lens(tmp_path):
    shipped, home = tmp_path / "shipped", tmp_path / "home"
    shipped.mkdir()
    home.mkdir()
    make(shipped, "a.md", front=GOOD_FRONT.format(name="a"))
    make(home, "b.md", front=GOOD_FRONT.format(name="b"))
    assert [l.name for l in load_layers(shipped, None, home_dir=home)] == ["a", "b"]


def test_a_dir_named_as_both_home_and_project_layer_loads_once(tmp_path):
    shipped, shared = tmp_path / "shipped", tmp_path / "shared"
    shipped.mkdir()
    shared.mkdir()
    make(shared, "a.md", front=GOOD_FRONT.format(name="a"))
    got = load_layers(shipped, shared, home_dir=shared)
    assert [l.name for l in got] == ["a"]


def test_project_layer_may_replace_a_home_lens(tmp_path):
    shipped, home, project = tmp_path / "shipped", tmp_path / "home", tmp_path / "project"
    for d in (shipped, home, project):
        d.mkdir()
    make(home, "h.md", front=GOOD_FRONT.format(name="h"))
    make(project, "p.md", front=GOOD_FRONT.format(name="p") + "replaces: h\n")
    assert [l.name for l in load_layers(shipped, project, home_dir=home)] == ["p"]


def test_home_lens_may_replace_a_shipped_lens(tmp_path):
    shipped, home = tmp_path / "shipped", tmp_path / "home"
    shipped.mkdir()
    home.mkdir()
    make(shipped, "a.md", front=GOOD_FRONT.format(name="a"))
    make(home, "a2.md", front=GOOD_FRONT.format(name="a2") + "replaces: a\n")
    assert [l.name for l in load_layers(shipped, None, home_dir=home)] == ["a2"]


def test_home_lens_name_clash_requires_replaces(tmp_path):
    shipped, home = tmp_path / "shipped", tmp_path / "home"
    shipped.mkdir()
    home.mkdir()
    make(shipped, "a.md", front=GOOD_FRONT.format(name="a"))
    p = make(home, "a.md", front=GOOD_FRONT.format(name="a"))
    msg = fails(lambda: load_layers(shipped, None, home_dir=home), p)
    assert "already taken by" in msg


def test_unknown_replaces_in_home_layer_fails(tmp_path):
    shipped, home = tmp_path / "shipped", tmp_path / "home"
    shipped.mkdir()
    home.mkdir()
    p = make(home, "h.md", front=GOOD_FRONT.format(name="h") + "replaces: nope\n")
    msg = fails(lambda: load_layers(shipped, None, home_dir=home), p)
    assert "replaces unknown lens" in msg


def test_project_layer_replacing_same_target_twice_fails(tmp_path):
    shipped, project = tmp_path / "shipped", tmp_path / "project"
    shipped.mkdir()
    project.mkdir()
    make(shipped, "a.md", front=GOOD_FRONT.format(name="a"))
    make(project, "x.md", front=GOOD_FRONT.format(name="x") + "replaces: a\n")
    p = make(project, "y.md", front=GOOD_FRONT.format(name="y") + "replaces: a\n")
    msg = fails(lambda: load_layers(shipped, project), p)
    assert "two lenses replace" in msg


def test_compile_audit_lenses_sees_the_home_layer(tmp_path):
    pdir, repo, home = tmp_path / "p", tmp_path / "repo", tmp_path / "home"
    for d in (pdir, repo, home):
        d.mkdir()
    (pdir / "audit.json").write_text(json.dumps({"lens_dir": str(repo)}))
    make(home, "home-lens.md",
         front="name: home-lens\ncategory: defect\napplies_to: [audit]\n")
    concerns = runs.compile_audit_lenses(pdir, home_dir=home)
    assert "home-lens" in [c["name"] for c in concerns]


def test_generic_lenses_compile(tmp_path):
    lenses = load_layers(REPO_LENSES, None)
    assert [l.name for l in lenses] == [
        "algo-bigo", "concurrency", "fixtures-through-real-path", "liveness",
        "resource-lifecycle", "security", "silent-failure", "stale-state",
        "state-data-loss", "transitions"]
    for lens in lenses:
        assert lens.category in ("defect", "quality")
        assert lens.applies_to == ("design", "pr", "delta", "audit")
        assert lens.triggers == "always"
        assert lens.catches == ()
        assert lens.prompt


# --- build ---------------------------------------------------------------------

def test_build_is_byte_identical_across_calls(tmp_path):
    gdir = tmp_path / "g"
    gdir.mkdir()
    make(gdir, "a.md", front=GOOD_FRONT.format(name="aye"))
    make(gdir, "b.md", front=GOOD_FRONT.format(name="bee"))
    out1, out2 = tmp_path / "o1", tmp_path / "o2"
    build(gdir, None, set(), out1)
    build(gdir, None, set(), out2)
    for name in ("concerns.json", "lenses.manifest.json"):
        assert (out1 / name).read_bytes() == (out2 / name).read_bytes()


def test_build_concerns_json_has_the_stage_shape(tmp_path):
    gdir = tmp_path / "g"
    gdir.mkdir()
    make(gdir, "a.md", front=GOOD_FRONT.format(name="aye"), body="Hunt it.")
    out = tmp_path / "out"
    build(gdir, None, set(), out)
    doc = json.loads((out / "concerns.json").read_text())
    assert list(doc) == ["concerns"]
    entry = doc["concerns"][0]
    assert set(entry) == {"name", "category", "prompt"}
    assert entry == {"name": "aye", "category": "defect", "prompt": "Hunt it."}


def test_build_manifest_records_source_and_hash(tmp_path):
    gdir = tmp_path / "g"
    gdir.mkdir()
    p = make(gdir, "a.md", front=GOOD_FRONT.format(name="aye") + "catches: [a-bug]\n")
    out = tmp_path / "out"
    build(gdir, None, {"a-bug"}, out)
    entry = json.loads((out / "lenses.manifest.json").read_text())["lenses"][0]
    assert set(entry) == {"name", "source", "sha256", "catches", "applies_to", "triggers"}
    assert entry["name"] == "aye"
    assert entry["source"] == str(p)
    assert entry["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()
    assert entry["catches"] == ["a-bug"]
    assert entry["applies_to"] == ["design", "pr", "delta"]
    assert entry["triggers"] == "always"


def test_build_rejects_unknown_catches_slug(tmp_path):
    gdir = tmp_path / "g"
    gdir.mkdir()
    p = make(gdir, "a.md", front=GOOD_FRONT.format(name="aye") + "catches: [not-in-gold]\n")
    fails(lambda: build(gdir, None, {"other"}, tmp_path / "out"), p)


# --- select --------------------------------------------------------------------

def test_select_always_trigger_and_mode_filter():
    always = mk("always")
    design_only = mk("design-only", applies_to=("design",))
    got = select([always, design_only], "pr", ["src/a.rs"], "")
    assert [l.name for l in got] == ["always"]
    assert [l.name for l in select([always, design_only], "design", [], "")] == \
        ["always", "design-only"]


def test_select_paths_trigger():
    lens = mk("guarded", triggers={"paths": ["src/**", "build.rs"]})
    assert select([lens], "pr", ["src/deep/a.rs"], "") == [lens]
    assert select([lens], "pr", ["build.rs"], "") == [lens]
    assert select([lens], "pr", ["docs/readme.md"], "") == []


def test_select_diff_matches_trigger():
    # The pattern is searched over the whole diff text, so an author who wants a
    # per-line anchor writes it in the pattern.
    lens = mk("guarded", triggers={"diff_matches": [r"(?m)^\+\s*retry"]})
    diff = "--- a/x\n+++ b/x\n+    retry = 3\n"
    assert select([lens], "pr", [], diff) == [lens]
    assert select([lens], "pr", [], "-    retry = 3\n") == []


def test_select_empty_trigger_mapping_matches_nothing():
    assert select([mk("guarded", triggers={})], "pr", ["src/a.rs"], "anything") == []


def test_select_sorted_by_name():
    lenses = [mk("zeta"), mk("alpha")]
    assert [l.name for l in select(lenses, "delta", [], "")] == ["alpha", "zeta"]


def test_select_rejects_unknown_mode():
    with pytest.raises(Fail):
        select([mk("a")], "review", [], "")


def test_a_named_lens_directory_that_does_not_exist_fails(tmp_path):
    shipped = tmp_path / "shipped"
    shipped.mkdir()
    make(shipped, "a.md", front=GOOD_FRONT.format(name="a"))
    with pytest.raises(Fail) as exc:
        load_layers(shipped, tmp_path / "nope")
    assert "lens directory not found" in str(exc.value)
