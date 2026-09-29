/**
 * DSH session events into AER's vocabulary.
 *
 * This is the only file in the repository that knows DSH's event names. Everything
 * downstream of it sees {@link NormalizedEnvelope}. That is the boundary the
 * milestone is built around: the harness may rename `callId` tomorrow, and the only
 * thing that breaks is this file and its tests.
 *
 * Three rules decide what is here, and each one is the answer to a specific way the
 * integration could lie:
 *
 * 1. **Durable events only.** The mapping reads `SessionEvent`s and nothing else. The
 *    live extension points carry the same facts (`tools/pre-execute`, `tools/result`)
 *    and mapping both would write every tool call twice (round-8.2 section 9).
 * 2. **A stated fact, never an inferred one.** `tool/result` carries `isError`
 *    structurally, so AER records success *and* failure from the payload. This is the
 *    one place DSH is strictly better than Codex, where a failed tool left the
 *    outcome unstated (`D-097`).
 * 3. **No verdicts.** `turn/end` reports why the *loop* stopped. Only two of its six
 *    reasons are declarations about the work; the rest say nothing, and are mapped to
 *    a status that claims nothing (`D-100`).
 *
 * Reasoning content is dropped here rather than downstream. `assistant/attempt`
 * embeds the model's raw stream, including thinking blocks, so it is not mapped at
 * all; `assistant/message` is mapped with reasoning blocks removed (round-8.2
 * section 77).
 *
 * @module @aer/dsh-adapter/mapping
 */

import type { SessionEvent, SessionHeader, SessionEventMap, TurnEndReason } from '@deepseek-ai/dsh-session';

import type {
  NormalizedEnvelope,
  NormalizedEventType,
  NormalizedModel,
  NormalizedSession,
  NormalizedTurnEnd,
  NormalizedTurnOutcome,
  ToolCallFact,
  ToolResultFact,
} from './protocol.js';

/** DSH's durable event names this adapter reads. Anything else is dropped by name. */
export type MappedEventType =
  | 'turn/start'
  | 'turn/end'
  | 'step/start'
  | 'step/end'
  | 'tool/call'
  | 'tool/result'
  | 'assistant/message'
  | 'user/message'
  | 'request/header';

/** Event types that are recognised but produce no AER evidence, with the reason. */
export const DROPPED_EVENT_TYPES: ReadonlyMap<string, string> = new Map([
  ['assistant/attempt', 'embeds the raw model stream including reasoning (section 77)'],
  ['request/context', 'route metadata; identity comes from request/header'],
  ['request/header', 'consumed for model identity, produces no event of its own'],
  ['step/start', 'paired with step/end; the model call it opens is recorded from assistant/message'],
  ['step/end', 'no AER event type corresponds to a step boundary'],
  ['user/message', 'consumed as the run task text, not recorded as an event'],
  ['system/message', 'context assembly, not an execution fact'],
  ['subagent/catalog', 'subagent topology; recorded as metadata only (section 56)'],
  ['subagent/descriptor', 'subagent topology; recorded as metadata only (section 56)'],
  ['approval/asked', 'a permission request is not an error and not a tool call (section 22)'],
  ['approval/decided', 'a permission decision is a human/system action, not agent evidence'],
  ['approval/policy', 'policy configuration'],
  ['compaction/start', 'context compaction is not an agent experience'],
  ['compaction/end', 'context compaction is not an agent experience'],
  ['compaction/prune', 'context compaction is not an agent experience'],
  ['compaction/summary', 'context compaction is not an agent experience'],
  ['todo/write', 'plan state; not an execution fact'],
  ['plan/mode', 'plan state; not an execution fact'],
  ['goal/change', 'goal state; not an execution fact'],
  ['model/selection', 'model identity is read from request/header instead'],
  ['llm/retry', 'retry policy; the failed attempt is visible through assistant/attempt'],
  ['llm/retry-started', 'retry policy'],
]);

