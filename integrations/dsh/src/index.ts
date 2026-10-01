/**
 * The AER adapter as a native DSH plugin.
 *
 * It uses exactly two extension points, and the split between them is the milestone's
 * architecture in one sentence: **durable events say what happened, the live pre-step
 * hook decides what the model sees next.**
 *
 * ```
 * ctx.on('session/event')   post-commit append feed  -> every execution fact, once
 * ctx.on('agent/pre-step')  waterfall               -> retrieval + context injection
 * ```
 *
 * Neither is a compatibility bridge: DSH's own typed pipeline is the data source, and
 * AER is a first-class participant in the step rather than a shell command observing
 * one (sections 3, 6).
 *
 * Three deliberate omissions:
 *
 * * **No `tools/pre-execute` / `tools/post-execute` / `tools/result` subscription.**
 *   Those carry the same tool facts as the durable log, and mapping both would write
 *   every call twice (section 9). The durable log is the authority.
 * * **No `session/created`-time cold read on the hot path.** Catch-up exists
 *   ({@link replay}) but runs only when a cursor says something is missing.
 * * **No verdicts.** The plugin reports what DSH said and asks AER what to do. It
 *   never decides that a task succeeded.
 *
 * @module @aer/dsh-adapter
 */

import type { Context } from '@deepseek-ai/cordis';
// Type-only, and load-bearing: importing the agent package is what pulls in its
// `declare module '@deepseek-ai/cordis'` augmentation, so `agent/pre-step` and the
// `agents` service exist for the compiler. Without it the extension point this plugin
// is built on is not even nameable.
import type { Agent } from '@deepseek-ai/dsh-agent';
import { createUserMessage } from '@deepseek-ai/dsh-llm';
import { SessionLogOffset, type Session, type SessionEvent } from '@deepseek-ai/dsh-session';

import { Bridge } from './bridge.js';
import {
  modelFromHeader,
  normalizeEvent,
  sessionFromHeader,
  taskTextForTurn,
  turnEndFrom,
  turnOf,
} from './mapping.js';
import type { NormalizedModel, NormalizedSession, RetrievalOffer } from './protocol.js';

/** Plugin name. Also the `source.plugin` value on every injected message. */
export const name = 'aer-dsh-adapter';

/**
 * The cursor value meaning "nothing consumed yet".
 *
 * DSH's `SessionSeq` is zero-based, so `0` is a real position and cannot also mean
 * "empty". `SessionSeqCursor` defines exactly this value as `-1`.
 */
const NO_EVENTS_YET = -1;

/**
 * The services this plugin needs before it is applied.
 *
 * `agents` because the pre-step waterfall is dispatched in agent scope, `sessions`
 * because the durable feed is dispatched in session scope.
 */
export const inject = ['agents', 'sessions'];

export interface Config {
  /** Python executable. */
  readonly python?: string;
  /** Arguments for the bridge, defaulting to `-m aer.adapter.dsh.bridge`. */
  readonly args?: readonly string[];
  /** Working directory for the bridge: where AER's data directory is resolved from. */
  readonly cwd?: string;
  readonly env?: Record<string, string>;
  /** Budget for the retrieval round trip. It is on the model's path (section 52). */
  readonly retrievalTimeoutMs?: number;
  /** Set false to load the plugin without a bridge, for a pure wiring probe. */
  readonly enabled?: boolean;
}

interface SessionState {
  readonly identity: NormalizedSession;
  /**
   * Highest `seq` handed to the bridge, or `-1` before any (section 38).
   *
   * `-1` rather than `0`: DSH's `SessionSeq` is zero-based, so a cursor starting at
   * `0` would read "seq 0 is already consumed" and drop the session's first event
   * before the bridge ever saw it. DSH's own `SessionSeqCursor` uses the same `-1`
   * for "no event yet", so this is the harness's convention rather than ours.
   */
  cursor: number;
  /** Runs opened per turn, so a `turn/end` closes the right one. */
  readonly turns: Set<number>;
  model: NormalizedModel | null;
  /**
   * Serialises this session's deliveries.
   *
   * The feed is synchronous but the work is not, and the bridge must see events in
   * `seq` order for a turn's run to exist before its tool calls arrive. Chaining one
   * promise per session is the whole of the ordering guarantee; firing an independent
   * task per event would let seq 19 overtake seq 18 whenever the first call was slower.
   */
  chain: Promise<void>;
}

/** A context offer that has been returned to DSH but not yet seen in the log. */
interface PendingInjection {
  readonly retrieval_id: string;
  readonly session_id: string;
  readonly turn: number;
  /** Hash of the exact text, used to recognise the durable message that proves it landed. */
  readonly digest: string;
  readonly experiences: number;
}

