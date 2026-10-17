# review-swarm: a seat's packet arrives by pre-fill (fundi) or read list (claude)
"""The contract these tests pin down: pre-fill bytes and read-list order are the
caller's on both paths, the read list states what a `path:a-b` entry means, the
instruction is the same text either way, the kind is always the preset's, and a
seat whose preset is missing or is not a review seat kind never spawns anything.
The fake stands in for the SDK client rafiki_io touches."""

from types import SimpleNamespace

from rafiki import ConnectError

from review_swarm.common import AtCap, Fail
from review_swarm.presets import seat_kind
from review_swarm.rafiki_io import Rafiki, packet_prompt

# Deliberately not in lexical order: a spawn_seat that reorders the packet would
# keep the seats' shared pre-fill prefix aligned only by accident.
FILES = ["packet/c.md", "packet/a.md", "packet/b.md:1-20"]
LISTING = "- packet/c.md\n- packet/a.md\n- packet/b.md:1-20"
PACKET_HEADER = "Read these files first, in this order, before anything else; they are the review packet:\n"
RANGE_NOTE = "A path ending in `:a-b` means lines a through b of that file, read in the same order."
INSTRUCTION = "Report findings for your lens."
LABELS = {"review/run": "r1", "review/stage": "find"}


class _Child:
    def __init__(self, cid):
        self.child_id = cid
        self.session_id = f"s_{cid}"


class FakeRafiki:
    """The SDK client surface rafiki_io uses: `presets` keyed by name, every
    spawn recorded as issued, get_preset keyed the way the real daemon is (an
    unknown name is ConnectError not_found)."""

    def __init__(self, presets=None, refuse=None):
        self.presets = presets or {}
        self.spawns = []
        self.refuse = refuse
        # Like the shared harness's fake, rk.c is the fake itself, so helpers
        # that only read rk.c can be driven with either the fake or a Rafiki.
        self.c = self

    def get_preset(self, name):
        if name not in self.presets:
            raise ConnectError("not_found", name)
        return {"version": 1, "spec": self.presets[name]}

    def spawn(self, name, *, kind, cwd, preset=None, model=None, labels=None,
              prompt=None, prefill=None):
        if self.refuse is not None:
            raise ConnectError(*self.refuse)
        self.spawns.append({"name": name, "kind": kind, "cwd": cwd, "preset": preset,
                            "model": model, "labels": labels, "prompt": prompt, "prefill": prefill})
        return f"c_{len(self.spawns)}"

    def get(self, cid):
        return _Child(cid)


def test_fundi_seat_prefills_the_packet_in_the_given_order():
    fake = FakeRafiki({"review-pr:finder": {"kind": "fundi", "model": "m"}})
    cid, sid, delivery = Rafiki(fake).spawn_seat(
        "find-1", preset="review-pr:finder", cwd="/tree",
        packet_files=FILES, instruction=INSTRUCTION, labels=LABELS)
    assert (cid, sid, delivery) == ("c_1", "s_c_1", "prefill")
    assert fake.spawns == [{"name": "find-1", "kind": "fundi", "cwd": "/tree",
                            "preset": "review-pr:finder", "model": None, "labels": LABELS,
                            "prompt": INSTRUCTION,
                            "prefill": [{"path": "packet/c.md"},
                                        {"path": "packet/a.md"},
                                        {"path": "packet/b.md", "start": 1, "end": 20}]}]


def test_claude_seat_reads_the_packet_then_the_instruction():
    fake = FakeRafiki({"review-pr:skeptic": {"kind": "claude"}})
    cid, sid, delivery = Rafiki(fake).spawn_seat(
        "find-1", preset="review-pr:skeptic", cwd="/tree",
        packet_files=FILES, instruction=INSTRUCTION, labels=LABELS, model="sonnet-x")
    assert (cid, sid, delivery) == ("c_1", "s_c_1", "read-list")
    (spawn,) = fake.spawns
    assert spawn["kind"] == "claude"
    assert spawn["prefill"] is None
    assert spawn["model"] == "sonnet-x"
    # The literal expected text, not packet_prompt(...): a read list reordered
    # inside either function has to fail here.
    assert spawn["prompt"] == PACKET_HEADER + LISTING + "\n" + RANGE_NOTE + "\n\n" + INSTRUCTION


