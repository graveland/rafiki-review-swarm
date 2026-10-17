---
name: concurrency
category: defect
applies_to: [design, pr, delta, audit]
triggers: always
---

Hunt concurrency defects: a lock or mutex held across work it does not need to cover (a network call, a blocking read, a long computation), a critical section split across separately committed transactions so another writer can interleave, data races on shared state, lock-order inversions, TOCTOU between a check and the write it guards, a background task racing the foreground path, missed wakeups in a debounce/notify loop. For each suspect, name BOTH paths and the interleaving that breaks.
