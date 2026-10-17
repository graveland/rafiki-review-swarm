---
name: state-data-loss
category: defect
applies_to: [design, pr, delta, audit]
triggers: always
---

Hunt state/data-loss defects: writes that can be lost or clobbered (a delete-then-insert with a failure in between, a reconcile or retry that drops records it should carry forward, a cache or index that diverges from its source of truth), ordering bugs where something is acknowledged before it is durable, a schema or data migration that can lose data, records left half-written or behind by an interrupted run. Trace the write to its durable landing and walk the failure branch.
