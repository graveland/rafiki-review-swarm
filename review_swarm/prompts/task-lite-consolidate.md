{preamble}

MODE: {mode}

The findings below are grouped into buckets of nearby lines in one file; judge every finding id separately, even when two in a bucket look alike. `checks` is a mechanical reference check run before you: `file_missing` or `line_out_of_range` means the cited anchor does not exist in tree/; an `unmatched_quotes` entry is a quoted fragment found nowhere in the tree. `delivery` says whether each raising reviewer worked from the shared pack (`fork`) or read the code independently (`independent`).

FINDINGS:
```json
{findings}
```

For every finding id above, give one verdict: "confirmed" (the mechanism is real and a trigger reaches it), "refuted" (code makes it impossible; quote that code), or "uncertain" (the code does not settle it; say what is missing). severity_adjustment is optional: a tier (S1/S2/S3) you would give it instead, with the reason in reasoning.

```json
{"verdicts": [{"id": "...", "verdict": "confirmed|refuted|uncertain",
   "reasoning": "the deciding lines and what they show", "severity_adjustment": "S1|S2|S3 or omitted"}],
 "process_notes": ["..."]}
```
