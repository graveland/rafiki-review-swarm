---
name: fixtures-through-real-path
category: defect
applies_to: [design, pr, delta, audit]
triggers: always
tests: true
---

Check the tests, not just the code: does each new test build its objects through the production builders and then change the inputs, or does it hand-set the exact field the code reads? A fixture that sets what the code reads encodes the code's assumption and will pass against the bug. Name the test and the production path it bypasses.
