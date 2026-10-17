# review-swarm

review-swarm is lens-driven, multi-agent code review on a rafiki daemon: a home repository names a target, the lenses and the seats, the engine freezes a commit into a run, and fresh rafiki children find, panel and verify findings while every stage writes only into a separate data directory.

## Requirements

- a running rafiki daemon;
- a rafiki profile the daemon accepts, able to reach the models the shipped presets name (OpenRouter ids by default; see Presets to change them);
- a live rafiki executor that can run script children;
- `uv`, `git`, and `gh` (the PR modes read PR metadata through `gh`).

## The engine, the home and the data dir

| Role | What it holds | Written by a stage? |
| --- | --- | --- |
| engine | this repository: the `review_swarm` package and the `review-swarm` console script | no |
| home | your own git repository: `projects/<name>/`, personal `lenses/`, `scripts/review.py`, `pyproject.toml` | no |
| data dir | an absolute path outside any checkout: `runs/`, `builds/`, `.daemon/` | yes |

The engine is installed code. The home is configuration and content: it is read, never written. The data dir is the only place a run writes: frozen trees, packets, attempts, reports and sandbox state. Nothing a stage runs writes into the home.

Resolution:

- home: `--home`, else `REVIEW_SWARM_HOME`, else the current directory if it contains `projects/`, else an error;
- data dir: `--data`, else `REVIEW_SWARM_DATA`, else an error. It must be an absolute path, and the executor must be able to write it too.

## Install

Copy `examples/home` as the start of your own home repository:

```
cp -R examples/home ~/code/my-review-home
cd ~/code/my-review-home
git init
```

Set the engine pin in `pyproject.toml`. The example pins a tag:

```
dependencies = ["review-swarm @ git+https://github.com/graveland/rafiki-review-swarm.git@v0.1.0"]
```

Then install it into the home's environment:

```
uv sync
```

The console script lives in the home's virtual environment, so run it as `uv run review-swarm` from the home (or activate `.venv` first). Set the two variables the commands below read:

```
export REVIEW_SWARM_HOME=$PWD
export REVIEW_SWARM_DATA=/absolute/path/to/review-data
mkdir -p "$REVIEW_SWARM_DATA"
```

`REVIEW_SWARM_HOME` names this home and `REVIEW_SWARM_DATA` names the data dir, so the remaining commands work without repeating `--home` or `--data`. Both are still optional flags.

Build the example lenses:

```
uv run review-swarm lenses build example
```

Register the home repository, not the engine, as a rafiki python git source named `review-swarm` at a ref:

```
rafiki python repo add review-swarm <home repo url> --ref <ref>
```

`launch` addresses that source by name (`--repo review-swarm`) and runs its `review` script, which is `scripts/review.py`.

## Presets

```
uv run review-swarm presets sync
```

This pushes the `review:` and `review-pr:` seats to the daemon. The presets ship with the engine, and the model ids in them are examples. They are read from the engine's package directory, so changing them means installing the engine from your own checkout: point the home's `pyproject.toml` at it (`review-swarm @ file:///path/to/engine`), edit `review_swarm/presets/*.json` there, `uv sync`, then `presets sync` again. The daemon's copy and the one a script child runs must agree, which `check_seats` enforces before a stage spends anything.

## First review

A project needs a real `target` (a git checkout) and a `pr.json`. See [docs/home-and-projects.md](docs/home-and-projects.md) for the project files, and [skills/review/SKILL.md](skills/review/SKILL.md) for the review workflow.

Pin the PR into a run. The last line it prints is the run id:

```
uv run review-swarm pr new example --mode pr --pr <number>
```

Gather the packet. It stops at `awaiting-confirm` when the estimate is over the project's threshold:

```
uv run review-swarm launch --repo review-swarm -- gather <run-id>
uv run review-swarm pr confirm <run-id>
```

Confirm only when gather stopped at `awaiting-confirm`. Then review, verify and write the report:

```
uv run review-swarm launch --repo review-swarm -- review <run-id>
uv run review-swarm launch --repo review-swarm -- verify-pr <run-id>
uv run review-swarm report <run-id> --out <path>
```

`launch` is for a human at a terminal: it starts the stage with no parent. An agent driving the review starts the stage with `pymodule_start` instead, so the stage and its seats stay in its tree and a settle reaches it (see [skills/review/SKILL.md](skills/review/SKILL.md)).

## Sandbox

The sandbox daemon builds rafiki from a checkout, so set `REVIEW_RAFIKI_SRC` to that checkout before `uv run review-swarm --target sandbox sandbox init`. It also needs a Postgres it may create a database in (`REVIEW_PG`, default `postgres://postgres@127.0.0.1:5432`) and `~/.config/rafiki/service.env` holding `OPENROUTER_API_KEY`, which it copies into the sandbox daemon's environment. Real reviews use the live daemon and need none of this.
