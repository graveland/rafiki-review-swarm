# review-swarm: lenses - markdown lens files compiled into the concerns.json the
# existing stages read, plus the manifest that pins what they were compiled from
import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .common import Fail

LENS_KEYS = {"name", "category", "applies_to", "triggers", "catches", "replaces", "packet", "context", "tests"}
CATEGORIES = {"defect", "quality", "drift"}
# "audit" is the whole-repo pipeline (map, find, verify); the rest are PR modes.
MODES = {"design", "pr", "delta", "audit"}
TRIGGER_KEYS = {"paths", "diff_matches"}

# The closing delimiter must sit on its own line; the body is non-greedy so a
# later "---" (a horizontal rule in the prompt) does not end the frontmatter.
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)


@dataclass(frozen=True)
class Lens:
    name: str
    category: str            # "defect" | "quality" | "drift"
    applies_to: tuple[str, ...]   # subset of MODES
    triggers: str | dict     # "always" or {"paths": [...globs], "diff_matches": [...regex]}
    catches: tuple[str, ...]
    replaces: str | None
    prompt: str              # markdown body, stripped
    source: str              # file path it came from
    sha256: str              # hex of the file's bytes
    packet: tuple[str, ...] = ()  # audit: globs of a cross-cutting cell's files; empty = per component
    context: tuple[str, ...] = ()  # audit: globs of extra files every cell of this lens reads
    tests: bool = False      # audit: keep the subject's tests in the packet (audit.json test_globs)


