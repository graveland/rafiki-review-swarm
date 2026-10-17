# Lenses

A lens is a markdown file: YAML frontmatter fenced by `---` lines, then the
prompt. The body is the text a reviewer seat receives, so write it as an
instruction, not as documentation.

```markdown
---
name: example-house-rule
category: quality
applies_to: [pr, audit]
triggers: always
---

Flag functions that mix I/O with decisions: a function that reads a file, calls
a service or writes output in the same body that chooses what to do with the
result should separate the decision from the effect, so the decision can be
tested without the I/O and the effect can change without rewriting the rule.
```

## Frontmatter

The only accepted keys are `name`, `category`, `applies_to`, `triggers`,
`catches`, `replaces`, `packet`, `context` and `tests`. An unknown key fails the
build.

- `name` (required): a non-empty string, unique within a layer.
- `category` (required): `defect`, `quality` or `drift`. A `drift` lens applies
  only to audit.
- `applies_to` (required): a list drawn from `design`, `pr`, `delta` and
  `audit`.
- `triggers` (optional): `always` (the default) or a mapping with `paths` and
  `diff_matches`. A lens with no triggers is always on.
  - `paths` is a list of globs matched with `fnmatchcase`; `*` already crosses
    `/`, and `**` means the same as `*`.
  - `diff_matches` is a list of regular expressions searched over the whole
    diff. They are compiled at build time, so a bad pattern fails the build
    rather than the run that first matches it.
- `catches` (optional): a list of gold slugs this lens claims to catch. The
  build refuses a slug no gold line carries.
- `replaces` (optional): the name of the lens this one replaces.
- `packet` (optional, audit only): a non-empty list of globs that makes the lens
  one cross-cutting cell over those files instead of a cell per component.
- `context` (optional, audit only): a non-empty list of globs every cell of this
  lens also reads.
- `tests` (optional, audit only): a boolean. `true` keeps a component's tests in
  this lens's packet when `test_globs` takes them out of everyone else's.

The body must be non-empty after trimming.

## Layers

Lenses load in three layers, in this order:

1. the lenses shipped with the engine;
2. `<home>/lenses/`;
3. the project's `lens_dir`, named by `pr.json` for the PR modes or by
   `audit.json` for the whole-repo audit.

A later layer does not silently win. It may shadow a lens from an earlier layer
only by declaring `replaces`.

## `replaces`

A lens that replaces another names its target:

```yaml
replaces: example-house-rule
```

Three failures stop the build:

- an unknown target: `replaces` names no lens on disk;
- a target replaced twice: two lenses name the same target, and the build names
  both files;
- a name already taken without `replaces`: a lens reuses an existing name but
  does not declare the replacement.

A replaced name leaves every layer before the layers are merged, so the
replacement lands whichever file would otherwise sort first. A `replaces`
target must exist in the layers below, so replacing a name that an earlier
layer already replaced fails as `replaces unknown lens`. A lens may replace
itself by name: the same name plus `replaces: <same>`.

## Build

```
review-swarm --home . --data <data dir> lenses build <project>
```

The command reads the project's `project.json` for its target, then
`lens_dir` (from `pr.json` for PR runs and from `audit.json` for whole-repo
audit runs), validates every lens in every layer, and writes:

- `<data>/builds/<project>/concerns.json`: the compiled concerns, for inspection;
  a run compiles its own `<run>/project/concerns.json` from the same layers at `run new`;
- `<data>/builds/<project>/lenses.manifest.json`: each lens's name, source,
  sha256, `catches`, `applies_to` and `triggers`.

The build fails, naming the file, on bad frontmatter, a bad `diff_matches`
pattern, an unknown gold slug, a duplicate name in one layer, or a bad
`replaces`. Run it after editing a lens: it is the gate that keeps a bad file
from reaching a run that has already paid for a packet.
