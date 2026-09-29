/**
 * The TypeScript-to-Python bridge, exercised across the process boundary.
 *
 * This is the test that cannot be written on either side alone: it starts the real
 * Python bridge as a child process, speaks the wire protocol to it from Node, and
 * asserts on the answers. A green run here means the two halves agree on framing,
 * versioning, operation names and response shape -- which is exactly the class of bug
 * that a per-side unit test cannot see.
 *
 * It also covers the two properties that decide whether a session degrades:
 *   * the bridge must answer a request before it reports itself ready (the warmup);
 *   * stdout must carry protocol frames only, so a warning on stderr cannot corrupt a
 *     response.
 *
 * @module @aer/dsh-adapter/tests/bridge
 */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { after, test } from 'node:test';
import { AER_BRIDGE_PROTOCOL_VERSION } from '../protocol.js';
const PYTHON = process.env['AER_DSH_PYTHON'] ?? 'python';
const CWD = process.env['AER_DSH_CWD'];
/**
 * Start the bridge and return a caller that correlates one response per request.
 *
 * Responses are matched by `request_id` rather than by arrival order: the protocol is
 * asynchronous in principle, and a test that assumed ordering would pass for the wrong
 * reason.
 */
async function startBridge() {
    const child = spawn(PYTHON, ['-m', 'aer.adapter.dsh.bridge'], {
        cwd: CWD ?? process.cwd(),
        stdio: ['pipe', 'pipe', 'pipe'],
    });
    child.stderr.setEncoding('utf8');
    child.stdout.setEncoding('utf8');
    let buffer = '';
    const waiting = new Map();
    const requests = [];
    const unmatched = [];
    let unmatchedWaiter = null;
    const deliverUnmatched = (response) => {
        if (unmatchedWaiter !== null) {
            const waiter = unmatchedWaiter;
            unmatchedWaiter = null;
            waiter(response);
            return;
        }
        unmatched.push(response);
    };
    child.stdout.on('data', (chunk) => {
        buffer += chunk;
        let newline = buffer.indexOf('\n');
        while (newline >= 0) {
            const line = buffer.slice(0, newline).trim();
            buffer = buffer.slice(newline + 1);
            newline = buffer.indexOf('\n');
            if (line === '') {
                continue;
            }
            // A line that is not JSON is a framing violation, and it must fail loudly
            // rather than be skipped: the plugin would time that request out and count
            // the bridge as degraded.
            const parsed = JSON.parse(line);
            const resolve = waiting.get(parsed.request_id);
            if (resolve !== undefined) {
                waiting.delete(parsed.request_id);
                resolve(parsed);
                continue;
            }
            deliverUnmatched(parsed);
        }
    });
    const bridge = {
        child,
        requests,
        async call(operation, payload = {}) {
            const requestId = `${operation}-${requests.length}`;
            requests.push(requestId);
            const line = `${JSON.stringify({
                protocol: AER_BRIDGE_PROTOCOL_VERSION,
                request_id: requestId,
                operation,
                payload,
            })}\n`;
            const settled = new Promise((resolve, reject) => {
                const timer = setTimeout(() => {
                    reject(new Error(`${operation} timed out`));
                }, 20_000);
                waiting.set(requestId, (response) => {
                    clearTimeout(timer);
                    resolve(response);
                });
            });
            child.stdin.write(line);
            return await settled;
        },
        async nextUnmatched() {
            const already = unmatched.shift();
            if (already !== undefined) {
                return already;
            }
            return await new Promise((resolve, reject) => {
                const timer = setTimeout(() => reject(new Error('no unmatched response arrived')), 20_000);
                unmatchedWaiter = (response) => {
                    clearTimeout(timer);
                    resolve(response);
                };
            });
        },
    };
    // The first answer may pay for interpreter startup; wait for it here so that no
    // later assertion is measuring the wrong thing.
    const first = await bridge.call('ping');
    assert.equal(first.ok, true);
    return bridge;
}
let shared = null;
async function bridge() {
    if (shared === null) {
        shared = await startBridge();
    }
    return shared;
}
after(() => {
    shared?.child.kill();
    shared = null;
});
test('the bridge answers the protocol version it speaks', async () => {
    const client = await bridge();
    const response = await client.call('ping');
    assert.equal(response.ok, true);
    assert.equal(response.protocol, AER_BRIDGE_PROTOCOL_VERSION);
});
test('every operation the plugin sends is understood', async () => {
    const client = await bridge();
    const session = { external_session_id: 'bridge-test-session', cwd: null };
    assert.equal((await client.call('session.begin', { session })).ok, true);
    assert.equal((await client.call('turn.begin', { session, turn: 1, task: 'a task' })).ok, true);
    assert.equal((await client.call('event.ingest', {
        envelope: {
            event_type: 'TOOL_CALL',
            external_event_id: 'bridge-test-session:3',
            external_session_id: 'bridge-test-session',
            external_turn_id: '1',
            external_sequence: 3,
            payload: { tool: 'bash', call_id: 'c1', arguments: '{}' },
            metadata: {},
        },
    })).ok, true);
    assert.equal((await client.call('turn.end', { session, turn: 1, reason_kind: 'completed' })).ok, true);
});
test('a retrieval with nothing to offer is an empty answer, not an error', async () => {
    const client = await bridge();
    const response = await client.call('experience.retrieve', {
        session: { external_session_id: 'bridge-test-session' },
        turn: 1,
        task: 'a task',
    });
    assert.equal(response.ok, true);
    const result = response.result;
    assert.equal(result.experience_count, 0);
    assert.equal(result.context_text, '');
});
test('an unknown operation is refused rather than absorbed', async () => {
    const client = await bridge();
    // Deliberately outside the wire vocabulary: a bridge that guessed at a request it
    // did not understand would write evidence nobody could trace to a source. The
    // refusal cannot be correlated to a request id -- an unparseable line has no
    // trustworthy one -- which is the documented behaviour, not an oversight.
    void client.call('teleport').catch(() => undefined);
    const response = await client.nextUnmatched();
    assert.equal(response.ok, false);
    assert.equal(response.request_id, 'unknown');
    assert.ok(response.error);
});
