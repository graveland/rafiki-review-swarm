Verify this DEFECT finding.

Why this lens exists (what the project wants found): {concern_prompt}
{also}
Title: {title}
Location: {file}:{line}
Claimed severity: {severity}
Claim: {claim}
Evidence given: {evidence}
Repro given: {repro}

DEFAULT refuted=true: uphold only if the code as written proves the claim, via a trigger that is actually reachable. {lens}
If you UPHOLD, separately judge SEVERITY against the rubric below: a real-but-minor mechanism, an operator-misuse footgun, or a self-correcting defect is at most S3; set severity_adjustment to "downgrade" when the impact is below the claimed tier, "upgrade" when above.

{severity_rubric}

Answer with ONE final message ending in a fenced ```json block:

```json
{"refuted": true, "reasoning": "what the code actually does, with the deciding lines quoted", "severity_adjustment": "none|upgrade|downgrade"}
```
