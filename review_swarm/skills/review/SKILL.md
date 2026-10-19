---
name: review
description: Run a review-swarm review of a design doc, a squashed PR, the fixes pushed after a review, or a whole-repo audit of a project's checkout. Use when asked to audit or sweep a repository rather than one change, when a design doc has landed or before implementing a destructive or safety-bypassing path, when a PR is squashed and about to be marked ready or handed to a human reviewer, or when fixes have been pushed after a review and need re-judging.
---

# review

Run these from your review home with REVIEW_SWARM_DATA set (or pass --data): the home holds projects/ and lenses/, the data dir holds runs/, and review-swarm is installed in the home's environment (uv run review-swarm ...).

Terms: a fundi seat is a child on rafiki's native agent runtime (the other kind is
claude); the workspace is your own working directory, never the target repo.

## Modes and when

A run pins a change by commit, freezes the trees it reviews, and puts seats from
the `review-pr:` preset group on it. Three change modes, one per moment, plus a
whole-repo audit:

- `design`: a design doc has landed, or you are about to implement a path that
  destroys data or bypasses a safety check. Adds the questions the design must
  answer before implementation.
- `pr`: a PR is squashed and about to be marked ready or handed to a human
  reviewer. Defects in the change against the intent it states.
- `delta`: fixes were pushed after a review. Re-judges each prior finding as
  fixed, not-fixed or incomplete.
- audit: the whole checkout at one commit, not a change. Use it when asked to
  audit or sweep a repository. Its seats and lenses come from the project's
  `audit.json` (see "Whole-repo audit" below).

```
review-swarm pr new <project> --mode pr --pr <N>
review-swarm pr new <project> --mode design --doc <path to the design>
review-swarm pr new <project> --mode delta --pr <N> --since <run>
review-swarm run new <project> --ref <commit>
```

Each prints the run id last.

## Running

Every stage is a script child of the registered review-swarm source. Start it
with `pymodule_start`, so the stage and every seat it spawns are your children:
they sit under you in the cockpit, and the stage's settle reaches you as an
event. Pass the args `review-swarm launch` would, since the stage fails on argparse
without `--data`, and the child grant, since the daemon's default live-children
cap refuses the rest of each batch of `--parallel` workers:

```
pymodule_start(repo="review-swarm", script="review",
               args=["--data", "<data dir>", "<stage>", "<run>"],
               cwd="<data dir>", max_children=24,
               labels={"review/run": "<run>", "review/stage": "<stage>"}, max_cost=<cap>)
```

For a change run, `<stage>` is `gather`, `review` or `verify-pr`, run in that
order. For an audit run it is `map`, then `find`, then `verify`, with the operator
between `map` and `find` (see "Running an audit" below). Never start a
stage with `review-swarm launch` from Bash: it spawns the stage with no parent, so
its seats are orphaned in the cockpit and no settle reaches you, which leaves
only a shell loop on `rafiki logs -f` to notice the stage ended. `launch` is for
a human at a terminal.

