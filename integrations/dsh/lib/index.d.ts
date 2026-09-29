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
/** Plugin name. Also the `source.plugin` value on every injected message. */
export declare const name = "aer-dsh-adapter";
/**
 * The services this plugin needs before it is applied.
 *
 * `agents` because the pre-step waterfall is dispatched in agent scope, `sessions`
 * because the durable feed is dispatched in session scope.
 */
export declare const inject: string[];
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
export declare function apply(ctx: Context, config?: Config): void;