def load_lens(path: Path) -> Lens:
    path = Path(path)
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise Fail(f"{path}: lens file is not utf-8: {e}")
    if not text.startswith("---\n"):
        raise Fail(f"{path}: missing frontmatter (the file must start with a '---' line)")
    m = _FRONTMATTER.match(text)
    if not m:
        raise Fail(f"{path}: missing frontmatter (no closing '---' line)")
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        raise Fail(f"{path}: frontmatter is not valid yaml: {e}")
    if not isinstance(fm, dict):
        raise Fail(f"{path}: frontmatter is empty; it must be a mapping of lens fields")

    unknown = sorted(set(fm) - LENS_KEYS)
    if unknown:
        raise Fail(f"{path}: unknown frontmatter key(s): {', '.join(unknown)}")
    missing = [k for k in ("name", "category", "applies_to") if k not in fm]
    if missing:
        raise Fail(f"{path}: missing required frontmatter key(s): {', '.join(missing)}")

    name = fm["name"]
    if not isinstance(name, str) or not name.strip():
        raise Fail(f"{path}: name must be a non-empty string")

    category = fm["category"]
    if category not in CATEGORIES:
        raise Fail(f"{path}: category {category!r} is not one of {sorted(CATEGORIES)}")

    applies_to = fm["applies_to"]
    if not isinstance(applies_to, list):
        raise Fail(f"{path}: applies_to must be a list of modes")
    for mode in applies_to:
        if mode not in MODES:
            raise Fail(f"{path}: applies_to entry {mode!r} is not one of {sorted(MODES)}")
    # Only the audit pipeline has a drift verifier; a PR run would drop the
    # findings on the floor.
    if category == "drift" and set(applies_to) - {"audit"}:
        raise Fail(f"{path}: a drift lens applies only to audit")

    # A lens that says nothing about triggering applies wherever its modes do:
    # the alternative (silently never selected) is the worse failure.
    triggers = fm.get("triggers", "always")
    if triggers != "always":
        if not isinstance(triggers, dict) or not set(triggers) <= TRIGGER_KEYS:
            raise Fail(f"{path}: triggers must be 'always' or a mapping with keys"
                       f" from {sorted(TRIGGER_KEYS)}")
        for key in sorted(triggers):
            val = triggers[key]
            if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
                raise Fail(f"{path}: triggers.{key} must be a list of strings")
        # Compiled here, not in select: build is the validation gate, so a bad
        # pattern must fail the build rather than the run that first matches it.
        for rx in triggers.get("diff_matches", []):
            try:
                re.compile(rx)
            except re.error as e:
                raise Fail(f"{path}: diff_matches pattern {rx!r} is not a valid regex: {e}")

    catches = fm.get("catches")
    if catches is None:
        catches = ()
    elif isinstance(catches, list) and all(isinstance(c, str) for c in catches):
        catches = tuple(catches)
    else:
        raise Fail(f"{path}: catches must be a list of gold slugs")

    replaces = fm.get("replaces")
    if replaces is not None and not isinstance(replaces, str):
        raise Fail(f"{path}: replaces must be the name of the lens it replaces")

    packet = fm.get("packet")
    if packet is None:
        packet = ()
    elif isinstance(packet, list) and packet and all(isinstance(g, str) for g in packet):
        packet = tuple(packet)
    else:
        raise Fail(f"{path}: packet must be a non-empty list of path globs")

    context = fm.get("context")
    if context is None:
        context = ()
    elif isinstance(context, list) and context and all(isinstance(g, str) for g in context):
        context = tuple(context)
    else:
        raise Fail(f"{path}: context must be a non-empty list of path globs")

    tests = fm.get("tests", False)
    if not isinstance(tests, bool):
        raise Fail(f"{path}: tests must be true or false")

    prompt = m.group(2).strip()
    if not prompt:
        raise Fail(f"{path}: empty lens body")

    return Lens(
        name=name.strip(),
        category=category,
        applies_to=tuple(applies_to),
        triggers=triggers,
        catches=catches,
        replaces=replaces,
        prompt=prompt,
        packet=packet,
        context=context,
        tests=tests,
        source=str(path),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _load_dir(d):
    """Every *.md in one layer, sorted by filename; sorting first makes duplicate
    detection deterministic about which file is reported as the duplicate."""
    d = Path(d)
    if not d.is_dir():
        raise Fail(f"{d}: lens directory not found")
    seen = {}
    lenses = []
    for f in sorted(d.glob("*.md")):
        lens = load_lens(f)
        if lens.name in seen:
            raise Fail(f"{f}: lens name {lens.name!r} already defined by {seen[lens.name]}")
        seen[lens.name] = f
        lenses.append(lens)
    return lenses


def _overlay(base: list[Lens], layer: list[Lens]) -> list[Lens]:
    # A repo lens may shadow another lens only by declaring it: `replaces` is
    # checked against every lens on disk (both layers) so a chain of repo lenses
    # replacing each other still names something real. A target may be replaced
    # once and once only: two repo lenses replacing the same lens would both ship
    # and the second would silently win in a run, so build is the gate that fails
    # on the duplicate and names both files.
    known = {l.name for l in base + layer}
    replaced = {}
    for lens in layer:
        if lens.replaces is not None:
            if lens.replaces not in known:
                raise Fail(f"{lens.source}: replaces unknown lens {lens.replaces!r}")
            if lens.replaces in replaced:
                raise Fail(f"{lens.source}: two lenses replace {lens.replaces!r}:"
                           f" it is already replaced by {replaced[lens.replaces]}")
            replaced[lens.replaces] = lens.source

    # Every replaced name leaves both layers before anything is merged, so a repo
    # override lands whichever of the two files sorts first.
    out = {l.name: l for l in base if l.name not in replaced}
    for lens in layer:
        if lens.name in replaced and lens.replaces != lens.name:
            continue  # this lens is what another repo lens replaces
        if lens.name in out:
            raise Fail(f"{lens.source}: lens name {lens.name!r} already taken by"
                       f" {out[lens.name].source}; set replaces: {lens.name} to replace it")
        out[lens.name] = lens
    return list(out.values())


def load_layers(generic_dir: Path, repo_dir: Path | None = None, *,
                home_dir: Path | None = None) -> list[Lens]:
    out = _load_dir(generic_dir)
    # A directory named as two layers (a home whose lenses/ is also the project's
    # lens_dir) is still one layer: loading it twice would report every name in it
    # as a collision with itself. The shipped dir is not seeded here: a lens_dir
    # that points at it is a mistake and must fail loudly, not be skipped.
    seen = set()
    for d in (home_dir, repo_dir):
        if d is not None:
            rd = Path(d).resolve()
            if rd in seen:
                continue
            seen.add(rd)
            out = _overlay(out, _load_dir(d))
    return sorted(out, key=lambda l: l.name)


def _dump(path: Path, obj):
    # sort_keys is not cosmetic: the manifest is hashed and the seat packet is
    # prefilled from these bytes, so the same inputs must give the same bytes.
    path.write_text(json.dumps(obj, indent=1, sort_keys=True) + "\n")


def build(generic_dir: Path, repo_dir: Path | None, gold_slugs: set[str],
          out_dir: Path, *, home_dir: Path | None = None) -> list[Lens]:
    lenses = load_layers(generic_dir, repo_dir, home_dir=home_dir)
    for lens in lenses:
        for slug in lens.catches:
            if slug not in gold_slugs:
                raise Fail(f"{lens.source}: catches unknown gold slug {slug!r}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # concerns.json is the shape stages.py already consumes via run_concerns.
    _dump(out_dir / "concerns.json", {"concerns": [
        {"name": l.name, "category": l.category, "prompt": l.prompt} for l in lenses]})
    _dump(out_dir / "lenses.manifest.json", {"lenses": [
        {"name": l.name, "source": l.source, "sha256": l.sha256, "catches": list(l.catches),
         "applies_to": list(l.applies_to), "triggers": l.triggers} for l in lenses]})
    return lenses


def _matches(lens: Lens, changed_paths, diff_text) -> bool:
    if lens.triggers == "always":
        return True
    paths = lens.triggers.get("paths", [])
    if any(fnmatch.fnmatchcase(p, g) for p in changed_paths for g in paths):
        return True
    for rx in lens.triggers.get("diff_matches", []):
        # load_lens already compiled these, so this fires only for a Lens built by
        # hand; it is still a Fail naming the file rather than a bare re.error.
        try:
            if re.search(rx, diff_text or ""):
                return True
        except re.error as e:
            raise Fail(f"{lens.source}: diff_matches pattern {rx!r} is not a valid regex: {e}")
    return False


def select(lenses: list[Lens], mode: str, changed_paths: list[str],
           diff_text: str) -> list[Lens]:
    # An unknown mode would silently select nothing, which reads as "no lens
    # applies" rather than as the caller bug it is.
    if mode not in MODES:
        raise Fail(f"unknown mode {mode!r}: expected one of {sorted(MODES)}")
    return sorted((l for l in lenses
                   if mode in l.applies_to and _matches(l, changed_paths, diff_text)),
                  key=lambda l: l.name)


def audit_concerns(lenses: list[Lens], exclude=()) -> list[dict]:
    """The audit lenses as the concern dicts the whole-repo stages read. The
    triggers and packet ride along: find uses them to place each lens."""
    unknown = sorted(set(exclude) - {l.name for l in lenses})
    if unknown:
        raise Fail(f"exclude_lenses names unknown lens(es): {', '.join(unknown)}")
    return [{"name": l.name, "category": l.category, "prompt": l.prompt,
             "triggers": l.triggers, "packet": list(l.packet), "context": list(l.context), "tests": l.tests}
            for l in lenses if "audit" in l.applies_to and l.name not in exclude]
