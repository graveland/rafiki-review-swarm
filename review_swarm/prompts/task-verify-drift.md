Verify this DRIFT finding: a claim in the project's documentation that the code no longer makes true.

Why this lens exists (what the project wants found): {concern_prompt}
{also}
Title: {title}
Doc: {doc}
The doc's claim: {claim}
Code location: {file}:{line}
Evidence given: {evidence}

Read the doc around the quoted claim and the code it is about. DEFAULT refuted=true: uphold only if one of these holds, and quote the deciding lines from both:
- contradicted: the code does something the claim says it does not, or does not do what the claim says it does;
- unenforced: the claim is a rule ("never", "always", "every path goes through X") and a reachable path in the code breaks it;
- unpinned: the doc says a test pins the rule, and no test does (name where you looked).
A claim that is merely loosely worded, or true by a route the finder missed, is refuted.

Answer with ONE final message ending in a fenced ```json block:

```json
{"refuted": true, "kind": "contradicted|unenforced|unpinned", "reasoning": "what the doc says, what the code does, with the deciding lines quoted"}
```
