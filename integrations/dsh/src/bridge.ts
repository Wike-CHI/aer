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

import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';

import type { BridgeOperation, BridgeResponse } from './protocol.js';
import { AER_BRIDGE_PROTOCOL_VERSION } from './protocol.js';

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

const DEFAULT_REQUEST_TIMEOUT_MS = 15_000;
const DEFAULT_RETRIEVAL_TIMEOUT_MS = 2_000;
const DEFAULT_MAX_IN_FLIGHT = 512;
const DEFAULT_MAX_RESTARTS = 3;

interface Pending {
  readonly resolve: (value: unknown) => void;
  readonly reject: (error: Error) => void;
  readonly timer: NodeJS.Timeout;
}

/** Raised when the bridge cannot answer. Never raised to the agent's own control flow. */
export class BridgeUnavailableError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'BridgeUnavailableError';
  }
}

let requestCounter = 0;

function nextRequestId(): string {
  requestCounter += 1;
  return `${process.pid}-${Date.now().toString(36)}-${requestCounter}`;
}

export class Bridge {
  private child: ChildProcessWithoutNullStreams | null = null;
  private readonly pending = new Map<string, Pending>();
  private stdoutBuffer = '';
  private stderrTail: string[] = [];
  private inFlight = 0;
  private restarts = 0;
  private sent = 0;
  private failed = 0;
  private dropped = 0;
  private disposed = false;
  private state: BridgeHealth['state'] = 'starting';
  private reason: string | null = null;
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
  private ready: Promise<void> | null = null;

  constructor(private readonly options: BridgeOptions) {}

