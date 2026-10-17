MODE: {mode}
SCOPE: {scope}

LENS - {lens_name}: {lens_prompt}

One lens over the change, inside your scope and nothing else. Other finders cover the other lenses and other parts of the change, so stay in yours: a padded list costs a verifier spawn and dilutes the findings that matter.

DESIGN MODE VARIANT (read this in place of the two sentences above when MODE is design): there is no change and no diff. The intent is a design doc, the code under review is the repository's current main, and SCOPE names the paths you may read there (where it says the whole change, read the whole of what the doc proposes). Read the design's claims and proposed mechanisms against that code, that is the code the doc names: the state, the transitions and the mechanisms its claims rest on, and what the current code does where the two meet. A finding is what the doc gets wrong about code that exists today, or what implementing it as written would break, and it still cites the file and line in the current code that decides it. A question the doc leaves unanswered is not a finding and not yours: the panel reports those.

A finding needs a reachable path: file and line, what concretely goes wrong, and the trigger that reaches it. Evidence says what you checked outside your scope (a caller, a callee, a schema) and quotes the line that decides it. Every finding is checked afterwards by a verifier whose default is to refute it, so a precise empty list beats a speculative finding.

severity: S1 for a defect that corrupts stored data, loses work or is a security hole on a normal path; S2 for a wrong result or a crash behind a plausible trigger; S3 for a real but minor mechanism, an operator-misuse footgun or a self-correcting defect.

Answer with ONE final message ending in a fenced ```json block of exactly this shape (an empty findings list is a valid answer):

```json
{"findings": [{"title": "short statement of the problem",
  "category": "defect|quality",
  "file": "repo-relative path", "line": 123,
  "severity": "S1|S2|S3",
  "claim": "what concretely goes wrong",
  "evidence": "the reachable path: functions, states, what you checked outside your scope",
  "repro": "the trigger sequence"}],
 "process_notes": ["..."]}
```
