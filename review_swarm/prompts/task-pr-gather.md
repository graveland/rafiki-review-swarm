INTENT OF THE CHANGE:
{intent}

CHANGED FILES:
{changed_files}

Pick the review context within roughly {budget_tokens} tokens of file ranges.

For the symbols this change touches (added, modified, or newly called), find the definition and the callers and callees that decide whether the change is safe. Use grep and read the files, and cite what the file says now.

DESIGN MODE VARIANT (read this in place of the sentence above when the mode is design): there is no change and no diff (your file list has no packet/diff.patch, and the intent is a design doc), so the context to pick is the code the doc names: the symbols, files and mechanisms its claims rest on, rather than the symbols a change touches. Find the definitions and the callers and callees those claims depend on, and cite the current code that would have to change or that already says something else. Everything else here is unchanged: the same budget, the same ranking, the same file_groups rule, the same JSON.

Rank the ranges: first what decides correctness (the definition itself, the guard or lock a caller holds, the branch a callee takes), then what shows how the changed code is reached. Include a file the change touches only when the changed range alone is not enough to show what it does.

file_groups only when the change spans clearly separate components: one group per component, naming its paths. A path is repo-relative. Keep ranges as small as the answer allows, and make "why" say what that range decides. process_notes carries what the next seat has to know about the checkout: a symbol that moved or a file the change renames.

Answer with ONE final message ending in a fenced ```json block of exactly this shape:

```json
{"context": [{"path": "repo-relative", "start": 1, "end": 40, "why": "..."}],
 "file_groups": [{"name": "...", "paths": ["..."]}],
 "process_notes": ["..."]}
```
