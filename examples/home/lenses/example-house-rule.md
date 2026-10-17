---
name: example-house-rule
category: quality
applies_to: [pr, audit]
triggers: always
---

Flag functions that mix I/O with decisions: a function that reads a file, calls a service or writes output in the same body that chooses what to do with the result should separate the decision from the effect, so the decision can be tested without the I/O and the effect can change without rewriting the rule.
