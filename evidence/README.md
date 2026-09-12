# Evidence bundles

Runtime evidence is written beneath this directory using the run ID as a stable folder name:

```text
evidence/
└── <run_id>/
    ├── events.jsonl
    └── failures/
        └── <step_id>/
            ├── screenshot.png
            ├── page.json
            └── error.json
```

`events.jsonl` is the chronological discovery or replay timeline. Each line carries the run mode,
timestamp, capability and step identifiers, action, selected locator strategy, status, duration,
and error code. Null fields mean that dimension did not apply to that event.

Failure folders contain a screenshot, a bounded and sanitized semantic page snapshot, and a
structured error record. Runtime folders are Git-ignored because they may still contain operational
metadata even after redaction.