/** The status a turn's ending justifies, by the reason DSH reported. */
const TURN_OUTCOME: ReadonlyMap<string, NormalizedTurnOutcome> = new Map([
  // DSH's `completed` means the loop stopped normally. It is not the task
  // succeeding: the model decided it was done, which is exactly the claim AER
  // refuses to accept as evidence (section 16, D-100).
  ['completed', 'INCONCLUSIVE'],
  // A cancellation request interrupted the turn: somebody stopped the work.
  ['aborted', 'ABORTED'],
  // A structured failure ended the turn.
  ['error', 'FAILED'],
  // The turn was blocked before it could proceed. Nothing was declared.
  ['blocked', 'INCONCLUSIVE'],
  // At least one step hit its output ceiling. That is a truncation fact, not a
  // statement that the task partly succeeded -- the harness never declared that.
  ['max-tokens', 'INCONCLUSIVE'],
  // A crash-orphaned turn closed after the fact: the writer died mid-turn. The
  // strongest possible case for "nobody said anything".
  ['interrupted', 'INCONCLUSIVE'],
]);

/**
 * The status a turn's ending justifies.
 *
 * An unknown reason returns `INCONCLUSIVE` rather than throwing: DSH's
 * `TurnEndReasonMap` is merge-extensible, so a plugin (or a newer harness) may add a
 * variant this adapter has never seen. Closing the run without claiming anything is
 * the correct response to a reason we do not understand; refusing to close it would
 * leave the run `RUNNING` forever, and guessing would be worse than either (D-100).
 */
export function turnOutcome(reasonKind: string): NormalizedTurnOutcome {
  return TURN_OUTCOME.get(reasonKind) ?? 'INCONCLUSIVE';
}

/** The `kind` discriminant of a turn-end reason, without assuming the union's shape. */
export function turnReasonKind(reason: TurnEndReason): string {
  const kind = (reason as { kind?: unknown }).kind;
  return typeof kind === 'string' ? kind : 'unknown';
}

/** `SessionHeader` into the session identity AER records. */
export function sessionFromHeader(header: SessionHeader): NormalizedSession {
  // Read through a narrow view: the header is DSH's, and a Developer Preview release
  // may add fields. Only the ones AER records are named.
  const view = header as unknown as Record<string, unknown>;
  return {
    external_session_id: String(view.id),
    cwd: typeof view.cwd === 'string' ? view.cwd : null,
    agent_preset: typeof view.agentPreset === 'string' ? view.agentPreset : null,
    parent_session_id: typeof view.parentSession === 'string' ? view.parentSession : null,
    delegation_depth: typeof view.delegationDepth === 'number' ? view.delegationDepth : null,
    origin: typeof view.origin === 'string' ? view.origin : null,
    created_at: typeof view.createdAt === 'string' ? view.createdAt : null,
  };
}

/** `event.time` is epoch milliseconds; AER records ISO-8601. */
export function eventTimeIso(event: SessionEvent): string {
  return new Date(event.time).toISOString();
}

function baseMetadata(event: SessionEvent, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    dsh: {
      event: event.type,
      seq: event.seq,
      turn: turnOf(event),
      ...extra,
    },
  };
}

/** The turn an event belongs to, or `null` when it carries none. */
export function turnOf(event: SessionEvent): number | null {
  const data = event.data as Record<string, unknown> | undefined;
  const turn = data?.['turn'];
  return typeof turn === 'number' ? turn : null;
}

/** The step an event belongs to, or `null`. */
export function stepOf(event: SessionEvent): number | null {
  const data = event.data as Record<string, unknown> | undefined;
  const step = data?.['step'];
  return typeof step === 'number' ? step : null;
}

function envelope(
  event: SessionEvent,
  sessionId: string,
  eventType: NormalizedEventType,
  payload: Record<string, unknown>,
  extraMetadata: Record<string, unknown> = {},
  /** Distinguishes several envelopes derived from one durable event. */
  suffix = '',
): NormalizedEnvelope {
  const turn = turnOf(event);
  return {
    event_type: eventType,
    external_event_id: `${sessionId}:${event.seq}${suffix}`,
    external_session_id: sessionId,
    external_turn_id: turn === null ? '' : String(turn),
    external_sequence: typeof event.seq === 'number' ? event.seq : 0,
    external_timestamp: eventTimeIso(event),
    payload,
    metadata: baseMetadata(event, extraMetadata),
  };
}

/** `tool/call` reduced to the facts AER records. */
export function toolCallFact(event: SessionEvent): ToolCallFact | null {
  const data = event.data as SessionEventMap['tool/call'] | undefined;
  if (data === undefined || typeof data.callId !== 'string' || typeof data.name !== 'string') {
    return null;
  }
  return {
    // `arguments` is the raw JSON string exactly as the model produced it, so it is
    // already text; it is truncated and redacted on the Python side, which owns the
    // sanitisation rules for every adapter.
    call_id: data.callId,
    name: data.name,
    arguments_json: typeof data.arguments === 'string' ? data.arguments : '',
    turn: data.turn,
    step: data.step,
  };
}

