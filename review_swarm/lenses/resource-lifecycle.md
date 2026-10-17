---
name: resource-lifecycle
category: defect
applies_to: [design, pr, delta, audit]
triggers: always
---

Hunt resource-lifecycle defects: resources leaked on an error path (connections, file handles, locks, child processes, tasks or temp files); unbounded caches or buffers with no eviction; a background loop that dies on error and never restarts; a listener or watcher that silently stops; double-close or use-after-close; retries without backoff or bound. Walk the non-happy path to its end state.
