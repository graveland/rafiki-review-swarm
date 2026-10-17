# review-swarm: run-state helpers the PR stages share (state.json + friction log)
"""state.json is the stage-to-stage carry of a PR run: each stage reads what
the previous one wrote and appends its own section. friction.jsonl is the
running log of everything that went wrong but did not stop the run, so a
reviewer of a finished run can see where the driver was pushed off its path.
"""

import contextlib
import contextvars
import functools
import json
import os
import tempfile
from pathlib import Path

from .common import Fail, now_iso


def load_state(rdir: Path) -> dict:
    f = rdir / "state.json"
    if not f.exists():
        raise Fail(f"no run state at {f}")
    try:
        return json.loads(f.read_text())
    except Exception as e:
        # A truncated state.json is what a crash mid-write used to leave, so
        # this is a likely failure and the stage needs the run directory named.
        raise Fail(f"load state {rdir}: {e}") from e


def save_state(rdir: Path, state: dict) -> None:
    """Written to a temp file, flushed and fsynced, then renamed into place: a
    reader never sees a half-written state.json, and a crash mid-write keeps
    the previous one. fsync is what makes the second half true: without it the
    rename can reach the disk before the bytes do."""
    try:
        fd, tmp = tempfile.mkstemp(dir=rdir, prefix=".state.json.", suffix=".tmp")
    except OSError as e:
        raise Fail(f"save state {rdir}: {e}") from e
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(state, sort_keys=True, indent=1) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, rdir / "state.json")
    except Exception as e:
        # The temp file is ours and no longer wanted. Cleanup must never mask
        # the failure that brought us here, so it stays best-effort.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise Fail(f"save state {rdir}: {e}") from e


# The stage (and, once it has one, the attempt) whose work a friction entry
# belongs to. A run holds every attempt of every stage, so an untagged entry
# cannot be told apart from an abandoned attempt's when a run is graded.
_SCOPE = contextvars.ContextVar("friction_scope", default=None)


def friction_stage(stage):
    """Decorate a stage entry point: friction appended during the call carries
    `stage`, and the attempt once friction_attempt names it."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*a, **kw):
            token = _SCOPE.set({"stage": stage})
            try:
                return fn(*a, **kw)
            finally:
                _SCOPE.reset(token)
        return inner
    return wrap


def friction_attempt(attempt: str) -> None:
    scope = _SCOPE.get()
    if scope is not None:
        scope["attempt"] = attempt


def append_friction(rdir: Path, entry: dict) -> None:
    """One JSONL record per friction event; `at` is stamped here so every
    entry orders the same way whatever the caller passed, and the current
    stage scope tags it unless the caller already did."""
    rec = {**(_SCOPE.get() or {}), **entry}
    rec["at"] = now_iso()
    try:
        line = json.dumps(rec, sort_keys=True) + "\n"
        with (rdir / "friction.jsonl").open("a") as f:
            f.write(line)
    except Exception as e:
        raise Fail(f"append friction {rdir}: {e}") from e
