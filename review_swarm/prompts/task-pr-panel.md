MODE: {mode}

ALWAYS-ON CHECKLIST:
{checklist}

{mode_rules}

Review the whole change, in the files and ranges your packet carries, against the intent the packet states. Every lens above is always-on: answer each one, either with findings or with the literal "checked: nothing, because <reason>", where the reason names the code that makes the concern impossible rather than a judgement about how conservative you were. A lens whose reason you cannot ground in code is a finding, not a "nothing".

Findings meet the same bar as any reviewer's: file and line, what concretely goes wrong, a trigger that reaches it, evidence quoting the deciding line, and what you checked outside the packet. severity: S1 for a defect that corrupts stored data, loses work or is a security hole on a normal path; S2 for a wrong result or a crash behind a plausible trigger; S3 for a real but minor mechanism, an operator-misuse footgun or a self-correcting defect.

checklist answers use "findings" when that lens produced at least one finding and "nothing" otherwise, with a one-line reason either way. Every finding names the lens it serves in `lens`, by the name on the checklist above: a "findings" answer is read against the findings you raised for that lens, so a lens you cannot name a finding for is "nothing", not "findings", and a finding whose lens is missing serves no answer. Fill prior only for the prior findings your mode re-judges, and questions only in design mode; leave them out otherwise.

Answer with ONE final message ending in a fenced ```json block of exactly this shape:

```json
{"checklist": [{"lens": "...", "answer": "findings|nothing", "reason": "..."}],
 "findings": [{"lens": "the always-on lens this finding serves",
   "title": "short statement of the problem", "category": "defect|quality",
   "file": "repo-relative path", "line": 123, "severity": "S1|S2|S3",
   "claim": "what concretely goes wrong", "evidence": "the reachable path and what you checked",
   "repro": "the trigger sequence"}],
 "questions": ["..."],
 "prior": [{"id": "...", "status": "fixed|not-fixed|incomplete", "reason": "..."}],
 "process_notes": ["..."]}
```
