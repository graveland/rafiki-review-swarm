---
name: transitions
category: defect
applies_to: [design, pr, delta, audit]
triggers: always
---

Consider transitions, not only states: several spec fields changed in one update (does every field the change reads see the new value, or is one read from an object that will not be updated?), an edit reverted mid-flight, and the return trip (moving back to where the change moved from). For each, name the sequence and the state it leaves behind.