def test_packet_prompt_lists_files_in_order_then_the_instruction():
    assert packet_prompt(FILES, INSTRUCTION) == PACKET_HEADER + LISTING + "\n" + RANGE_NOTE + "\n\n" + INSTRUCTION


def test_absent_kind_is_a_fundi_seat():
    fake = FakeRafiki({"review-pr:finder": {"model": "m"}})
    assert seat_kind(fake, "review-pr:finder") == "fundi"
    _, _, delivery = Rafiki(fake).spawn_seat(
        "find-1", preset="review-pr:finder", cwd="/tree",
        packet_files=FILES, instruction=INSTRUCTION, labels=LABELS)
    assert delivery == "prefill"


def test_seat_kind_reads_a_live_row_with_no_spec_wrapper():
    # The daemon's PresetRow carries the spec fields directly, unlike the fake's
    # {"version", "spec"} shape.
    row = SimpleNamespace(kind="claude")
    assert seat_kind(SimpleNamespace(c=SimpleNamespace(get_preset=lambda name: row)),
                     "review-pr:skeptic") == "claude"


def test_seat_kind_reads_the_preset():
    fake = FakeRafiki({"review-pr:finder": {"kind": "fundi"}, "review-pr:skeptic": {"kind": "claude"}})
    assert seat_kind(fake, "review-pr:finder") == "fundi"
    assert seat_kind(fake, "review-pr:skeptic") == "claude"


def test_missing_preset_fails_before_any_spawn():
    fake = FakeRafiki({})
    for call in (lambda: seat_kind(fake, "review-pr:nope"),
                 lambda: Rafiki(fake).spawn_seat("find-1", preset="review-pr:nope", cwd="/tree",
                                                 packet_files=FILES, instruction=INSTRUCTION,
                                                 labels=LABELS)):
        try:
            call()
        except Fail as e:
            assert "review-pr:nope" in str(e)
        else:
            raise AssertionError("expected Fail")
    assert fake.spawns == []


def test_unknown_kind_is_refused_not_spawned_as_fundi():
    fake = FakeRafiki({"review-pr:finder": {"kind": "script"}})
    try:
        Rafiki(fake).spawn_seat("find-1", preset="review-pr:finder", cwd="/tree", packet_files=FILES,
                                instruction=INSTRUCTION, labels=LABELS)
    except Fail as e:
        assert "review-pr:finder" in str(e)
        assert "script" in str(e)
        assert "is not a review seat kind" in str(e)
    else:
        raise AssertionError("expected Fail")
    assert fake.spawns == []


def test_spawn_seat_translates_cap_and_failure_like_spawn():
    at_cap = FakeRafiki({"review-pr:finder": {"kind": "fundi"}},
                        refuse=("resource_exhausted", "spawn refused: 1 live agent(s) already running", None, "at_capacity"))
    try:
        Rafiki(at_cap).spawn_seat("find-1", preset="review-pr:finder", cwd="/tree", packet_files=FILES,
                                  instruction=INSTRUCTION, labels=LABELS)
    except AtCap:
        pass
    else:
        raise AssertionError("expected AtCap")

    gone = FakeRafiki({"review-pr:finder": {"kind": "fundi"}}, refuse=("unavailable", "daemon gone"))
    try:
        Rafiki(gone).spawn_seat("find-1", preset="review-pr:finder", cwd="/tree", packet_files=FILES,
                                instruction=INSTRUCTION, labels=LABELS)
    except Fail as e:
        assert not isinstance(e, AtCap)
        assert "daemon gone" in str(e)
    else:
        raise AssertionError("expected Fail")
