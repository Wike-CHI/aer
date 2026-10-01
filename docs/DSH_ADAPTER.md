# DSH Adapter (M8.2)

AER's integration with the [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness).

## Tested versions

| Component | Version | Source |
|---|---|---|
| `@deepseek-ai/dsh` | `0.1.5-rc.1` | `dsh --version` |
| Node | `v22.22.2` | `node --version` |
| AER | `0.6.1` (working tree, M8 uncommitted) | `git describe` |
| Adapter protocol | `1` | `AER_ADAPTER_PROTOCOL_VERSION` |
| Bridge protocol | `1` | `AER_BRIDGE_PROTOCOL_VERSION` |
| DSH session format | `3` | `SESSION_FORMAT_VERSION` |

DSH is a **Developer Preview**. Everything below was read off the installed package's
shipped types, not off documentation, because at this maturity the two disagree.

## Why this is not a hook bridge

DSH ships compatibility bridges (`dsh-hooks-codex`, `dsh-hooks-claude-code`) that turn
harness events into shell commands. AER does not use them. DSH already has what AER
needs natively:

* a **typed durable session log** — every model-visible fact, append-only, with a
  monotonic `seq`;
* a **typed tool pipeline** — calls and results carrying `callId` and a structural
  `isError`;
* **model-facing context injection** — `agent/pre-step` can add a message to the very
  request the model is about to see.

Routing through a shell bridge would discard the first two and make the third
impossible. So the adapter is a native Cordis plugin.

## Architecture

```text
DeepSeek Harness                    TypeScript
  session/event  ──────────────►  integrations/dsh/src/index.ts
  agent/pre-step ──────────────►  (durable facts / retrieval + injection)
                                        │
                                        │ JSON Lines, stdin/stdout
                                        ▼
                                  aer/adapter/dsh/bridge.py
                                        │
                                        ▼
                                  aer/adapter/dsh/adapter.py  (RuntimeSink)
                                        │
                                        ▼
                                  AdapterIngestor → AER Runtime
```

Two extension points, and the split is the design in one sentence: **durable events say
what happened; the live pre-step hook decides what the model sees next.**

## Native extension points used

| Extension point | Purpose |
|---|---|
| `session/event` | Every durable execution fact, post-commit, once |
| `agent/pre-step` | Retrieval and context injection into the model request |

Deliberately **not** subscribed: `tools/pre-execute`, `tools/post-execute`,
`tools/result`. They carry the same tool facts as the durable log, and mapping both
would write every call twice. The durable log is the single authority
(one event, one authority).

## Durable events used

From `SessionEventMap` in `@deepseek-ai/dsh-session`:

| Event | Maps to |
|---|---|
| `turn/start` | opens an AER run |
| `turn/end` | closes it, with the reason DSH stated |
| `tool/call` | `TOOL_CALL` |
| `tool/result` | `TOOL_RESULT` (+ `ERROR` when `isError`) |
| `assistant/message` | `MODEL_RESULT` (reasoning blocks dropped) |
| `step/start` | `MODEL_CALL` |
| `request/header` | provider/model identity only, produces no event |
| `user/message` | the turn's task text; skipped when source is a plugin |

`KNOWN_SESSION_EVENT_TYPES` in the installed build holds **56** types. The rest are
dropped by name with a stated reason (`DROPPED_EVENT_TYPES` in `mapping.ts`), including
`assistant/attempt`, which embeds the model's raw stream and therefore reasoning.

## Run granularity decision

**One DSH turn is one AER run.**

DSH publishes `turn/start` and `turn/end`, so a turn has a definite beginning, ending,
stated reason and input — much closer to "one attempt at a task" than a session, which
may contain several unrelated tasks.

The consequence: **one external session maps to many AER runs**. AER expresses that with
`AdapterIngestor.reopen()`, which opens a new run while keeping earlier run ids on the
mapping. Nothing in DSH's semantics had to be bent.

## Turn outcome mapping

`TurnEndReasonMap` has six variants. Only two are declarations about the work.

| DSH reason | AER status | Why |
|---|---|---|
| `completed` | `INCONCLUSIVE` | the loop stopped normally; not the task succeeding |
| `aborted` | `ABORTED` | a cancellation interrupted the turn |
| `blocked` | `INCONCLUSIVE` | nothing was declared |
| `error` | `FAILED` | a structured failure ended the turn |
| `max-tokens` | `INCONCLUSIVE` | a truncation fact, not partial success |
| `interrupted` | `INCONCLUSIVE` | a crash-orphaned turn; nobody said anything |
| *(unrecognized)* | `INCONCLUSIVE` | `TurnEndReasonMap` is merge-extensible |

`completed → INCONCLUSIVE` is the load-bearing choice. DSH's `completed` means the agent
loop stopped normally; it is not evidence the task was solved. Verification is what
settles that.

## Tool mapping

`tool/result` carries its outcome structurally, which is where DSH is strictly better
than Codex (where a failed tool left the outcome unstated, `D-097`):

* `message.content[0].isError === true` → `ERROR` then `TOOL_RESULT(success=false)`
* otherwise → `TOOL_RESULT(success=true)`

`data.error` is `{ name, code }` — there is no `reason` and no stack trace, so
`ErrorRecord.stack_trace` stays `None` rather than being fabricated.

