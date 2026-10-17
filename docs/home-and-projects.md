# The home and the data dir

A home is your own git repository. It holds the projects you review, the lenses
you add on top of the shipped ones, and a small shim the daemon runs. It is
configuration and content: nothing a stage runs writes into it.

A data dir is an absolute path outside every checkout. It holds every run, every
compiled lens build and the optional sandbox daemon. The executor must be able to
write it as well as the process that starts a stage.

## Home layout

```
<home>/
  pyproject.toml
  scripts/review.py
  projects/<name>/
    project.json
    pr.json
    severity.md
    evidence.md
    tools.md                # optional: tools the machine offers seats
    gold/<ref>.jsonl
    audit.json              # optional: whole-repo audit configuration
  lenses/                   # optional: your personal lens layer
    <lens>.md
```

### `projects/<name>/project.json`

```json
{
  "name": "example",
  "target": "~/code/example",
  "include": ["src/**", "README.md"],
  "must_cover": ["src/**"],
  "context_files": ["README.md"]
}
```

The code reads these keys:

- `name`: the project identity and the name of its run directory. Required.
- `target`: the checkout to review. It is expanded and resolved. Required.
  When the target carries `.review/project.json`, that directory is the project's
  config instead of `projects/<name>/` (for a target you do not want to write into,
  keep the config in the home).
- `include`: globs matched against the target's tracked files. Only matching
  files enter a run. Required.
- `must_cover`: globs matched against the included files. At least one included
  file must match one of these globs; otherwise the run is refused. Required.
- `context_files`: extra files each map, find and verify seat reads. Optional.

### `projects/<name>/pr.json`

This file names the seats for the PR pipeline. `gather`, `finder`, `panel` and
`verifiers` are required; a missing one is refused before a seat is spawned.

```json
{
  "confirm_over_tokens": 150000,
  "finder": "review-pr:finder",
  "gather": "review-pr:gather",
  "panel": ["review-pr:panel-1", "review-pr:panel-2"],
  "verifiers": {
    "review-pr:panel-1": "review-pr:verify-1",
    "review-pr:panel-2": "review-pr:verify-2"
  }
}
```

- `gather`: the seat that builds the review packet.
- `finder`: the seat used for finder cells.
- `panel`: the always-on panel seats, in order.
- `verifiers`: one verifier seat per panel seat.
- `confirm_over_tokens`: when the packet's token count is over this number,
  gather stops at `awaiting-confirm`. Optional; no threshold means no stop.
- `lens_dir`: the project's lens layer. Optional; see `docs/lenses.md`.
- `finders`: `"triggered"` (the default) gives a finder cell to each triggered
  lens; `"all"` gives one to every lens.
- `split_groups`: `true` splits finder work by the gather seat's file groups.
  Default `false`.
- `verify_claims`: `true` sends "checked: nothing" answers to verifiers.
  Default `false`.

### `projects/<name>/severity.md` and `evidence.md`

Both are plain markdown with no keys. Their text is inserted into prompts:
`severity.md` into the defect finder and the PR verifier, `evidence.md` into the
defect and drift finders. Write the bar you want enforced, not a generic essay.

### `projects/<name>/tools.md`

Optional plain markdown, inserted into every seat's task (mapper, finders,
panel, gather and verifiers in both pipelines), just before the answer
contract. Use it for what the machine running the review offers beyond a seat's
built-in tools: a code index and the tree name to scope it to, say. A tool a
seat is not told about tends to go unused, particularly a deferred MCP tool it
would have to search for. Keep it to what the seat should do with the tool;
the seat already knows how to call it.

### `projects/<name>/gold/<ref>.jsonl`

A gold set is the graded answer key for one frozen revision. It lives in the
home, never in the target: fixing a bug upstream does not invalidate a snapshot
that already recorded it.

One JSON object per line, one bug per object:

- `bug`: the bug's slug. Required. A lens's `catches` names these slugs, and the
  build refuses a slug no gold line carries.
- `grade`: `real`, `mixed`, `nit` or `wrong`. Recall counts `real` and `mixed`
  as real bugs.
- `description`: what the bug is.
- `locations`: a list of `file:line` strings. A finding matches a gold bug when
  it is in the same file and within 30 lines of one of these.
- `titles`: a list of short titles, used to break a tie when two known bugs sit
  near the same lines.
- `found_by`: who or what found the bug.
- `graded_by`: who graded it. Every entry records this; a first-pass model grade
  stays marked until a human reviews it.
- `commit`: the short commit sha where the bug is present. A comparison counts
  only entries whose `commit` is a prefix of the run's head.

Conventions:

- The commit is the identity; a tag is a convenience. Tag `review-baseline-N` in
  repositories you own. For an upstream repository, record the sha and do not
  push a tag there.
- Gold sets only grow. Append newly graded bugs and re-score old runs against
  the current set rather than rewriting the answer key.
- A gold set describes one snapshot, so grade it against that snapshot even
  after the bugs are fixed.

### `projects/<name>/audit.json`

Optional. It configures the whole-repo audit pipeline:

- `lens_dir`: the project's lens layer for audit.
- `exclude_lenses`: lens names to leave out.
- `seats`: a mapping of `mapper`, `finder` and `verifier` to seat names.
- `lens_seats`: a mapping of lens name to the finder seat for that lens.
- `test_globs`: globs that identify tests.
- `honor_map_lenses`: `false` runs every lens everywhere instead of only the
  lenses the map chose. Default `true`.

Without `audit.json`, the project uses its hand-written `concerns.json` and the
shipped `review:*` seats.

### `lenses/`

Your personal lens layer. The shipped lenses are in the engine, this layer is
read next, and the project's `lens_dir` is read last. See `docs/lenses.md` for
the file format and the `replaces` rules.

### `scripts/review.py`

The shim the daemon runs as a script child. It supplies `--home` from its own
location and forwards every argument it receives, which is how `launch` passes
`--data` only:

```python
import sys
from pathlib import Path

from review_swarm.cli import review_main

sys.exit(review_main(["--home", str(Path(__file__).resolve().parents[1]), *sys.argv[1:]]))
```

### `pyproject.toml`

Pins the engine as a dependency and installs the `review-swarm` console script
into the home's environment. `uv sync` creates that environment. Set the pin to
the tag or commit you want to run.

## Data dir layout

```
<data>/
  runs/<project>/<run-id>/
    run.json
    state.json
    project/
    tree/
    base_tree/
    packet/
    review/
    verify/
    grades.jsonl
  builds/<project>/
    concerns.json
    lenses.manifest.json
  .daemon/
```

- `runs/`: one directory per run. `run.json` records the target, the pinned
  commits and the changed files. `state.json` records the status. `project/` is
  the project snapshot the run was started with. `tree/` and `base_tree/` are
  the frozen commits every seat reads. `packet/` is the gather stage's output.
  `review/` and `verify/` hold numbered attempts. `grades.jsonl` is the grading
  sheet.
- `builds/<project>/`: `lenses build` output. `concerns.json` is what the stages
  read; `lenses.manifest.json` records each lens's source and sha256.
- `.daemon/`: sandbox binaries, logs, isolated XDG directories and
  `daemon.json`, written only when the sandbox target is used.

Nothing a stage runs writes into the home. A run reads the home for configuration
and content, then writes every output under the data dir.
