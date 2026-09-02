# PilotDeck-lite control plane

This document defines a deliberately small control-plane layer for long-running,
multi-agent work in Coding Tools MCP.  It borrows architectural ideas from
PilotDeck without turning the MCP runtime into a general agent operating system.

The first implementation is intentionally **deterministic, local-first, and
side-effect free until explicitly wired**.  It must not add an LLM judge, memory
extractor, background discovery loop, MCP tool, or web/client request on its own.

## Goals

1. Normalize agent/executor failures into a stable contract so recovery does not
   depend on parsing free-form error text.
2. Give persistence, validation, review, cleanup, and telemetry one lifecycle
   vocabulary with explicit blocking versus non-blocking failure semantics.
3. Preserve high-integrity task state across context compaction and fresh chats
   without replaying full transcripts.
4. Keep the existing execution engine authoritative for process control, atomic
   patching, filesystem confinement, protocol handling, and permission modes.
5. Reduce rather than increase web-client context pressure for long-running work.

## Non-goals for the first stages

The following are intentionally deferred:

- LLM-based task difficulty judging.
- Automatic semantic-memory extraction every turn.
- A vector database or Dream-style memory consolidation.
- Proactive task discovery.
- Autonomous background agents that invent work.
- Replacement of `processes.py`, `patching.py`, or the public MCP schemas.
- Direct integration with the current GitHub `main` multichat implementation;
  the production/local branch may be newer and must be reconciled separately.

## Invariants (Checkpoint 0)

These are release blockers for later integration work:

1. Atomic patch commit/rollback semantics must not regress.
2. Process timeout, group termination, and retained-output behavior must not
   regress.
3. Public CLI names, import paths, MCP schemas, and release behavior remain
   compatible unless a separately approved change requires otherwise.
4. Existing `safe`, `trusted`, and `dangerous` semantics remain compatible.
5. A stale or superseded result can never overwrite the current accepted result.
6. An accepted child/agent result must be durably persisted before cleanup is
   permitted.
7. Rehydrate returns bounded current state and references; it never replays a
   complete child-chat transcript or raw tool-output history.
8. Cleanup acts on exact recorded targets and produces exact verifiable receipts.

## Checkpoint 1: canonical errors and recovery

`coding_tools_mcp.control_plane.errors` defines provider-agnostic categories such
as transport, timeout, provider, auth, billing, context overflow, invalid output,
capability missing, permission denied, stale result, process crash, and task
failure.

A `CanonicalAgentError` carries explicit policy facts:

- `retryable`
- `fallback_allowed`
- `compact_allowed`
- `safe_to_replay`

These flags are intentionally explicit.  The web/client model should not have to
infer replay safety from a human-readable error message.

`decide_recovery()` is bounded and deterministic.  The initial policy is:

1. If user-visible content has already streamed, do not replay or fallback.
2. If side effects may have occurred, replay only when explicitly marked safe.
3. Recover context overflow by compaction when supported.
4. Allow at most the configured same-executor retry count.
5. Then allow a fallback only when the error says fallback and replay are safe.
6. Otherwise fail and surface the structured reason.

This mirrors a key property of robust streaming routers: once content has escaped
to the consumer, switching executors can duplicate output and is no longer a
safe transparent recovery.

## Checkpoint 2: lifecycle runtime

`LifecycleRuntime` is an in-process dispatcher.  It performs no I/O and makes no
model calls.  Handlers register for an event type and declare whether failure is
blocking.

Suggested event vocabulary for later integration:

- `task_created`
- `attempt_started`
- `executor_started`
- `executor_finished`
- `result_persisted`
- `validation_started`
- `validation_passed`
- `validation_failed`
- `result_accepted`
- `result_rejected`
- `review_started`
- `review_finished`
- `cleanup_started`
- `cleanup_finished`
- `cleanup_failed`
- `task_completed`
- `task_failed`

Examples of blocking handlers:

- durable accepted-result persistence;
- base/attempt identity validation;
- acceptance-gate verification required before cleanup.

Examples of non-blocking handlers:

