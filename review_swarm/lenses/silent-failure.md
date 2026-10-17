---
name: silent-failure
category: defect
applies_to: [design, pr, delta, audit]
triggers: always
---

Hunt SILENT FAILURES: errors swallowed (a discarded return value, a fallible call whose failure is dropped, an error caught and then treated as success), a branch that skips work without any signal, a failed write, persistence or external call the caller never learns about, partial results returned as if complete. For each: name the operation that fails, the path that swallows it, and what the operator or caller never learns.
