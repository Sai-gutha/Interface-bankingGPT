# Computer-Use Automation System

A focused interface.ai take-home: an LLM discovers a workflow on a real, legacy-style banking UI; a deterministic compiler turns the successful neutral recording into a typed capability; and replay executes it without an LLM.

## Architecture

```text
goal + policy -> discovery -> SurfaceAdapter -> real UI
                    |              |
              neutral events  Playwright adapter
                    v
             artifact compiler -> capability JSON
                                      |
inputs + policy -> deterministic replay -> typed result + evidence
                         |
                  same-session handoff <-> operator console
```

Reusable layers depend on `SurfaceAdapter`, never Playwright. Browser lifecycle, DOM inspection, and Playwright locator translation remain in `PlaywrightSurfaceAdapter`. Replay is an ordered interpreter with stable locator priority, explicit waits, checkpoints, bounded recovery, policy checks, and typed outputs.

## Installation

Requires Python 3.12.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,llm]'
playwright install chromium
cp .env.example .env
```

## Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | Discovery only | LLM authentication; replay never needs it |
| `COMPUTER_USE_LLM_MODEL` | Discovery only | Structured-output-capable model name |
| `COMPUTER_USE_EVIDENCE_DIR` | No | Evidence root, default `evidence` |

## Run the demo application

```bash
uvicorn demo_app.app:app --host 127.0.0.1 --port 8001
```

Open `http://127.0.0.1:8001`. Member `12345` is normal; `70007` is slow; `80008` is permission denied; `90009` expires the session; any other five-digit ID is not found. All data is synthetic.

### Vercel deployment

The included `api/index.py`, `requirements.txt`, and `vercel.json` deploy only the synthetic FastAPI target:

```bash
vercel link
vercel --prod
```

Discovery, deterministic replay, Playwright, and same-session human takeover remain local processes; a serverless request cannot retain their live browser session.

## Run discovery

Keep the demo server running and supply real LLM credentials:

```bash
OPENAI_API_KEY='your-key' COMPUTER_USE_LLM_MODEL='your-structured-output-model' computer-use discover --goal 'Look up member 12345 and return their savings balance' --start-url http://127.0.0.1:8001 --member-id 12345 --output artifacts/examples/lookup_savings_balance.v1.json
```

The provider returns strict JSON decisions with API storage disabled. Recording retains concise operational reasons and evidence—not chain-of-thought or raw transcripts—and successful discovery compiles the artifact.

## Run deterministic replay

No LLM credentials are required:

```bash
computer-use replay --artifact artifacts/examples/lookup_savings_balance.v1.json --member-id 12345
```

Expected: `SUCCESS` with typed numeric `savings_balance` (redacted in persisted evidence).

## Run the failure demo

```bash
computer-use failure-demo --artifact artifacts/examples/lookup_savings_balance.v1.json --member-id 55555
```

Expected: legitimate `BUSINESS_OUTCOME` with `MEMBER_NOT_FOUND`.

## Run the human handoff demo

```bash
computer-use handoff-demo --artifact artifacts/examples/lookup_savings_balance.v1.json --member-id 12345 --operator-port 8010 --headed
```

Open `http://127.0.0.1:8010/operator`, claim the intervention, operate the retained browser, then select **Resume automation**. No second browser context is created; ownership, human actions, and resume are logged.

## Tests

```bash
ruff format --check .
ruff check .
mypy src
pytest -q
```

Coverage includes schema/compiler contracts, discovery stopping rules, replay and taxonomy integration, missing/duplicate/ambiguous locators, policy enforcement before the adapter, redaction/evidence, and handoff.

## Evidence

Sanitized reviewer evidence is under `evidence/submission/`:

- `example-artifact.json`: reusable capability.
- `replay-success/`: genuine Playwright replay and typed result.
- `business-outcome/`: genuine member-not-found replay.
- `human-handoff/`: genuine intervention, claim, same-session action, resume, and completion.

`evidence/submission/discovery/` is intentionally absent until run with real credentials. This workspace has neither `OPENAI_API_KEY` nor `COMPUTER_USE_LLM_MODEL`; a scripted or relabeled trace would not be genuine. Run the command above before final delivery to add it.

## Repository layout

```text
artifacts/                 versioned capability examples
demo_app/                  legacy-style FastAPI banking target
evidence/submission/       curated sanitized evidence
src/computer_use/
  agent/                   structured discovery
  api/                     API and operator UI
  artifacts/               Pydantic schema and compiler
  handoff/                 same-session transfer
  locator/                 deterministic locator models
  observability/           events, redaction, evidence
  policy/                  authorization and risk
  replay/                  interpreter and taxonomy
  surface/                 protocol and Playwright adapter
  cli.py                   reproducible commands
tests/                     unit and integration tests
REPORT.md                  design trade-offs and cuts
```

See [REPORT.md](REPORT.md) for limitations and defendable design decisions.

For instructions aimed at users of the deployed banking website, see the [website guide](demo_app/README.md).