/**
 * `tool/result` reduced to the facts AER records.
 *
 * The correlation id and the outcome are both structural, so neither is guessed:
 * `message.content[0].toolCallId` pairs the result with its call, and
 * `message.content[0].isError` is the outcome DSH published. An absent `isError`
 * means the harness did not say, which is recorded as `false` only because DSH's
 * own type documents the flag as the *error* marker -- a result without it is not an
 * error result.
 */
export function toolResultFact(event: SessionEvent): ToolResultFact | null {
  const data = event.data as SessionEventMap['tool/result'] | undefined;
  if (data === undefined) {
    return null;
  }
  const message = data.message as unknown as Record<string, unknown> | undefined;
  const blocks = message?.['content'];
  const block = Array.isArray(blocks) ? (blocks[0] as Record<string, unknown> | undefined) : undefined;
  if (block === undefined) {
    return null;
  }
  const callId = block['toolCallId'];
  if (typeof callId !== 'string') {
    return null;
  }
  const error = data.error as { name?: unknown; code?: unknown } | undefined;
  return {
    call_id: callId,
    name: '',
    is_error: block['isError'] === true,
    error_name: typeof error?.name === 'string' ? error.name : null,
    error_code: typeof error?.code === 'string' ? error.code : null,
    output_summary: summarizeBlocks(block['content']),
    turn: data.turn,
    step: data.step,
  };
}

/**
 * Model-facing text out of a tool result's content blocks.
 *
 * Reasoning blocks are skipped: they exist in the model's stream, not in the tool's
 * answer, and AER never stores private reasoning (section 77). Images and files are
 * counted rather than inlined, because their bytes belong to the attachment service
 * and a placeholder is more honest than a truncated blob.
 */
export function summarizeBlocks(blocks: unknown): string {
  if (!Array.isArray(blocks)) {
    return '';
  }
  const parts: string[] = [];
  for (const raw of blocks) {
    const block = raw as Record<string, unknown>;
    const kind = block['type'];
    if (kind === 'reasoning') {
      continue;
    }
    if (kind === 'text' && typeof block['text'] === 'string') {
      parts.push(block['text']);
      continue;
    }
    if (typeof kind === 'string') {
      parts.push(`<${kind}>`);
    }
  }
  return parts.join('\n');
}

/** `request/header` into the provider route and model DSH actually resolved. */
export function modelFromHeader(event: SessionEvent): NormalizedModel | null {
  const data = event.data as SessionEventMap['request/header'] | undefined;
  const config = (data?.header as unknown as Record<string, unknown> | undefined)?.['config'] as
    | Record<string, unknown>
    | undefined;
  const provider = config?.['provider'];
  const model = config?.['model'];
  if (typeof provider !== 'string' || typeof model !== 'string') {
    return null;
  }
  return { provider, model };
}

/**
 * Translate one durable event.
 *
 * Returns an empty array for every event AER has no business recording, which is
 * most of them: a session that ran twenty tool calls may have a hundred durable
 * events, and the ones that are not evidence are dropped by name (see
 * {@link DROPPED_EVENT_TYPES}).
 */
