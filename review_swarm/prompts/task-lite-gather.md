INTENT OF THE CHANGE:
{intent}

CHANGED FILES:
{changed_files}

Your working directory is the review run. `packet/intent.md` is the intent above, {diff_line}`tree/` is the frozen tree after the change{base_line}. Every path below is relative to it.

Start from the change ({first_read}). Then decide what is related, and at what granularity. Read the thing that matters: the function, the struct, the type, the branch, not the file it lives in. Follow what decides whether the change is correct: the definition of a symbol the change alters, the callers whose behaviour it can break, the guard or lock that decides whether they are reached, the test that would fail. Follow an edge when it could change a verdict; stop when further reading would not.

Rules:
- What a reviewer relies on must be READ, not named. Naming a file gives them a path; reading it gives them the code.
- The code index, if you have one, is a map, never evidence: read the range before you rely on it, and cite what the file says now.
- Prefer a range that holds the whole function over a read that clips it, but do not read the file around it. Everything you read is paid for by every reviewer and dilutes their attention.
- Stay within roughly {budget_tokens} tokens of reading in total, the change included. The smallest reviewer has to hold all of it.
- Never review: no findings, no judgements, no severities, no "the main risk is".
- Your reading is the floor, not the ceiling: reviewers may read more.

Answer with ONE final message, and do not re-narrate your reading: your tool calls already record every path and range, and the reviewers see them. Say only what this conversation cannot show: what you deliberately left out and why, anything you could not resolve, and anything a reviewer would waste time rediscovering (a symbol that moved, a file the change renames). End it with a fenced ```json block of exactly this shape:

```json
{"left_out": [{"what": "...", "why": "..."}],
 "unresolved": ["..."],
 "notes": ["..."]}
```
