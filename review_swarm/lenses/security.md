---
name: security
category: defect
applies_to: [design, pr, delta, audit]
triggers: always
---

Hunt security defects: a query, command or config built by string interpolation from anything external, path traversal or symlink escape when reading or walking a filesystem, an endpoint or service reachable without the authentication its siblings require, a secret or token compared in a way that leaks through timing, logs or error text, secrets (credentials, keys, connection strings) echoed in logs, errors or debug output, untrusted input driving subprocess arguments, file paths or resource sizes. Name the gate that's missing vs the path that has it.
