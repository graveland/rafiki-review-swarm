# review-swarm

Lens-driven, multi-agent code review that runs on a [rafiki](https://github.com/graveland/rafiki)
daemon. A review is many narrow reviewers, each hunting one kind of problem
(a lens) in one slice of code, followed by fresh reviewers whose default is to
refute every finding. It reviews a pull request, a design doc, the fixes made
after a review, or a whole repository.

You do not import this repository. You create a **review home**: a small git
repository that rafiki runs as a Python pymodule source, which depends on this
engine and holds your projects, lenses and gold sets. rafiki checks the home out
on an executor, builds its virtualenv (which installs the engine), and runs the
home's `scripts/review.py` as a script child for each review stage. Each stage
spawns the reviewer seats as its own rafiki children, so a whole review sits in
one tree in rafiki's cockpit, bounded by one budget.

## How the pieces fit

| Piece | What it is | Where it comes from |
| --- | --- | --- |
| engine | this repository: the stages, the shipped lenses and seat presets, the `review-swarm` CLI | upstream; your home pins a tag of it and never edits it |
| home | your git repository: `projects/<name>/`, optional `lenses/`, `scripts/review.py`, `pyproject.toml` pinning the engine | you write it; a stage only reads it |
| data dir | an absolute directory outside every checkout: `runs/`, `builds/` | stages write it, and nothing else does |
| rafiki source | the home, registered with `rafiki python repo add` | rafiki clones it onto each executor and builds its venv there, which installs the pinned engine |
| seats | rafiki presets (`review-pr:*`, `review-pr-claude:*`, `review-audit-claude:*`, ...) naming each reviewer's kind, model and budget | shipped in the engine, put on the daemon by `review-swarm presets sync` |

The engine is installed in two places, for two purposes. rafiki installs it on
the executor, in its own checkout of the home, when it syncs the source: that is
the copy every stage runs, and you never touch it. You install it once more in
your own checkout of the home with `uv sync`, only to get the `review-swarm` CLI:
creating runs, confirming them, rendering reports, syncing seats and launching
stages all happen on your machine, not through rafiki.

A review runs like this:

```
you (or an agent)                rafiki daemon                   executor
review-swarm launch / ----> spawns a script child -------> runs <home>/scripts/review.py
pymodule_start                                                 in <home>/.venv (engine installed)
                                                                  |
                                    reviewer seats  <-------------+ the stage spawns its seats
                                    (claude or fundi children)       as its own children
```

## What you need

- **A rafiki daemon**, and a rafiki profile on your machine that reaches it.
- **An executor on the machine holding the code to review**, started with
  `--launch script` (to run stages: this also turns on git-source sync) and
  `--launch claude` if any seat is claude-kind. The executor needs `git` and
  `uv` on its `PATH` and must be able to fetch your home's URL with the
  credentials in its own environment. It must be able to read your target
  checkouts and write your data dir; its `--root` is a working directory, not a
  sandbox.
- **Models for the seats you use.** fundi seats (the `review-pr:` and `review:`
  groups) name OpenRouter models, so the daemon needs an OpenRouter key. claude
  seats (the `*-claude:` groups) run Claude Code under the executor's Claude
  login (`CLAUDE_CONFIG_DIR` in the executor's environment).
- **`gh`**, authenticated, for PR, design and delta reviews (they read PR
  metadata).

A note on claude seats: rafiki does not narrow a claude-kind preset's tools, MCP
servers or context files, so a claude seat has every tool and MCP server its
Claude login has, including ones that write. Prefer fundi seats for reviewing
code from people you do not trust.

## 1. Create your home

Start from the example:

```
cp -R examples/home ~/code/my-review-home
cd ~/code/my-review-home
git init
```

It holds:

```
pyproject.toml        # pins the engine
scripts/review.py     # the shim rafiki runs for every stage
projects/example/     # one directory per codebase you review
lenses/               # optional: your own lenses, layered over the shipped ones
```

Pin the engine to a tag in `pyproject.toml`, then install it locally for the
CLI (rafiki builds the executor's own copy later, from the same pin):

```
dependencies = ["review-swarm @ git+https://github.com/graveland/rafiki-review-swarm.git@v0.1.2"]
```

```
uv sync
```

`scripts/review.py` passes its own home as `--home` and forwards everything else,
so it needs no editing. Pick a data dir and point the CLI at both:

```
export REVIEW_SWARM_HOME=$PWD
export REVIEW_SWARM_DATA=/absolute/path/to/review-data
mkdir -p "$REVIEW_SWARM_DATA"
```

Commit the home (including `uv.lock`) and push it somewhere the executor can
fetch.

## 2. Register the home with rafiki

```
rafiki python repo add review-swarm https://git.example.com/you/my-review-home.git --ref main
```

`review-swarm` is the source name the CLI uses by default (`launch --repo`
overrides it). Check the executor built the home's venv and can run the shim:

```
rafiki python repo list
```

then, from an agent with rafiki's MCP tools, `pymodule_run(repo="review-swarm",
script="review", args=["--help"])` should print the stage list with `--data`
required.

After every push to the home, refresh the executor's copy:

```
rafiki python repo refresh review-swarm
```

rafiki caches a source by name. If you re-register a name with a different URL,
remove `~/.cache/rafiki/pymodule-repos/<name>` on the executor and refresh, or
it keeps serving the old checkout.

## 3. Sync the seats

```
uv run review-swarm presets sync
```

This puts every seat preset the engine ships onto the daemon. A stage checks
that the daemon's seats match the engine it runs before it spends anything, so
sync again after every engine upgrade. The model in each preset is a starting
point; to change seats, point the home's dependency at your own checkout of the
engine (`review-swarm @ file:///path/to/engine`), edit
`review_swarm/presets/*.json` there, `uv sync`, and sync again. Which seats a
project uses is the project's choice (`pr.json`, `audit.json`).

## 4. Add a project

A project is a directory under `projects/<name>/`:

| File | Purpose |
| --- | --- |
| `project.json` | the target checkout, which files a run covers, context every seat reads, and whether audits review the checkout itself (`"freeze": false`) or a frozen copy |
| `pr.json` | seats for PR, design and delta reviews, and the token count over which `gather` stops for a confirm |
| `audit.json` | seats and the lens layer for a whole-repo audit |
| `severity.md`, `evidence.md` | the severity rubric and evidence bar, inserted into seat prompts |
| `tools.md` | what the machine offers seats beyond their built-in tools (a code index and its tree, say), inserted into every task |
| `gold/<ref>.jsonl` | graded known bugs at a commit, for measuring a configuration's recall |

A project's own lenses live in the directory its `pr.json` or `audit.json` names
as `lens_dir`. Compile them to check they load:

```
uv run review-swarm lenses build <project>
```

Every key and file is described in [docs/home-and-projects.md](docs/home-and-projects.md);
the lens format and layering in [docs/lenses.md](docs/lenses.md).

## 5. Run a review

Every stage runs as a rafiki script child. From a terminal, `launch` starts one
and returns; it passes your data dir for you:

```
uv run review-swarm launch -- <stage> <run-id> [stage flags]
```

An agent driving a review starts each stage with rafiki's `pymodule_start`
instead, so the stage and its seats are the agent's children and its settle
reaches the agent. The [`review` skill](review_swarm/skills/review/SKILL.md)
is written for that agent. It ships inside the package, so a home that lists
`review-swarm` under `[tool.rafiki] skill-sources` (the example home does)
gives `review-swarm:review` to the owner's agents at the pinned version when
the home is registered with `rafiki repo add`.

### A pull request, design doc or delta

```
uv run review-swarm pr new <project> --mode pr --pr <number>     # prints the run id last
uv run review-swarm launch -- gather <run-id>
uv run review-swarm pr confirm <run-id>                          # only if gather stopped at awaiting-confirm
uv run review-swarm launch -- review <run-id>
uv run review-swarm launch -- verify-pr <run-id>
uv run review-swarm report <run-id> --out review.md
```

`--mode design --doc <path>` reviews a design doc against the current code;
`--mode delta --pr <number> --since <run-id>` re-judges an earlier review's
findings against the fixes.

### A whole repository

With `"freeze": false`, put the target checkout at the commit to review (clean),
refresh anything that indexes it, and leave it alone until the run finishes;
every stage refuses to start once it has moved. Without it, the run freezes a
copy and the checkout is free.

```
uv run review-swarm run new <project> --ref <commit>             # prints the run id
uv run review-swarm launch -- map <run-id>
uv run review-swarm promote <run-id> map 01/mapper-1             # then correct <data>/runs/<project>/<run-id>/map/final.json
uv run review-swarm launch -- find <run-id> --arm swarm --arm seam --arm cross --rounds 1
uv run review-swarm launch -- verify <run-id> --find 01
uv run review-swarm report <run-id> --out audit.md
```

The map is the one hand step: it decides the components, the seams between them
and which lenses run where, and reviewing it is reviewing what the audit will
cover. `find --only cell=...` re-runs single cells; `verify --only
category=quality` re-judges a subset in a new attempt, and the report takes the
latest verdict per finding.

## Upgrading the engine

Bump the tag in the home's `pyproject.toml`, then:

```
uv lock --upgrade-package review-swarm && uv sync
git commit -am "pin review-swarm vX.Y.Z" && git push
rafiki python repo refresh review-swarm
uv run review-swarm presets sync
```

## Developing the engine

`uv sync && uv run pytest` in this repository. The sandbox target
(`--target sandbox`) runs stages against a throwaway daemon built from a rafiki
checkout: set `REVIEW_RAFIKI_SRC` to that checkout, `REVIEW_PG` to a Postgres it
may create a database in (default `postgres://postgres@127.0.0.1:5432`), and put
`OPENROUTER_API_KEY` in `~/.config/rafiki/service.env`, then
`uv run review-swarm --target sandbox sandbox init`.