/**
 * A cheap, stable digest of the injected text.
 *
 * Not a cryptographic hash: this only has to distinguish two injections within one
 * session, and it must be computed identically on both sides of the offer.
 */
function digestOf(text: string): string {
  let accumulator = 0x811c9dc5;
  for (let index = 0; index < text.length; index += 1) {
    accumulator ^= text.charCodeAt(index);
    accumulator = Math.imul(accumulator, 0x01000193) >>> 0;
  }
  return accumulator.toString(16).padStart(8, '0');
}

export function apply(ctx: Context, config: Config = {}): void {
  const log = (message: string): void => {
    // The plugin's own diagnostics go to stderr; DSH surfaces it with the rest.
    process.stderr.write(`[aer-dsh] ${message}\n`);
  };

  log(`applied; enabled=${String(config.enabled !== false)} python=${String(config.python)}`);
  const bridge =
    config.enabled === false
      ? null
      : new Bridge({
          command: config.python ?? process.env['AER_DSH_PYTHON'] ?? 'python',
          args: [...(config.args ?? ['-m', 'aer.adapter.dsh.bridge'])],
          ...(config.cwd === undefined ? {} : { cwd: config.cwd }),
          env: { ...(config.env ?? {}) },
          ...(config.retrievalTimeoutMs === undefined
            ? {}
            : { retrievalTimeoutMs: config.retrievalTimeoutMs }),
          log,
        });

  bridge?.start();

  const sessions = new Map<string, SessionState>();
  const pendingInjections: PendingInjection[] = [];

  ctx.effect(() => () => {
    void bridge?.dispose();
  }, 'aer-dsh-adapter.dispose()');

  // -- durable execution facts ------------------------------------------------

  function stateFor(session: Session): SessionState {
    const key = String(session.id);
    const existing = sessions.get(key);
    if (existing !== undefined) {
      return existing;
    }
    const identity = sessionFromHeader(session.header);
    const created: SessionState = {
      identity,
      cursor: NO_EVENTS_YET,
      turns: new Set(),
      model: null,
      chain: Promise.resolve(),
    };
    sessions.set(key, created);
    return created;
  }

  async function beginSession(state: SessionState): Promise<void> {
    await bridge?.request('session.begin', {
      session: state.identity,
      replay_hint: bridge.replayHint,
    });
  }

  /**
   * Ingest a range of durable events, in seq order, advancing the cursor.
   *
   * Called with one event from the live feed and with a whole range from catch-up, so
   * the ordering rule exists once: the bridge must see events in order for a turn's
   * run to exist before its tool calls arrive.
   */
  async function ingest(session: Session, state: SessionState, events: readonly SessionEvent[]): Promise<void> {
    for (const event of events) {
      const seq = typeof event.seq === 'number' ? event.seq : 0;
      if (seq <= state.cursor) {
        continue;
      }
      const turn = turnOf(event);
      switch (event.type) {
        case 'turn/start': {
          if (turn === null) {
            break;
          }
          state.turns.add(turn);
          await bridge?.request('turn.begin', {
            session: state.identity,
            turn,
            task: taskTextForTurn(String(session.id), session.snapshotEvents(), turn),
            model: state.model,
          });
          break;
        }
        case 'turn/end': {
          const ended = turnEndFrom(state.identity, event);
          if (ended === null) {
            break;
          }
          state.turns.delete(ended.turn);
          await bridge?.request('turn.end', {
            session: ended.session,
            turn: ended.turn,
            reason_kind: ended.reason_kind,
            outcome: ended.outcome,
            detail: ended.detail,
            seq,
          });
          break;
        }
        case 'request/header': {
          const model = modelFromHeader(event);
          if (model !== null) {
            state.model = model;
          }
          break;
        }
        default: {
          for (const envelope of normalizeEvent(String(session.id), event)) {
            bridge?.notify('event.ingest', { envelope });
          }
          break;
        }
      }
      state.cursor = seq;
      confirmInjection(session, state, event, seq);
    }
  }

  /**
   * Recognise an injected context message in the durable log.
   *
   * This is the injection *proof* (section 70): DSH commits the message we returned
   * from the pre-step waterfall as an ordinary `user/message` event, so the log itself
   * is the evidence that AER's context entered the model's request. Recording the
   * injection here rather than at the point of return is what makes "retrieved but not
   * injected" a distinguishable state (section 29).
   */
  function confirmInjection(
    session: Session,
    state: SessionState,
    event: SessionEvent,
    seq: number,
  ): void {
    if (event.type !== 'user/message' || bridge === null) {
      return;
    }
    const message = event.data as unknown as Record<string, unknown>;
    const source = message['source'] as Record<string, unknown> | undefined;
    if (source?.['kind'] !== 'plugin' || source['plugin'] !== name) {
      return;
    }
    const index = pendingInjections.findIndex(
      (candidate) =>
        candidate.session_id === String(session.id) &&
        candidate.digest === digestOf(textOf(message['content'])),
    );
    if (index < 0) {
      return;
    }
    const [offer] = pendingInjections.splice(index, 1);
    if (offer === undefined) {
      return;
    }
    bridge.notify('experience.injected', {
      session: state.identity,
      turn: offer.turn,
      retrieval_id: offer.retrieval_id,
      experience_count: offer.experiences,
      // The seq of the durable message that carries the injected context. This is the
      // fact a reader checks to confirm the context really reached the step.
      seq,
    });
  }

  ctx.on('session/event', (session: Session, event: SessionEvent) => {
    const state = stateFor(session);
    state.chain = state.chain.then(async () => {
      try {
        if (state.cursor === NO_EVENTS_YET) {
          await beginSession(state);
        }
        await ingest(session, state, [event]);
      } catch (error) {
        // An AER failure is never the agent's failure. The durable log still holds the
        // event, the cursor still points at the last one AER confirmed, and the next
        // delivery or the catch-up path can close the gap (sections 49, 75).
        log(`ingest failed at seq ${String(event.seq)}: ${String(error)}`);
      }
    });
  });

  /**
   * Re-read the durable log from the cursor and ingest whatever AER has not seen.
   *
   * This is provider-owned replay (section 39): the events come back from DSH, not
   * from a copy AER kept, so nothing needs to be stored twice and no transcript is
   * duplicated into SQLite (section 40).
   */
  async function replay(session: Session): Promise<number> {
    const state = stateFor(session);
    if (state.cursor === NO_EVENTS_YET) {
      await beginSession(state);
    }
    // `snapshotEvents` takes a branded *offset*, which must be non-negative: `-1` is
    // a cursor value, not a log position, so it becomes "read from the start".
    const missing = session.snapshotEvents(SessionLogOffset(Math.max(0, state.cursor)));
    await ingest(session, state, missing);
    return missing.length;
  }

  // -- the model-facing step --------------------------------------------------

  ctx.on('agent/pre-step', async (payload, next) => {
    const decision = await next();
    if (decision.kind === 'reject' || payload.signal.aborted || bridge === null) {
      return decision;
    }
    const session = sessionOf(payload.agent);
    const state = stateFor(session);
    let offer: RetrievalOffer | null = null;
    try {
      offer = (await bridge.request(
        'experience.retrieve',
        {
          session: state.identity,
          turn: payload.turn,
          step: payload.step,
          task: taskTextForTurn(String(session.id), session.snapshotEvents(), payload.turn),
        },
        config.retrievalTimeoutMs,
      )) as RetrievalOffer | null;
    } catch (error) {
      // Retrieval is on the model's path, so a slow or broken AER must not break the
      // turn: the agent proceeds without experience and AER records that retrieval was
      // unavailable (section 51).
      log(`retrieval unavailable for turn ${String(payload.turn)}: ${String(error)}`);
      return decision;
    }
    if (offer === null || offer.context_text.trim() === '') {
      return decision;
    }

    const text = offer.context_text;
    pendingInjections.push({
      retrieval_id: offer.retrieval_id,
      session_id: String(session.id),
      turn: payload.turn,
      digest: digestOf(text),
      experiences: offer.experience_count,
    });

    return {
      ...decision,
      messages: [
        ...decision.messages,
        createUserMessage({
          content: [{ type: 'text', text }],
          source: {
            kind: 'plugin',
            plugin: name,
            // `recall` is DSH's own name for "material lifted out of another session's
            // log" -- which is exactly what an AER experience is. Deliberately not
            // `instructions`: experience is historical evidence, and DSH reserves that
            // form for content the model is expected to *follow* (sections 27, 28).
            form: 'recall',
          },
        }),
      ],
    };
  });
}

/** The session an agent is bound to. Named so the `dsh-agent` types stay referenced. */
function sessionOf(agent: Agent): Session {
  return agent.session;
}

/** The plain text of a message's content blocks. Used only for the injection digest. */
function textOf(content: unknown): string {
  if (!Array.isArray(content)) {
    return '';
  }
  const parts: string[] = [];
  for (const block of content) {
    const record = block as Record<string, unknown>;
    if (record['type'] === 'text' && typeof record['text'] === 'string') {
      parts.push(record['text']);
    }
  }
  return parts.join('\n');
}