- telemetry;
- optional trace persistence;
- optional memory capture.

A blocking error stops later handlers for that event.  A non-blocking error is
recorded and dispatch continues.

## Checkpoint 3: protected state and bounded rehydrate

`ProtectedTaskState` separates exact task state from disposable conversation
history.  Protected fields currently include:

- task id and attempt id;
- base SHA and workspace identity;
- acceptance criteria;
- accepted result ids;
- superseded attempts and stale result ids;
- exact cleanup targets;
- exact cleanup receipts.

Working-state fields such as completed work, pending work, blockers, and evidence
references are useful but may be clipped when a rehydrate budget is reached.

### No silent truncation of protected state

`render_rehydrate()` treats protected state atomically.  If protected state alone
cannot fit the configured budget, it raises `RehydrateBudgetExceeded` rather
than dropping an acceptance criterion, stale-result marker, or cleanup receipt.

This is intentional: a larger explicit budget is safer than a plausible-looking
summary that silently lost authority-bearing state.

### Transcript-free by construction

The protected schema has no transcript, message-history, or raw-tool-output
field.  Large evidence belongs in durable artifacts; rehydrate carries stable
references only.

### Fingerprint

Protected state has a SHA-256 fingerprint over canonical JSON.  Working-summary
changes do not change this fingerprint; accepted/superseded/cleanup identity
changes do.  Later collectors can use this to detect accidental mutation across
compaction or handoff boundaries.

## Web/client load budget

The first three checkpoints are designed to have effectively zero additional
web/model request cost:

| Capability | Extra model calls | Extra web requests | Expected context effect |
| --- | ---: | ---: | --- |
| canonical errors | 0 | 0 | neutral or lower |
| deterministic recovery | 0 | 0 | lower failed-turn churn |
| lifecycle dispatch | 0 | 0 | neutral |
| protected state | 0 | 0 | neutral |
| bounded rehydrate | 0 | 0 | lower for long tasks |

The following later features are **not** allowed into the default hot path
without a separate measurement gate:

- LLM router judge: may add roughly one model call per routed turn.
- Per-turn LLM memory extraction: may add roughly one model call per turn.
- Mandatory dual-agent review: may add one or two secondary agent calls.
- Proactive Always-On discovery: can create unbounded task demand if not budgeted.

Before enabling any of those by default, measure calls/turn, tokens/rehydrate,
latency, failure recovery rate, and background concurrency against a baseline.

## Integration order

1. Keep the new package unregistered and run unit/CI checks.
2. Reconcile this branch with the current local/production multichat baseline.
3. Wire canonical errors at executor boundaries without changing retry behavior.
4. Emit lifecycle events in shadow mode; compare traces with current behavior.
5. Produce rehydrate v2 alongside the current implementation and compare output
   size and acceptance-state completeness.
6. Only after parity is demonstrated, switch consumers to the new state path.
7. Add deterministic router-lite and context compaction in later checkpoints.

## Acceptance gates for Checkpoints 0-3

A change is not ready for production wiring unless all of the following hold:

- Existing patch/process/protocol tests remain green.
- A retry requires replay safety and respects its retry budget.
- Streamed output prevents transparent retry/fallback.
- Possible side effects prevent replay unless explicitly safe.
- Context overflow chooses compaction when allowed.
- Non-blocking lifecycle failure does not prevent later handlers.
- Blocking lifecycle failure prevents later handlers.
- Protected rehydrate preserves accepted, superseded, stale, and cleanup fields
  exactly.
- Protected state that exceeds the budget fails loudly rather than truncating.
- Working state can be clipped while protected state remains intact.
- Rehydrate contains no full transcript or raw tool history by construction.
- Protected-state fingerprint ignores working-summary changes and changes when an
  authority-bearing protected field changes.

## External adversarial review

The preferred integration workflow uses independent agy and CodeBuddy/HY4 review
for medium/high-risk wiring changes.  Their verdicts must be recorded as evidence,
not assumed.  This document and the initial primitives do not claim those reviews
have run when the relevant executor tools are unavailable in the active session.