Correlation uses `callId` (`tool/call.callId` ↔ `message.content[0].toolCallId`), never
arrival order, because DSH executes tools concurrently.

## Session seq, idempotency and replay

`SessionSeq` is zero-based and contiguous, so the external event id is
`session_id + seq`. The same id delivered twice is `DUPLICATE` and produces no second
event.

Each session holds `last_ingested_seq` in the plugin. If the bridge was down, the
adapter re-reads DSH's own log from the cursor (`session.snapshotEvents`) — **provider-
owned replay**: the events come from DSH, not from a copy AER kept, so no transcript is
duplicated into SQLite.

A seq gap is detected. If DSH's public API cannot reconcile it, the gap is reported as
such rather than silently skipped.

## Retrieval injection

This is what M8.2 adds over M8.1.

```text
turn begins → retrieve_for_run() → ExperienceContextFormatter
            → injected into the model request via agent/pre-step
            → durable user/message confirms it → record_injection()
```

The injected message is a `user`-role message whose source is
`{ kind: 'plugin', plugin: 'aer-dsh', form: 'recall' }`. `recall` is DSH's own name for
"material lifted out of another session's log", which is exactly what an AER experience
is. It is deliberately **not** `instructions`: experience is historical evidence, not
content the model is expected to obey.

`record_injection()` is called only after the plugin sees its own message come back in
the durable log. That is what makes *retrieved but never injected* a distinguishable
state, and it is the injection proof: the durable log — not the plugin's memory — is the
evidence that AER's context entered a model request.

Reminder: injection is recorded with a `context_fingerprint` and `formatter_version`, so
a reader can tell which rendering of the context actually reached the model.

## Adoption and utility

Not claimed. DSH gives no explicit adoption or utility signal, so:

* `explicit_adoption_signal = False` — adoption stays `UNKNOWN` even when the agent
  visibly follows the retrieved advice;
* `explicit_utility_signal = False` — no `HELPFUL` label is ever written, not even when
  the task succeeds.

Adding an `aer_experience_used` tool to make adoption observable was considered and
rejected for this milestone: it would change the agent's tool surface.

## Capabilities

```text
tool_events                = true
context_injection          = true
session_linkage            = true
explicit_adoption_signal   = false
explicit_utility_signal    = false
human_feedback             = false
decision_summary           = false
external_verification      = false
```

`context_injection` is new in M8.2, added to `AdapterCapabilities` because DSH is the
first integration that can genuinely put retrieved text into a model request. It is a
general capability, not a DSH special case: a hook-based integration that can only
observe a transcript cannot claim it.

## Verification integration

Neither `turn/end completed` nor `tool/result isError=false` is verification. A
deterministic tool result (a passing `pytest`) can be *evidence* for a verifier, but
`VerificationRecord` is still written by AER's own `VerificationEngine`. The first
version does not treat every successful shell command as a verifier.

## What was actually run

Real `dsh` on this machine, plugin loaded from `integrations/dsh` via the `aer-dev`
profile.

**Captured:**

* plugin load and `apply()` (`--dump-config` and a real boot);
* the full operation sequence `session.begin → experience.retrieve → turn.begin →
  event.ingest → experience.injected → turn.end`;
* durable events with `seq` starting at 0 and running to 17 — which is what exposed the
  cursor bug below;
* retrieval failing open when the bridge is slow (§51), with the turn proceeding.

**Not captured:**

* a real model turn producing `tool/call` / `tool/result`, and therefore `completed`,
  `error`, `aborted` and `max-tokens` in the wild. The configured DeepSeek credential
  is rejected (`AUTH: api key is invalid`) and the workstation's default route
  (`openai-codex`) has no adapter in this bundle. These are contract-tested against
  DSH's shipped types instead, and are not presented as captured.

## Two defects found by running it

1. **The first event of every session was dropped.** The cursor started at `0`, but
   `SessionSeq` is zero-based, so `seq <= cursor` discarded `seq 0`. DSH's own
   `SessionSeqCursor` uses `-1` for "no event yet"; the adapter now does the same.

2. **The first retrieval of every session timed out.** `start()` set `state = 'ready'`
   immediately after `spawn`, but the Python side takes ~1.5 s to import AER before it
   reads a line (measured). The 2 s retrieval budget was therefore paying for
   interpreter startup, and the session degraded from its first turn. The bridge now
   exchanges a warmup `ping` before it reports itself ready; the retrieval timeout stays
   short, which is correct only because startup has already been paid.

## Known limitations

* Provider-owned replay is implemented, but the reconciliation path has not been
  exercised against a long-running session in production.
* Subagents (`subagent/start`, `subagent/end`) are Cordis events, not durable session
  events. Only the durable topology events are recorded, as metadata; a child is not
  automatically an AER run.
* PTC (`tool/ptc-dispatch`) is recognised but produces no AER evidence; nested code-mode
  calls are not yet mapped.
* MCP tools go through the same pipeline and need no special case, but this has not been
  exercised against a live MCP server.
* The bridge is a subprocess over stdin/stdout. A socket transport is a possible later
  milestone; the protocol is explicit enough to survive it.
* Usage is persisted in the M7 `experience_usage` table (revision 0005).
  Experience deletion semantics must be checked against that revision before adding
  destructive retention tools.
