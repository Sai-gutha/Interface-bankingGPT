# Architecture

The project is a typed modular monolith built around one boundary: `SurfaceAdapter`. Discovery, policy, compilation, replay, observability, and handoff use neutral models; only `PlaywrightSurfaceAdapter` knows browser pages and locators. This keeps the slice small while leaving a credible seam for accessibility-tree, coordinate, or desktop adapters.

Discovery may ask an LLM for a strict `AgentDecision`, but validation and policy run before every action. A successful neutral recording is compiled into an artifact. Replay interprets that artifact in order and has no LLM dependency. FastAPI serves the deliberately imperfect banking target and a minimal operator console. In-memory coordination suits a take-home; production needs durable state, authentication, and distributed leases.

# Artifact schema

`CapabilityArtifact` is the reusable contract, not an LLM transcript. It identifies schema and capability versions, target application, typed inputs/outputs, ordered actions, alternative locators, extraction rules, checkpoints, business outcomes, bounded recovery, risk, and tenant/vendor metadata. Runtime examples such as member `12345` become `{{member_id}}`.

Locators are declarative candidates ordered by semantic strength: role/name, label, visible or relative text, stable attributes, structural selectors, then coordinates. The deterministic compiler removes exact cycles/no-ops, normalizes candidates, parameterizes known values, and rejects an artifact if a registered sensitive value remains. The trade-off is explicit: compilation needs a `CompilerSpec`; it does not infer a perfect generalized workflow from one trace.

# Determinism & error handling

Replay validates inputs, resolves templates, evaluates policy, executes ordered steps, performs condition-based waits, verifies checkpoints, and validates outputs. It never asks an LLM to choose an action or fallback locator. Multiple plausible elements are an error rather than an invitation to pick the first.

The taxonomy separates legitimate business outcomes (`MEMBER_NOT_FOUND`, `INVALID_INPUT`, `ACCOUNT_NOT_FOUND`), recoverable runtime conditions, and hard failures. Recovery occurs only when both taxonomy and artifact permit a bounded retry/directive. Results are typed as `SUCCESS`, `BUSINESS_OUTCOME`, `RECOVERED_SUCCESS`, `HARD_FAILURE`, or `HUMAN_REQUIRED`, with failed step, expected/observed state, error code, and evidence paths where applicable.

This favors reproducibility over resilience to arbitrary redesigns. A material UI change should fail visibly and prompt artifact review, not cause replay to improvise.

# Heterogeneity & multi-tenant

The core sees only targets, observations, screenshots, and surface state. Browser startup, DOM inspection, and Playwright translation stop inside the adapter. Another adapter can honor the same operations using an accessibility API or desktop driver while rejecting unsupported locator kinds.

Artifacts carry vendor/tenant metadata and target compatibility information. This slice omits tenant-specific patch layering; cloning and versioning is safer than premature inheritance. Production work would add signed artifacts, constrained overrides, compatibility fingerprints, and migrations.

# Escalation & handoff

The coordinator retains the exact live adapter and browser context. Intervention transfers ownership from `AUTOMATION` to `HUMAN`, records the goal/capability, step, reason, screenshot, URL, and state, then blocks on an awaitable resume gate. An exclusive operator claim and increasing epoch reject competing or stale controls.

The FastAPI operator page can claim the session, issue typed label/coordinate actions, and resume. Actions use the same adapter and event stream. Resume either verifies a human-completed checkpoint or grants approval only for the paused step before policy is re-evaluated. Coordination is process-local, so restarts lose leases and active sessions.

# Safety

Every concrete action passes through `ExplicitPolicyEngine` immediately before the adapter. Policy restricts origins, routes, action types, maximum risk, coordinate use, and approvals. `READ_ONLY`, `REVERSIBLE_WRITE`, and `IRREVERSIBLE` are explicit; irreversible actions are blocked by default unless the exact step is approved. Denials override allows.

The redactor removes registered secrets, credential-shaped strings, and sensitive-key values before logs or artifacts persist. LLM prompts/responses and chain-of-thought are not retained. Screenshots and sanitized page snapshots can still reveal context, so production evidence needs encryption, retention limits, access control, and tenant isolation.

# Cuts

Deliberate omissions include distributed orchestration, persistent session recovery, authentication/RBAC, artifact signing, a visual editor, automatic tenant adaptation, and broad device coverage. Recovery supports a small explicit directive set rather than arbitrary scripts. The demo uses synthetic data and one local Chromium session.

Checked-in evidence includes a real Playwright success, a member-not-found outcome, and same-session handoff/resume. It does not claim genuine LLM discovery: no API key or model was available. The documented discovery command produces that evidence when credentials are supplied; fabricating it would undermine the submission. Next priorities are that credentialed run and durable authenticated handoff—not feature breadth.
