---
name: algo-bigo
category: quality
applies_to: [design, pr, delta, audit]
triggers: always
---

Find algorithmic/big-O problems on real paths: quadratic loops over the elements the code actually walks (rows, files, objects, symbols), N+1 round-trips where one query or bulk read would do, a full scan where an index or map exists, repeated work that should be cached, unbounded fan-out. Name the hot path, the input that grows, and the current vs achievable complexity, so a reader can judge whether it matters at the scale this code runs at.
