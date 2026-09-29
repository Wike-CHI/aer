/**
 * The long-lived Python bridge.
 *
 * One child process per plugin instance, not one per event (round-8.2 section 48): a
 * tool-heavy turn can produce a hundred durable events, and a process spawn each
 * would cost more than the work. Communication is one JSON object per line over
 * stdin/stdout.
 *
 * Two failure paths are designed rather than discovered, because both are on the
 * *agent's* critical path:
 *
 * * **A crashed bridge must not crash DSH** (section 49). The plugin reports itself
 *   degraded, keeps serving the agent, and restarts the child a bounded number of
 *   times. The durable log is the safety net -- see {@link Bridge.replayHint} -- so a
 *   gap caused by a restart is recoverable, while an agent that dies because its
 *   telemetry sink died is not.
 * * **The queue is bounded** (section 50). Telemetry is `fail-open`: if the bridge is
 *   behind, events are dropped with a counter rather than accumulated until the
 *   agent runs out of memory.
 */
import type { BridgeOperation } from './protocol.js';
/** What the plugin needs to know about the bridge's health. */
export interface BridgeHealth {
    readonly state: 'starting' | 'ready' | 'degraded' | 'disposed';
    /** Why it is degraded, for the operator's log. `null` while healthy. */
    readonly reason: string | null;
    readonly sent: number;
    readonly failed: number;
    /** Events dropped because the outbound queue was full. Non-zero is a real problem. */
    readonly dropped: number;
    readonly restarts: number;
}
export interface BridgeOptions {
    /** Executable to run. Defaults to the `python` on `PATH`. */
    readonly command: string;
    readonly args: readonly string[];
    readonly cwd?: string;
    readonly env?: Record<string, string>;
    /** Timeout for ordinary calls. Generous: none of them are on the model's path. */
    readonly requestTimeoutMs?: number;
    /** Timeout for the retrieval call, which *is* on the model's path (section 52). */
    readonly retrievalTimeoutMs?: number;
    /** How many fire-and-forget events may be in flight before events start being dropped. */
    readonly maxInFlight?: number;
    /** How many times the child may be restarted before the bridge stays degraded. */
    readonly maxRestarts?: number;
    /** Receives one line per lifecycle change, for the operator. */
    readonly log?: (message: string) => void;
}
/** Raised when the bridge cannot answer. Never raised to the agent's own control flow. */
export declare class BridgeUnavailableError extends Error {
    constructor(message: string);
}
export declare class Bridge {
    private readonly options;
    private child;
    private readonly pending;
    private stdoutBuffer;
    private stderrTail;
    private inFlight;
    private restarts;
    private sent;
    private failed;
    private dropped;
    private disposed;
    private state;
    private reason;
    /**
     * Settles when the child has answered its first request, or `null` before spawn.
     *
     * Spawning the process is not the same as it being able to answer: the Python side
     * imports AER before it reads a line, and that takes about 1.5 s on a warm
     * interpreter (measured, `DEFAULT_RETRIEVAL_TIMEOUT_MS` below). The first real
     * request therefore used to race the interpreter's startup and lose, which is how
     * the very first retrieval of every session timed out and silently degraded the
     * whole session. Waiting here costs nothing that is not already being paid.
     */
    private ready;
    constructor(options: BridgeOptions);
    /** Start the child. Safe to call twice; the second call is a no-op. */
    start(): void;
    /**
     * Exchange one request with the child purely to learn that it is serving.
     *
     * The timeout here is the generous one, because it is paying for interpreter
     * startup rather than for work. Keeping the *retrieval* timeout short stays
     * correct only because the startup cost has already been paid by this call.
     */
    private warmup;
    /** Wait for the child's first answer, when one is in flight. */
    private ensureReady;
    get health(): BridgeHealth;
    /**
     * A hint for the operator about what a gap means.
     *
     * The bridge does not replay anything itself, and it does not pretend to: DSH's own
     * durable log is the replay source (section 39), and until the adapter reconciles
     * from it a dropped event is *lost and visible* rather than silently duplicated.
     */
    get replayHint(): string;
    /** Send one request and wait for its answer. */
    request(operation: BridgeOperation, payload: Record<string, unknown>, timeoutMs?: number): Promise<unknown>;
    /**
     * The transport half of {@link request}, without the readiness wait.
     *
     * Split out so {@link warmup} can use it: waiting for readiness inside the warmup
     * request would be waiting for the request that is doing the waiting.
     */
    private send;
    /**
     * Send one request without waiting for the answer.
     *
     * Used for execution facts, which are *already durable in DSH* before they reach
     * here. Losing one costs a gap the replay can close; blocking the agent on a
     * telemetry ack costs latency on every tool call. When the queue is full the event
     * is dropped and counted rather than queued without bound (section 50).
     */
    notify(operation: BridgeOperation, payload: Record<string, unknown>): void;
    /** Flush and stop. Resolves once the child is gone. */
    dispose(timeoutMs?: number): Promise<void>;
    private consumeStdout;
    private deliver;
    private consumeStderr;
    private onExit;
}
