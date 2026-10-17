Map this repository into review components.

Rules:
- Every file listed under MUST COVER below appears in exactly one component. Tests and docs may be attached to the component they exercise, or left out.
- Use the file paths exactly as given (relative to the repository root). A path may be a glob (`*` crosses `/`); it covers every file in scope it matches.
- Name components with short lowercase identifiers (e.g. `store`, `mcp`).
- `purpose`: one sentence on what the component is responsible for.
- `neighbours`: other component names it calls or is called by.
- `lenses`: which of the concerns listed below are worth running on it; omit a concern only when it genuinely cannot apply (e.g. no shared state means no concurrency).
- `notes`: anything a reviewer of this component should know that its files alone won't tell them (an invariant owned elsewhere, a caller's assumption). Empty string if nothing.
- `seams`: cross-component boundaries worth reviewing on their own (a protocol, a shared schema, a lock held across modules). Empty list is fine.
- A seam's `paths`: the files where the boundary is defined and crossed (the interface, its call sites on each side, the tests that pin it), as paths or globs. Not every file of each component: the seam's reviewer reads only these.
- A seam's `lenses`: the concerns worth running on the boundary itself (what each side assumes about the other), chosen the same way. The lenses you choose are the only ones run on that component or seam.

CONCERNS: {lenses}

MUST COVER:
{must_cover}

{listing}
Answer with ONE final message ending in a fenced ```json block of exactly this shape:

```json
{"components": [{"component": "name", "paths": ["src/a.rs"], "purpose": "...", "neighbours": ["other"], "lenses": ["<a name from CONCERNS>"], "notes": ""}],
 "seams": [{"seam": "name", "components": ["a", "b"], "what": "...", "paths": ["src/a.rs"], "lenses": ["<a name from CONCERNS>"]}]}
```