export function normalizeEvent(sessionId: string, event: SessionEvent): NormalizedEnvelope[] {
  switch (event.type) {
    case 'tool/call': {
      const fact = toolCallFact(event);
      if (fact === null) {
        return [];
      }
      return [
        envelope(
          event,
          sessionId,
          'TOOL_CALL',
          {
            tool: fact.name,
            call_id: fact.call_id,
            arguments: fact.arguments_json,
            step: fact.step,
          },
          { call_id: fact.call_id },
        ),
      ];
    }

    case 'tool/result': {
      const fact = toolResultFact(event);
      if (fact === null) {
        return [];
      }
      const payload: Record<string, unknown> = {
        tool: fact.call_id,
        call_id: fact.call_id,
        success: !fact.is_error,
        result: fact.output_summary,
        step: fact.step,
      };
      if (fact.is_error) {
        // The failure travels *inside* the result: AER's ingest path turns a result
        // carrying an `error` into ERROR-then-TOOL_RESULT, which is the order the
        // trace wants, and emitting a separate ERROR envelope here would record the
        // same failure twice (the same reasoning as the Codex adapter).
        payload['error'] = {
          error_type: errorTypeOf(fact),
          message: fact.output_summary,
        };
      }
      return [envelope(event, sessionId, 'TOOL_RESULT', payload, { call_id: fact.call_id })];
    }

    case 'step/start': {
      const step = stepOf(event);
      return [
        envelope(
          event,
          sessionId,
          'MODEL_CALL',
          { step: step ?? 0 },
          {},
          // `step/start` and the `assistant/message` that answers it come from
          // different durable events, so they carry different seqs already; no
          // suffix is needed. It is passed explicitly to keep the intent visible.
          '',
        ),
      ];
    }

    case 'assistant/message': {
      // The model's visible answer. Reasoning blocks are dropped by
      // `summarizeBlocks`, so thinking never reaches AER (section 77).
      const data = event.data as SessionEventMap['assistant/message'] | undefined;
      const message = data?.message as unknown as Record<string, unknown> | undefined;
      return [
        envelope(event, sessionId, 'MODEL_RESULT', {
          step: stepOf(event) ?? 0,
          text: summarizeBlocks(message?.['content']),
        }),
      ];
    }

    default:
      return [];
  }
}

function errorTypeOf(fact: ToolResultFact): string {
  if (fact.error_name !== null && fact.error_code !== null) {
    return `${fact.error_name}.${fact.error_code}`;
  }
  if (fact.error_name !== null) {
    return fact.error_name;
  }
  return 'dsh.ToolError';
}

/** `turn/end` into the turn's closing fact, or `null` when the event is not one. */
export function turnEndFrom(session: NormalizedSession, event: SessionEvent): NormalizedTurnEnd | null {
  if (event.type !== 'turn/end') {
    return null;
  }
  const data = event.data as SessionEventMap['turn/end'] | undefined;
  if (data === undefined || typeof data.turn !== 'number') {
    return null;
  }
  const reason = data.reason as unknown as Record<string, unknown>;
  const reasonKind = turnReasonKind(data.reason);
  return {
    session,
    turn: data.turn,
    reason_kind: reasonKind,
    outcome: turnOutcome(reasonKind),
    detail: detailOf(reasonKind, reason),
  };
}

/**
 * The extra facts a reason carries, flattened to JSON.
 *
 * `error` carries DSH's structured `LlmFailure` and `aborted` a cancellation cause;
 * both are recorded so a reader can tell *why* a turn ended without AER having to
 * interpret them.
 */
function detailOf(reasonKind: string, reason: Record<string, unknown>): Record<string, unknown> {
  if (reasonKind === 'error') {
    const failure = reason['error'] as Record<string, unknown> | undefined;
    return {
      message: typeof failure?.['message'] === 'string' ? failure['message'] : null,
      code: typeof failure?.['code'] === 'string' ? failure['code'] : null,
      status: typeof failure?.['status'] === 'number' ? failure['status'] : null,
    };
  }
  if (reasonKind === 'aborted') {
    const cause = reason['reason'] as Record<string, unknown> | undefined;
    return { cause: typeof cause?.['kind'] === 'string' ? cause['kind'] : null };
  }
  return {};
}

/**
 * The task text for the AER run opened by a turn.
 *
 * The user's prompt is taken from the durable `user/message` events that belong to
 * the turn, not from the CLI's argument: a turn's input may have been queued, edited
 * or produced by a relay, and the log is what actually entered the model's context.
 * Plugin-sourced messages are skipped -- AER's own injected context must never
 * become the description of the task (section 29's principle, one layer up).
 */
export function taskTextForTurn(
  sessionId: string,
  events: readonly SessionEvent[],
  turn: number,
): string {
  const parts: string[] = [];
  for (const event of events) {
    if (event.type !== 'user/message' || turnOf(event) !== turn) {
      continue;
    }
    // `user/message` carries the message itself, unlike `assistant/message` and
    // `system/message`, which wrap theirs in `{ message }`.
    const message = event.data as unknown as Record<string, unknown>;
    const source = message['source'] as Record<string, unknown> | undefined;
    if (source?.['kind'] !== 'user') {
      // Injected context (a plugin's recall message) is not the task. Skipping it is
      // what keeps a run from describing itself as "AER experience context".
      continue;
    }
    const text = summarizeBlocks(message['content']);
    if (text !== '') {
      parts.push(text);
    }
  }
  void sessionId;
  return parts.join('\n');
}