  /** Start the child. Safe to call twice; the second call is a no-op. */
  start(): void {
    if (this.child !== null || this.disposed) {
      return;
    }
    this.state = 'starting';
    this.reason = null;
    const child = spawn(this.options.command, [...this.options.args], {
      cwd: this.options.cwd ?? process.cwd(),
      env: { ...process.env, ...(this.options.env ?? {}) },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (chunk: string) => this.consumeStdout(chunk));
    child.stderr.on('data', (chunk: string) => this.consumeStderr(chunk));
    child.on('error', (error: Error) => this.onExit(`spawn failed: ${error.message}`));
    child.on('exit', (code, signal) =>
      this.onExit(`exited code=${String(code)} signal=${String(signal)}`),
    );
    this.child = child;
    // Deliberately *not* `ready` yet: the interpreter exists but has not answered
    // anything. The first answer is what proves the bridge can serve.
    this.state = 'starting';
    this.ready = this.warmup();
  }

  /**
   * Exchange one request with the child purely to learn that it is serving.
   *
   * The timeout here is the generous one, because it is paying for interpreter
   * startup rather than for work. Keeping the *retrieval* timeout short stays
   * correct only because the startup cost has already been paid by this call.
   */
  private async warmup(): Promise<void> {
    try {
      await this.send('ping', {}, this.options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS);
      if (this.state === 'starting') {
        this.state = 'ready';
      }
    } catch (error) {
      // A bridge that never answers is degraded, not fatal: the agent keeps
      // running and the durable log remains the replay source (section 49).
      this.reason = `warmup failed: ${String(error)}`;
      this.state = 'degraded';
    }
  }

  /** Wait for the child's first answer, when one is in flight. */
  private async ensureReady(): Promise<void> {
    if (this.ready !== null) {
      await this.ready;
    }
  }

  get health(): BridgeHealth {
    return {
      state: this.state,
      reason: this.reason,
      sent: this.sent,
      failed: this.failed,
      dropped: this.dropped,
      restarts: this.restarts,
    };
  }

  /**
   * A hint for the operator about what a gap means.
   *
   * The bridge does not replay anything itself, and it does not pretend to: DSH's own
   * durable log is the replay source (section 39), and until the adapter reconciles
   * from it a dropped event is *lost and visible* rather than silently duplicated.
   */
  get replayHint(): string {
    return 'DSH durable session log (readColdSessionLog) is the replay source; AER holds a per-session seq cursor.';
  }

  /** Send one request and wait for its answer. */
  async request(
    operation: BridgeOperation,
    payload: Record<string, unknown>,
    timeoutMs?: number,
  ): Promise<unknown> {
    this.start();
    await this.ensureReady();
    return await this.send(operation, payload, timeoutMs);
  }

  /**
   * The transport half of {@link request}, without the readiness wait.
   *
   * Split out so {@link warmup} can use it: waiting for readiness inside the warmup
   * request would be waiting for the request that is doing the waiting.
   */
  private async send(
    operation: BridgeOperation,
    payload: Record<string, unknown>,
    timeoutMs?: number,
  ): Promise<unknown> {
    const child = this.child;
    if (child === null) {
      this.failed += 1;
      throw new BridgeUnavailableError('bridge is not running');
    }
    const requestId = nextRequestId();
    const budget =
      timeoutMs ??
      (operation === 'experience.retrieve'
        ? (this.options.retrievalTimeoutMs ?? DEFAULT_RETRIEVAL_TIMEOUT_MS)
        : (this.options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS));

    const line = `${JSON.stringify({
      protocol: AER_BRIDGE_PROTOCOL_VERSION,
      request_id: requestId,
      operation,
      payload,
    })}\n`;

    return await new Promise<unknown>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(requestId);
        this.failed += 1;
        reject(new BridgeUnavailableError(`${operation} timed out after ${budget}ms`));
      }, budget);
      // Do not hold the event loop open for a telemetry timeout.
      timer.unref?.();
      this.pending.set(requestId, { resolve, reject, timer });
      this.sent += 1;
      child.stdin.write(line, (error) => {
        if (error) {
          this.pending.delete(requestId);
          clearTimeout(timer);
          this.failed += 1;
          reject(new BridgeUnavailableError(`write failed: ${error.message}`));
        }
      });
    });
  }

  /**
   * Send one request without waiting for the answer.
   *
   * Used for execution facts, which are *already durable in DSH* before they reach
   * here. Losing one costs a gap the replay can close; blocking the agent on a
   * telemetry ack costs latency on every tool call. When the queue is full the event
   * is dropped and counted rather than queued without bound (section 50).
   */
  notify(operation: BridgeOperation, payload: Record<string, unknown>): void {
    if (this.inFlight >= (this.options.maxInFlight ?? DEFAULT_MAX_IN_FLIGHT)) {
      this.dropped += 1;
      return;
    }
    this.inFlight += 1;
    void this.request(operation, payload)
      .catch(() => {
        /* the failure is already counted in `failed` */
      })
      .finally(() => {
        this.inFlight -= 1;
      });
  }

  /** Flush and stop. Resolves once the child is gone. */
  async dispose(timeoutMs = 2_000): Promise<void> {
    this.disposed = true;
    const child = this.child;
    this.child = null;
    for (const [id, pending] of this.pending) {
      clearTimeout(pending.timer);
      this.pending.delete(id);
      pending.reject(new BridgeUnavailableError('bridge is shutting down'));
    }
    if (child === null) {
      this.state = 'disposed';
      return;
    }
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        child.kill('SIGKILL');
        resolve();
      }, timeoutMs);
      timer.unref?.();
      child.once('exit', () => {
        clearTimeout(timer);
        resolve();
      });
      try {
        child.stdin.write(
          `${JSON.stringify({
            protocol: AER_BRIDGE_PROTOCOL_VERSION,
            request_id: nextRequestId(),
            operation: 'shutdown',
            payload: {},
          })}\n`,
        );
        child.stdin.end();
      } catch {
        child.kill();
      }
    });
    this.state = 'disposed';
  }

  private consumeStdout(chunk: string): void {
    this.stdoutBuffer += chunk;
    for (;;) {
      const newline = this.stdoutBuffer.indexOf('\n');
      if (newline < 0) {
        return;
      }
      const line = this.stdoutBuffer.slice(0, newline);
      this.stdoutBuffer = this.stdoutBuffer.slice(newline + 1);
      if (line.trim() === '') {
        continue;
      }
      this.deliver(line);
    }
  }

  private deliver(line: string): void {
    let response: BridgeResponse;
    try {
      response = JSON.parse(line) as BridgeResponse;
    } catch {
      // A stray line on stdout is a bug in the bridge, not in DSH. Logging it beats
      // killing the agent's turn over it.
      this.options.log?.(`aer-dsh: unparsable bridge line: ${line.slice(0, 200)}`);
      return;
    }
    const pending = this.pending.get(response.request_id);
    if (pending === undefined) {
      return;
    }
    this.pending.delete(response.request_id);
    clearTimeout(pending.timer);
    if (response.ok) {
      pending.resolve(response.result);
      return;
    }
    this.failed += 1;
    pending.reject(
      new BridgeUnavailableError(
        `${response.error?.code ?? 'BRIDGE_ERROR'}: ${response.error?.message ?? 'no message'}`,
      ),
    );
  }

  private consumeStderr(chunk: string): void {
    // Keep a bounded tail for diagnosis: the bridge's traceback is the only clue
    // when a call fails, and an unbounded buffer would be its own outage.
    for (const line of chunk.split('\n')) {
      if (line.trim() === '') {
        continue;
      }
      this.stderrTail.push(line);
      this.options.log?.(`aer-dsh bridge: ${line}`);
    }
    if (this.stderrTail.length > 200) {
      this.stderrTail = this.stderrTail.slice(-200);
    }
  }

  private onExit(reason: string): void {
    if (this.child === null && this.state === 'disposed') {
      return;
    }
    this.child = null;
    for (const [id, pending] of this.pending) {
      clearTimeout(pending.timer);
      this.pending.delete(id);
      pending.reject(new BridgeUnavailableError(`bridge unavailable: ${reason}`));
    }
    if (this.disposed) {
      this.state = 'disposed';
      return;
    }
    const maxRestarts = this.options.maxRestarts ?? DEFAULT_MAX_RESTARTS;
    if (this.restarts >= maxRestarts) {
      // Bounded on purpose: an unbounded restart loop turns a broken bridge into a
      // spinning process that makes the agent slow instead of merely incomplete.
      this.state = 'degraded';
      this.reason = `${reason}; giving up after ${this.restarts} restart(s)`;
      this.options.log?.(`aer-dsh: ${this.reason}`);
      return;
    }
    this.restarts += 1;
    this.state = 'degraded';
    this.reason = `${reason}; restarting (${this.restarts}/${maxRestarts})`;
    this.options.log?.(`aer-dsh: ${this.reason}`);
    setTimeout(() => {
      if (!this.disposed) {
        this.start();
      }
    }, 250 * this.restarts).unref?.();
  }
}
