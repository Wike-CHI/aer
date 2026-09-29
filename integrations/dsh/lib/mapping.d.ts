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
import type { SessionEvent, SessionHeader, TurnEndReason } from '@deepseek-ai/dsh-session';
import type { NormalizedEnvelope, NormalizedModel, NormalizedSession, NormalizedTurnEnd, NormalizedTurnOutcome, ToolCallFact, ToolResultFact } from './protocol.js';
/** DSH's durable event names this adapter reads. Anything else is dropped by name. */
export type MappedEventType = 'turn/start' | 'turn/end' | 'step/start' | 'step/end' | 'tool/call' | 'tool/result' | 'assistant/message' | 'user/message' | 'request/header';
/** Event types that are recognised but produce no AER evidence, with the reason. */
export declare const DROPPED_EVENT_TYPES: ReadonlyMap<string, string>;
/**
 * The status a turn's ending justifies.
 *
 * An unknown reason returns `INCONCLUSIVE` rather than throwing: DSH's
 * `TurnEndReasonMap` is merge-extensible, so a plugin (or a newer harness) may add a
 * variant this adapter has never seen. Closing the run without claiming anything is
 * the correct response to a reason we do not understand; refusing to close it would
 * leave the run `RUNNING` forever, and guessing would be worse than either (D-100).
 */
export declare function turnOutcome(reasonKind: string): NormalizedTurnOutcome;
/** The `kind` discriminant of a turn-end reason, without assuming the union's shape. */
export declare function turnReasonKind(reason: TurnEndReason): string;
/** `SessionHeader` into the session identity AER records. */
export declare function sessionFromHeader(header: SessionHeader): NormalizedSession;
/** `event.time` is epoch milliseconds; AER records ISO-8601. */
export declare function eventTimeIso(event: SessionEvent): string;
/** The turn an event belongs to, or `null` when it carries none. */
export declare function turnOf(event: SessionEvent): number | null;
/** The step an event belongs to, or `null`. */
export declare function stepOf(event: SessionEvent): number | null;
/** `tool/call` reduced to the facts AER records. */
export declare function toolCallFact(event: SessionEvent): ToolCallFact | null;
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
export declare function toolResultFact(event: SessionEvent): ToolResultFact | null;
/**
 * Model-facing text out of a tool result's content blocks.
 *
 * Reasoning blocks are skipped: they exist in the model's stream, not in the tool's
 * answer, and AER never stores private reasoning (section 77). Images and files are
 * counted rather than inlined, because their bytes belong to the attachment service
 * and a placeholder is more honest than a truncated blob.
 */
export declare function summarizeBlocks(blocks: unknown): string;
/** `request/header` into the provider route and model DSH actually resolved. */
export declare function modelFromHeader(event: SessionEvent): NormalizedModel | null;
/**
 * Translate one durable event.
 *
 * Returns an empty array for every event AER has no business recording, which is
 * most of them: a session that ran twenty tool calls may have a hundred durable
 * events, and the ones that are not evidence are dropped by name (see
 * {@link DROPPED_EVENT_TYPES}).
 */
export declare function normalizeEvent(sessionId: string, event: SessionEvent): NormalizedEnvelope[];
/** `turn/end` into the turn's closing fact, or `null` when the event is not one. */
export declare function turnEndFrom(session: NormalizedSession, event: SessionEvent): NormalizedTurnEnd | null;
/**
 * The task text for the AER run opened by a turn.
 *
 * The user's prompt is taken from the durable `user/message` events that belong to
 * the turn, not from the CLI's argument: a turn's input may have been queued, edited
 * or produced by a relay, and the log is what actually entered the model's context.
 * Plugin-sourced messages are skipped -- AER's own injected context must never
 * become the description of the task (section 29's principle, one layer up).
 */
export declare function taskTextForTurn(sessionId: string, events: readonly SessionEvent[], turn: number): string;
