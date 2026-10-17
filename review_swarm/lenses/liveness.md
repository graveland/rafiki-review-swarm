---
name: liveness
category: defect
applies_to: [design, pr, delta, audit]
triggers: always
---

For every wait, gate, retry-until, requeue or precondition the change adds or touches, ask whether the object being acted on feeds what the wait depends on, directly or through an aggregate such as a Healthy phase or a readiness count. If it does, an unhealthy instance of that object can block its own repair or cleanup forever. Also ask what happens when each input the wait reads is degraded (unhealthy, unreadable, slow): does the wait still end, and if not, is that intended? A reason of the form 'this only makes it more conservative' is a liveness concern, not a dismissal: name what can now never happen.
