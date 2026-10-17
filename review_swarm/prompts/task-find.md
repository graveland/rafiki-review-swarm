SCOPE: {scope}
{scope_notes}

CONCERN - {concern_name}: {concern_prompt}

{category_rules}
{suppress}
Answer with ONE final message ending in a fenced ```json block of exactly this shape (an empty "findings" list is a valid answer):

```json
{"findings": [{"title": "short statement of the problem",
  "category": "{category}",
  "file": "repo-relative path", "line": 123,
  "doc": "drift only: the repo-relative path of the doc whose claim is stale",
  "severity": "S1|S2|S3 (defects only)",
  "claim": "what concretely goes wrong",
  "evidence": "the reachable path: functions, states, what you checked outside your scope",
  "repro": "defects: the trigger sequence",
  "effort": "quality only: S|M|L"}]}
```
