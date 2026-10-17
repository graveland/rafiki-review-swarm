# review-swarm: the rafiki client surface the stages use, over the rafiki-py SDK
"""Answers are plain dicts in the shapes the stage code reads: a child is
{child_id, session_id, status, model, labels, cost_usd}; a transcript is
{"turns": [{role, content: [block, ...], input_tokens, output_tokens,
cache_read_tokens, served_provider}]} with content decoded from the wire's
verbatim JSON."""

import json
import re

from rafiki import Client, ConnectError

from .common import AtCap, Fail
from .presets import seat_kind

_RANGE = re.compile(r"^(.*):(\d+)-(\d+)$")

# What the read list's `path:a-b` entry means. A fundi seat gets the range as a
# read entry the daemon understands; a seat that reads the listing itself needs
# to be told, or it may look for a file named after the range.
RANGE_NOTE = "A path ending in `:a-b` means lines a through b of that file, read in the same order."


def prefill_read(line):
    """"path" or "path:a-b" (the --prefill-files listing syntax) -> PrefillRead."""
    m = _RANGE.match(line)
    if m:
        return {"path": m.group(1), "start": int(m.group(2)), "end": int(m.group(3))}
    return {"path": line}


def packet_prompt(files, instruction):
    """The prompt a seat that cannot be pre-filled receives: the packet as a
    read list, what the `path:a-b` suffix means, then the seat's instruction. The
    file order is the caller's and must not be reshuffled: seats share the
    prefix, so it has to be identical byte for byte across them."""
    listing = "\n".join(f"- {f}" for f in files)
    return ("Read these files first, in this order, before anything else; they are the review packet:\n"
            + listing + "\n" + RANGE_NOTE + "\n\n" + instruction)


def final_answer(conv):
    """The last assistant turn's text when the conversation has settled on an
    answer (last turn is assistant with no tool_use), else None."""
    turns = (conv or {}).get("turns") or []
    if not turns or turns[-1].get("role") != "assistant":
        return None
    blocks = turns[-1].get("content") or []
    if any(b.get("type") == "tool_use" for b in blocks):
        return None
    return "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


class Rafiki:
    def __init__(self, client: Client):
        self.c = client

    @classmethod
    def inside(cls):
        return cls(Client.inside())

    @classmethod
    def from_profile(cls, profile=None):
        return cls(Client.from_profile(profile))

    def spawn(self, name, *, cwd, prefill_lines, labels, preset=None, model=None):
        """Spawn a fundi child with a pre-fill; returns (child_id, conversation_id)."""
        if not preset and not model:
            raise Fail("spawn needs a preset or a model")
        try:
            cid = self.c.spawn(
                name=name, kind="fundi", cwd=str(cwd), preset=preset, model=model, labels=labels,
                prefill=[prefill_read(l) for l in prefill_lines] if prefill_lines else None,
            )
            return cid, self.c.get(cid).session_id
        except ConnectError as e:
            # The live-children cap is the one transient refusal: the daemon
            # tags it at_capacity, and a slot frees when a sibling settles.
            if e.reason == "at_capacity":
                raise AtCap(f"spawn {name}: {e}") from e
            raise Fail(f"spawn {name}: {e}") from e

    def spawn_seat(self, name, *, preset, cwd, packet_files, instruction, labels, model=None):
        """Spawn a review seat and deliver the packet the way its kind can take
        it. A fundi seat gets the files pre-filled; a claude-kind seat cannot be
        pre-filled, so it gets the same files as a read list at the head of its
        prompt; any other kind is refused as a mistyped seat. The instruction
        rides along with the spawn rather than a later send, so a child is never
        left idle between the two. Returns (child_id, session_id, delivery)."""
        kind = seat_kind(self, preset)
        if kind == "claude":
            # claude-kind children cannot be pre-filled: the same packet goes in
            # as a read list, so the seat's instruction stays last.
            spawn_kind, prefill, prompt, delivery = (
                "claude", None, packet_prompt(packet_files, instruction), "read-list")
        elif kind == "fundi":
            # Only a fundi seat can be pre-filled, and the caller's file order is
            # kept exactly: the bytes must match across seats.
            spawn_kind, prefill, prompt, delivery = (
                "fundi", [prefill_read(f) for f in packet_files], instruction, "prefill")
        else:
            # The preset decides the kind; spawning anything else as a fundi seat
            # would silently override seat policy.
            raise Fail(f"preset {preset}: kind {kind!r} is not a review seat kind")
        try:
            cid = self.c.spawn(name=name, kind=spawn_kind, cwd=str(cwd), preset=preset, model=model,
                               labels=labels, prompt=prompt, prefill=prefill)
            return cid, self.c.get(cid).session_id, delivery
        except ConnectError as e:
            # Same discrimination as spawn: at_capacity is the live-children cap.
            if e.reason == "at_capacity":
                raise AtCap(f"spawn {name}: {e}") from e
            raise Fail(f"spawn {name}: {e}") from e

    def spawn_script(self, name, *, cwd, repo, script, args, labels, max_cost=None, max_children=None):
        try:
            return self.c.spawn(name=name, kind="script", cwd=str(cwd), labels=labels, max_cost=max_cost,
                                max_children=max_children,
                                script={"repo": repo, "script": script, "args": [str(a) for a in args]})
        except ConnectError as e:
            raise Fail(f"spawn {name}: {e}") from e

    def prompt(self, child_id, text):
        try:
            self.c.send(child_id, text)
        except ConnectError as e:
            raise Fail(f"prompt {child_id}: {e}") from e

    def child_info(self, child_id):
        ch = self.c.get(child_id)
        return {"child_id": ch.child_id, "session_id": ch.session_id, "status": ch.status,
                "model": ch.model, "labels": dict(ch.labels), "cost_usd": ch.cost_usd}

    def export(self, child_id):
        tr = self.c.export(child_id)
        return {"conversation_id": tr.conversation_id, "turns": [
            {"role": t.role, "content": json.loads(t.content or b"[]"),
             "input_tokens": t.input_tokens, "output_tokens": t.output_tokens,
             "cache_read_tokens": t.cache_read_tokens,
             "served_provider": t.served_provider}
            for t in tr.turns]}

    def stop(self, child_id, *, report=False):
        """Best-effort stop. report=False (the stages' cleanup) swallows every
        ConnectError: already exited or gone. report=True (the operator's
        `stop` command) swallows only an already-gone child (not_found, or the
        exited child's failed_precondition) and raises Fail for the rest;
        the SDK's stop() can time out on a live script child, so the caller
        reports those ids as possibly still live."""
        try:
            self.c.stop(child_id)
        except ConnectError as e:
            if not report or e.code in ("not_found", "failed_precondition"):
                return
            raise Fail(f"stop {child_id}: {e}") from e

    def result(self, obj):
        self.c.result(obj)
