# review-swarm: helpers every other module shares
import datetime
import json
import re
import subprocess
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
PROMPTS = PACKAGE / "prompts"
PRESETS = PACKAGE / "presets"
LENSES = PACKAGE / "lenses"
INCLUDE_RE = re.compile(r"\{\{([A-Za-z0-9_.-]+\.md)\}\}")


def as_line(value):
    """A finding's line as an int: a model may report "12-14", "L12" or a list,
    and an unparseable value must not crash a stage."""
    try:
        return int(str(value).strip().split("-")[0].lstrip("Ll").strip() or 0)
    except (TypeError, ValueError):
        return 0


class Fail(Exception):
    pass


class AtCap(Fail):
    """A spawn refused because the spawner's subtree is at its live-children
    cap: transient, retry once a running child settles."""


def run(cmd, *, env=None, check=True, capture=True, input=None, cwd=None):
    p = subprocess.run(
        [str(c) for c in cmd], env=env, cwd=cwd, input=input,
        capture_output=capture, text=True, check=False,
    )
    if check and p.returncode != 0:
        detail = (p.stderr or p.stdout or "").strip() if capture else ""
        raise Fail(f"{' '.join(str(c) for c in cmd[:3])} … exited {p.returncode}: {detail}")
    return p


def now_iso():
    return datetime.datetime.now(datetime.UTC).isoformat()


def git(target, *args):
    return run(["git", "-C", target, *args]).stdout


def expand_includes(text):
    """Replace {{file.md}} with prompts/file.md's text; returns (text, missing)."""
    missing = []

    def sub(m):
        f = PROMPTS / m.group(1)
        if not f.exists():
            missing.append(m.group(1))
            return m.group(0)
        return f.read_text().strip()

    return INCLUDE_RE.sub(sub, text), missing


def render_prompt(name, **fields):
    text = (PROMPTS / name).read_text()
    for k, v in fields.items():
        text = text.replace("{" + k + "}", v)
    return text


def last_json_block(text):
    """The last fenced ```json block. Trailing commas before } or ] are the one
    repair made: models emit them often, and they carry no ambiguity."""
    blocks = re.findall(r"```json\s*\n(.*?)```", text or "", re.DOTALL)
    if not blocks:
        raise ValueError("no fenced ```json block in the final message")
    try:
        return json.loads(blocks[-1])
    except json.JSONDecodeError:
        return json.loads(re.sub(r",(\s*[}\]])", r"\1", blocks[-1]))


def parse_only(values):
    """--only k=a,b (repeatable) -> {k: {a, b}}."""
    out = {}
    for v in values or []:
        k, _, rest = v.partition("=")
        if not rest:
            raise Fail(f"--only {v!r}: expected key=value[,value]")
        out.setdefault(k, set()).update(x for x in rest.split(",") if x)
    return out


# How wide a PR review fans out, from the run's pr.json. The defaults are the
# lean configuration: the panel answers every always-on lens on its checklist,
# so a finder per always-on lens and a verifier per "checked: nothing" answer
# repeat that work, and a file-group split only earns its seats when the packet
# does not fit a finder. Each widening is a setting, so a run records which
# configuration it was and compare-pr can weigh one against the other.
FANOUT_DEFAULTS = {"finders": "triggered", "split_groups": False, "verify_claims": False}


def fanout(prj, where="pr.json"):
    out = dict(FANOUT_DEFAULTS)
    for key in FANOUT_DEFAULTS:
        if key in prj:
            out[key] = prj[key]
    if out["finders"] not in ("triggered", "all"):
        raise Fail(f"{where}: finders must be \"triggered\" or \"all\", got {out['finders']!r}")
    for key in ("split_groups", "verify_claims"):
        if not isinstance(out[key], bool):
            raise Fail(f"{where}: {key} must be true or false, got {out[key]!r}")
    return out


def finder_lenses(lenses, fan):
    """The lenses that get a finder cell: every one, or only the triggered
    ones (an always-on lens is already on each panel seat's checklist)."""
    return [l for l in lenses if fan["finders"] == "all" or not l.get("always")]