gather builds the packet and prices it, then stops at `awaiting-confirm` when the
packet crosses the project's `confirm_over_tokens` or a seat whose context window
is known cannot hold it. On `awaiting-confirm`, show the operator the stage's own
estimate before asking for a yes: fundi dollars (an upper bound summed from each
fundi seat's max_cost, `UNKNOWN` when a seat sets none, which is not $0) and
claude-kind volume (seat count times packet tokens) separately. Then wait for a yes
and confirm the run:

```
review-swarm pr confirm <run>
```

Say what the estimate can and cannot tell the operator, because the design's one
hard limit cannot be checked today. No shipped preset carries a context window and
the SDK has no model catalog, so `pr confirm` prints `fit UNCHECKED` and names the
seats whose window nothing reported: that line is the absence of a check, not a
claim that the packet fits. A seat whose reported window cannot hold the packet is
still refused (`blocking`), and a seat that cannot take an unchecked packet fails
at run time and lands as parse-error friction. Never treat the estimate, or a
silent run-time failure, as a fit claim. `pr status <run>` prints the same lines.

The same shape with `pr abort <run>` abandons the run instead, and with `pr status
<run>` reads one without touching it. After the confirm, start `review`; `verify-pr`
follows a finished review.

gather lands at `confirmed` directly when the packet is under the threshold and no
seat with a reported context window is over it, the ordinary path while no window
is reported anywhere. There is nothing to confirm then: start `review` on it
straight away.

Which seat plays gather, finder, panel and verifier comes from the project's
`pr.json`. Models and kinds are seat policy: retune them by calibration and sync
the group, never hard-coding a model here or passing one as a stage flag.

How wide a review fans out is also `pr.json`, and the defaults are lean: a
finder per triggered lens over the whole change (`finders: "triggered"`), with
the always-on lenses left to the panel's checklist; no file-group split
(`split_groups: false`); and "checked: nothing" answers reported, not verified
(`verify_claims: false`). Widen one (`"all"`, `true`, `true`) only when a
baseline shows the lean run missing what that widening would catch, and record
the comparison with `compare-pr`.

```
review-swarm presets sync
```

While a stage runs, wait for its settle event, then read the output with
`rafiki logs <cid> --all`. Never poll: a poll loop spends money, races the
stage's own claim on the run, and can only ever see a status the stage has not
written yet.

## Triage

When the run is `done`, `report` renders it: confirmed findings by severity,
contested with both sides, incomplete (a vote missing or unparseable) with
whatever votes exist, and the Triage table. Read every confirmed and contested
finding against the code, not against the report, and give each one a row:
agree, disagree or need-info, with the deciding lines. Never present a finding
you have not checked yourself, and never drop one you believe is wrong: a
disagreement with a confirmed finding is a row carrying the lines that say why.

Two things the run already did, so do not redo them. Verifiers read every claim
independently, so a confirmed finding stands on the code rather than on who
raised it. A `quality` finding is judged against the purpose of the lens that
raised it (real, and worth the change), not against the defect severity rubric,
so a confirmed quality finding means the cut was checked, not that it is a bug. And every always-on lens is answered with findings or with the literal
"checked: nothing, because <reason>", where the reason names the code that makes
the concern impossible; a reason that is a judgement rather than code is a
finding, and the verifier seats refute such answers by default. This lists what a
run could not report or check, and `--project <p>` rolls every PR run of one
project up instead:

```
review-swarm friction <run>
```

## Report

```
review-swarm report <run> --out <reports dir>/YYYY-MM-DD-<repo>-<pr>-review-<n>.md
```

Design runs take `-design-review-<n>` in place of `-review-<n>`, and `<n>` counts
up so a second review of one target never overwrites the first. Commit the report
to the workspace, never to the target repo, and fill Disposition as fixes land. A
run is never edited after it finishes: the session's triage and disposition live
in the report.

## Recording a miss

When a human finds a bug a review missed, or a lens fails to catch what it claims
to catch:

1. add a gold line for the bug to the project's `gold/`, at the commit where the
   bug existed, with `graded_by` naming who graded it;
2. add or edit a lens whose `catches:` names that gold line's slug, since the
   build refuses a slug no gold line carries;
3. compile the lenses:

   ```
   review-swarm lenses build <project>
   ```

4. pin a baseline run at that commit and compare the runs, which needs no daemon
   and prints sorted JSON rows carrying each run's recall and precision, to show
   the bug is caught now:

   ```
   review-swarm compare-pr <older run> <new run>
   ```

   A lens that changes nothing measurable is not a fix.

## Lenses

Frontmatter keys: `name`, `category` (`defect`, `quality`, or `drift`),
`applies_to` (any of `design`, `pr`, `delta`, `audit`), `triggers`, `catches`,
`replaces`, `packet`, `context`, `tests`. A lens with no `triggers` is always on. Each project's lens
dir is its `pr.json` `lens_dir` (PR modes) or `audit.json` `lens_dir` (whole-repo
audit), and those repo lenses overlay the generic `lenses/` layer.

`drift` is audit-only: a doc claim (CLAUDE.md, an architecture doc) that the code
contradicts, does not enforce, or says a test pins when none does. `packet` is
audit-only too: a list of globs that makes the lens one cross-cutting cell over
those files (flag spelling across every command's generated docs, say) instead of
a cell per component, where it would only see a fragment.
`context` (audit) is a list of globs every cell of that lens also reads, so a doc
only some lenses need stays out of the rest. `tests: true` keeps a component's
tests in that lens's packet when `audit.json` `test_globs` takes them out of
everyone else's.

`paths` globs match with `fnmatchcase`, where `*` already crosses `/` and `**`
means the same thing as `*`, so write `pkg/resource-handler/*` and drop the `**`
that is not special here. A `diff_matches` pattern is a `re.search` over the whole
diff unless it carries an inline `(?m)`. The `diff_matches` regexes are compiled
at build time, so a bad one fails `lenses build` rather than the run that first
matches it; the globs are matched as written, at select time.

A target is replaced once and once only: two repo lenses that `replaces` the same
target fail `lenses build`, which names both files. Both would otherwise ship and
one would win in a run, so replace a target from one lens and drop the duplicate
rather than relying on which file sorts first.

## Running an audit

1. `map <run>`. The mapper splits the repo into components and seams and picks
   the lenses each one runs.
2. Stop. The map is the operator's hand step: reviewing it is reviewing what the
   audit will cover. Tell the operator the run id and the candidate maps under
   `<data dir>/runs/<project>/<run>/map/`, and wait. They promote one
   (`review-swarm promote <run> map NN/mapper-N`) and correct
   `map/final.json` by hand. Do not promote or edit the map yourself unless they
   ask you to.
3. `find <run> --arm swarm --arm seam --arm cross --rounds 1`. Pass explicit
   `--arm`s on a large repo: `control` hands the whole repo to one cell and does
   not fit. `find --only cell=...` re-runs single cells.
4. `verify <run> --find 01` (the find attempt to verify).
5. `review-swarm report <run> --out audit.md` from the home (a plain command,
   not a stage), then hand the operator the report.

With the project's `"freeze": false` the target checkout must stay at the run's
commit, clean, until the run finishes; every stage refuses to start once it has
moved. Without it the run freezes a copy and the checkout is free.

## Whole-repo audit

`map`, `find` and `verify` audit a whole repo rather than a change. A project opts
into lens-compiled concerns and its own seats with `audit.json` next to its
`project.json`:

```json
{"lens_dir": "/path/to/your/project-lenses",
 "exclude_lenses": ["transitions"],
 "seats": {"mapper": "review-audit-claude:mapper", "finder": "review-audit-claude:finder",
           "verifier": "review-audit-claude:verifier"}}
```

`lens_seats` maps a lens to its own finder seat (a cheaper one for mechanical
lenses), and `test_globs` names the files that are tests. `run new` compiles the lenses whose `applies_to` has `audit` into the run's own
`concerns.json`, so a run keeps the lenses it started with. Without `audit.json`
the project's hand-written `concerns.json` and the fundi `review:*` seats apply.
A claude seat cannot be pre-filled, so its files arrive as a read list.

The mapper picks the lenses for each component and seam, and `find` runs only
those, so the hand review of `map/final.json` is also the review of what runs
where. A map that leaves a per-component defect lens off every component fails
validation, and `find` writes what the map cut to `skipped-lenses.json` and prints
it. `"honor_map_lenses": false` in `audit.json` runs every lens everywhere
(set it to stay comparable with earlier runs).

A seam names its own boundary files in `paths` (the interface, its call sites,
the tests that pin it), and its cells read only those. A seam without `paths`
reads every file of the components it joins, and a map where that comes to more
than about 60K tokens fails validation.

`verify` merges findings of one category in one file within a few lines into
one issue, verifies it once through its most severe report (showing the verifier
the others), and gives every member the verdict. Every verifier is told the lens's
purpose, so "worth fixing" is judged against what the lens is for. `--only
category=quality` (or `concern=`, `arm=`) re-verifies a subset in a new attempt;
`report <run> --out <path>` renders an audit run from all its verify attempts,
the latest verdict winning, with quality findings judged real but not worth
fixing kept in their own section.

A repo over about 100K tokens goes to the mapper as a per-file line listing plus
its `context_files`, and a map's component paths may be globs. Promote the map
(`promote <run> map NN/mapper-N`), correct `map/final.json` by hand, then run
`find` with explicit `--arm`s: `control` hands the whole repo to one cell and does
not fit a large one.

## Never post to GitHub

Nothing is posted to GitHub without an explicit ask: no comment, no review, no
approval, no marking ready. The report is written to the workspace and read there.
