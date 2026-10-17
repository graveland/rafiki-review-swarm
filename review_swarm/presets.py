# review-swarm: the review:* preset seats, resolved from presets/ + prompts/, synced to a daemon, checked before a stage spawns
import json

from rafiki import ConnectError

from .common import PRESETS, Fail, expand_includes

# PresetRow fields a seat is compared on: everything a put sets.
SEAT_FIELDS = ("name", "description", "kind", "provider", "model", "thinking", "executor", "labels",
               "tools", "skills", "mcp_servers", "context_files", "system_prompt", "append_system_prompt",
               "max_cost", "max_depth", "max_children")


def resolved_preset(path):
    spec = json.loads(path.read_text())
    missing = []
    for k in ("system_prompt", "append_system_prompt"):
        if isinstance(spec.get(k), str):
            spec[k], m = expand_includes(spec[k])
            missing += m
    return spec, missing


def checkout_seats():
    """{name: resolved spec} for every seat file whose prompts all exist."""
    out = {}
    for path in sorted(PRESETS.glob("*.json")):
        spec, missing = resolved_preset(path)
        if missing:
            raise Fail(f"{path.name}: prompt files missing: {', '.join(missing)}")
        out[spec["name"]] = spec
    return out


def comparable(seat):
    """A PresetRow or a spec dict as {field: value}, unset fields dropped.
    The tri-state lists stay distinct: None (absent, the kind's default) is
    dropped, [] (none) is kept."""
    out = {}
    for k in SEAT_FIELDS:
        v = seat.get(k) if isinstance(seat, dict) else getattr(seat, k)
        if hasattr(v, "items") and not isinstance(v, dict):
            v = list(v.items)  # StringList
        if v is None or v == "" or v == {}:
            continue
        out[k] = v
    return out


def live_seat(rk, name):
    try:
        return rk.c.get_preset(name)
    except ConnectError as e:
        if e.code == "not_found":
            return None
        raise Fail(f"preset get {name}: {e}") from e


def seat_kind(rk, preset):
    """The kind a seat's preset runs as: "fundi" unless the preset says
    otherwise. The kind is seat policy, not a code choice: it decides whether
    the packet can be pre-filled or has to arrive as a read list."""
    live = live_seat(rk, preset)
    if live is None:
        raise Fail(f"preset {preset} not found")
    # A live PresetRow carries the spec fields on itself; the test fake wraps
    # them under "spec", so accept either shape.
    spec = (live.get("spec") or live) if isinstance(live, dict) else live
    kind = spec.get("kind") if isinstance(spec, dict) else getattr(spec, "kind", None)
    return kind or "fundi"


def sync(rk):
    for name, spec in checkout_seats().items():
        live = live_seat(rk, name)
        if live is not None and comparable(live) == comparable(spec):
            print(f"{name}: unchanged (version {live.version})")
            continue
        try:
            row = rk.c.put_preset(spec)
        except ConnectError as e:
            raise Fail(f"preset put {name}: {e}") from e
        print(f"{name}: put (version {row.version})")


def check_seats(rk, names):
    """{name: version} of the daemon's seats, refusing when any differs from
    the checkout this code was read from: a git-source refresh updates the
    prompts a stage renders but never the presets its workers run on."""
    want = checkout_seats()
    versions, drift = {}, []
    for name in names:
        live = live_seat(rk, name)
        if live is None:
            drift.append(f"{name} (missing)")
        elif comparable(live) != comparable(want[name]):
            drift.append(f"{name} (version {live.version})")
        else:
            versions[name] = live.version
    if drift:
        raise Fail(f"presets differ from this checkout: {', '.join(drift)}; run `review-swarm presets sync`")
    return versions


def check_resumed(rk, names, recorded):
    """A resumed attempt spawns more workers: refuse if its seats moved since
    the attempt started, so one attempt never mixes two versions of a seat."""
    now = check_seats(rk, names)
    if recorded is not None and now != recorded:
        raise Fail(f"presets changed since this attempt started: {recorded} -> {now}; start a new attempt")
    return now
