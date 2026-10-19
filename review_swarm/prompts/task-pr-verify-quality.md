Verify this QUALITY finding. It does not claim a defect, so the severity rubric does not apply and the finding is never refuted for not being a bug. Judge two things: is it REAL, and is the change it proposes WORTH making.

Why the lens that raised it exists (what the project wants found): {lens_prompt}

Title: {title}
Location: {file}:{line}
Claim: {claim}
Evidence given: {evidence}
Proposed change: {repro}

REAL: refute by default. Uphold only if the code as written shows what the claim says, checked at every place it names (every copy of a duplicate, every unused method, every line of churn), quoting the deciding lines. A claim you could not confirm in the code is refuted.

WORTH IT: judge against the lens's purpose and across every place the finding names, not one copy in isolation. Uphold when the proposed change removes code, a copy or a moving part with nothing a stated requirement or a verifiable constraint (a framework's behaviour, an existing caller, a documented failure) needs. Refute when the complexity is required, when the change would alter behaviour, or when the saving is too small to justify the churn.

refuted is false only when the finding is both real and worth it. severity_adjustment is always "none".

Answer with ONE final message ending in a fenced ```json block:

```json
{"refuted": true, "reasoning": "what the code actually does, with the deciding lines quoted, and why the change is or is not worth making", "severity_adjustment": "none", "process_notes": ["..."]}
```
