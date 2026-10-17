# tests: a driver-free stand-in for the rafiki client, so stage and PR-run
# code can be exercised without a daemon, an executor or a real model.
import pytest

from review_swarm import runs
from review_swarm.common import AtCap, Fail


class FakeRafiki:
    """The slice of review_swarm.rafiki_io.Rafiki the tests drive: spawns are
    recorded instead of sent, answers come from `answers`, and `refuse_next`
    injects the spawner-cap refusal a test needs to exercise the requeue rule.

    `self.c` is the fake itself, so code reaching through the client handle
    (`rk.c.get_preset(...)`) works unchanged.
    """

    def __init__(self):
        self.spawns: list[dict] = []
        self.seat_spawns: list[dict] = []
        self.presets: dict[str, dict] = {}
        self.answers: dict[str, str] = {}
        self.prompts: list[dict] = []
        self.stops: list[str] = []
        self.results: list = []
        self.refuse_next = 0
        self._n = 0
        self._children: dict[str, dict] = {}
        self.c = self

    def _accept(self, name, labels):
        """Register one child and hand back its (child_id, session_id) pair.
        A refused call never gets here: it spawned nothing to record."""
        if self.refuse_next > 0:
            self.refuse_next -= 1
            raise AtCap("live agent(s) already running")
        self._n += 1
        cid, sid = f"c_{self._n}", f"s_{self._n}"
        self._children[cid] = {"name": name, "session_id": sid, "labels": dict(labels or {})}
        return cid, sid

    def spawn(self, name, *, cwd, prefill_lines, labels, preset=None, model=None, prompt_text=None):
        cid, sid = self._accept(name, labels)
        self.spawns.append({"name": name, "cwd": str(cwd), "prefill_lines": list(prefill_lines or []),
                            "labels": dict(labels or {}), "preset": preset, "model": model,
                            "prompt_text": prompt_text, "child_id": cid, "session_id": sid})
        return cid, sid

    def spawn_seat(self, name, *, preset, cwd, packet_files, instruction, labels, model=None):
        """The PR stages' seat spawn: same bookkeeping, plus the delivery mode
        the seat's kind forces (a claude-kind seat cannot be pre-filled, so it
        reads the packet from the run directory instead)."""
        spec = self.presets.get(preset)
        if spec is None:
            raise Fail(f"preset {preset!r}: not found (known: {', '.join(sorted(self.presets))})")
        cid, sid = self._accept(name, labels)
        self.seat_spawns.append({"name": name, "preset": preset, "cwd": str(cwd),
                                 "packet_files": list(packet_files or []), "instruction": instruction,
                                 "labels": dict(labels or {}), "model": model,
                                 "child_id": cid, "session_id": sid})
        return cid, sid, ("read-list" if spec.get("kind") == "claude" else "prefill")

    def get_preset(self, name):
        if name not in self.presets:
            raise Fail(f"preset {name!r}: not found")
        return {"version": 1, "spec": self.presets[name]}

    def child_info(self, child_id):
        ch = self._children.get(child_id, {})
        return {"child_id": child_id, "session_id": ch.get("session_id"), "status": "idle",
                "model": "m", "labels": ch.get("labels", {}), "cost_usd": 0.01}

    def export(self, child_id):
        return {"conversation_id": "conv-" + child_id, "turns": [
            {"role": "assistant", "content": [{"type": "text", "text": self.answer(child_id)}],
             "input_tokens": 10, "output_tokens": 5, "served_provider": "p"}]}

    def answer(self, child_id):
        """The final answer configured for a child, keyed by a prefix of the
        name it was spawned under (names carry a run- and seat-specific tail)."""
        name = self._children.get(child_id, {}).get("name", "")
        for prefix in sorted(self.answers):
            if name.startswith(prefix):
                return self.answers[prefix]
        return ""

    def prompt(self, child_id, text):
        self.prompts.append({"child_id": child_id, "text": text})

    def stop(self, child_id):
        self.stops.append(child_id)

    def result(self, obj):
        self.results.append(obj)


@pytest.fixture
def fake_rafiki():
    return FakeRafiki()


@pytest.fixture
def tmp_home(tmp_path):
    return runs.Home(tmp_path)
