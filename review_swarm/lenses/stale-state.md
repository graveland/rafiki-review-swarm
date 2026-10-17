---
name: stale-state
category: defect
applies_to: [design, pr, delta, audit]
triggers: always
---

Find every place the change keeps an object alive, or reads an object, after the controller or writer that keeps it current has stopped updating it. For each reader of that object's fields, decide whether stale data is acceptable; a decision (delete, retain, retry, route) made from a field nothing updates any more is a defect.
