The code for this review follows, inline, in the form `<line number><TAB><line>`. Files are delimited by `=== <path> ===` headers.

{context}

SCOPE: {scope}
{scope_notes}

CONCERN - {concern_name}: {concern_prompt}

{category_rules}
{suppress}
Answer with ONE final message ending in a fenced ```json block of exactly this shape (empty lists are valid answers):

```json
{"findings": [{"title": "short statement of the problem",
  "category": "{category}",
  "file": "repo-relative path", "line": 123,
  "severity": "S1|S2|S3 (defects only)",
  "claim": "what concretely goes wrong",
  "evidence": "the reachable path: functions and states involved",
  "repro": "defects: the trigger sequence",
  "effort": "quality only: S|M|L"}],
 "needs": [{"path": "src/other.rs", "start": 10, "end": 80, "why": "which finding this would confirm or rule out"},
           {"grep": "fn some_function|some_function\\(", "why": "..."}]}
```
