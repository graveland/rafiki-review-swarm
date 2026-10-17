A reviewer answered the always-on lens below with "checked: nothing, because {reason}". That answer is itself a claim, and it is REFUTED by default: it holds only when you can show, from the code, that the reason is true. So the question is not whether the reason sounds careful, it is whether the code makes the concern impossible.

LENS - {lens}: {lens_prompt}

Reason given: {reason}

A reason holds when it names the code that makes the concern impossible (the line that raises on the bad input, the guard that forbids the state, the constraint the schema enforces) and you confirm that is the code which actually runs. It does not hold when you can name a reachable path around that code, and it does not hold when it is a judgement rather than a statement about the code (the risk is low, the case is unlikely, the author was careful): a judgement grounds nothing, so that lens was never actually checked.

Set holds=false whenever the reason does not hold, and always with a finding that IS the refutation. The finding takes one of two forms, both in the shape below: a reason defeated by a reachable path yields the defect the reviewer missed (file and line, what concretely goes wrong, the trigger, evidence quoting the deciding line, severity by the usual rubric); a reason that is an ungrounded judgement yields the missing grounding itself, whose claim is that the reason names no code and the lens is therefore unanswered, with file and line at the changed code the lens covers, evidence quoting what that code does and does not contain, repro "n/a" and severity at most S3 because no code defect is shown. holds=false with finding=null is never a legal answer, and holds=true over an ungrounded reason is a failure of this check.

When holds is true, finding is null.

Answer with ONE final message ending in a fenced ```json block, in this shape, shown for the refuting case:

```json
{"holds": false, "reasoning": "what the code does and why the reason is not true of it, with the deciding line quoted, or the judgement the reason rests on instead of code",
 "finding": {"title": "...", "category": "defect|quality", "file": "repo-relative path",
   "line": 123, "severity": "S1|S2|S3", "claim": "...", "evidence": "...", "repro": "..."},
 "process_notes": ["..."]}
```

The same keys with "holds": true and "finding": null are the answer when the reason does hold.
