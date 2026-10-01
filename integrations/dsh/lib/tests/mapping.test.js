/**
 * Contract tests for the DSH-to-AER mapping.
 *
 * These are *contract* tests, not captured ones: the events are built from DSH's own
 * published types rather than taken from a real provider run. That is legitimate here
 * because what is being tested is the mapping's logic, and DSH's type declarations are
 * the contract. Whether DSH really emits these shapes is the captured fixtures' job.
 *
 * @module @aer/dsh-adapter/tests/mapping
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { DROPPED_EVENT_TYPES, normalizeEvent, taskTextForTurn, toolCallFact, toolResultFact, turnOutcome, } from '../mapping.js';
/** Build a durable event without pretending to own DSH's branded constructors. */
function event(type, seq, data) {
    return { type, seq, time: 1_700_000_000_000, data };
}
function toolResult(seq, isError, error) {
    return event('tool/result', seq, {
        turn: 1,
        step: 0,
        message: {
            role: 'user',
            content: [{ type: 'tool-result', toolCallId: 'call-1', content: [], isError }],
            source: { kind: 'tool', callId: 'call-1' },
        },
        ...(error === undefined ? {} : { error }),
    });
}
test('a completed turn claims nothing about the work', () => {
    assert.equal(turnOutcome('completed'), 'INCONCLUSIVE');
});
test('a structured error is the one outcome DSH declares as failure', () => {
    assert.equal(turnOutcome('error'), 'FAILED');
});
test('a cancellation is an abandonment, and says so', () => {
    assert.equal(turnOutcome('aborted'), 'ABORTED');
});
test('truncation and blocking are not partial success', () => {
    assert.equal(turnOutcome('max-tokens'), 'INCONCLUSIVE');
    assert.equal(turnOutcome('blocked'), 'INCONCLUSIVE');
    assert.equal(turnOutcome('interrupted'), 'INCONCLUSIVE');
});
test('a reason this adapter has never seen closes without claiming anything', () => {
    // TurnEndReasonMap is merge-extensible, so this is a real future, not a thought
    // experiment: refusing to close would leave the run RUNNING forever.
    assert.equal(turnOutcome('quota-exhausted'), 'INCONCLUSIVE');
});
test('a tool result carries its outcome structurally', () => {
    const fact = toolResultFact(toolResult(4, true, { name: 'ToolError', code: 'UNKNOWN_TOOL' }));
    assert.ok(fact);
    assert.equal(fact?.is_error, true);
    assert.equal(fact?.error_name, 'ToolError');
    assert.equal(fact?.error_code, 'UNKNOWN_TOOL');
});
test('an absent isError is not a failure', () => {
    // DSH documents the flag as the *error* marker, so its absence means "no error was
    // published" rather than "the outcome is unknown".
    const fact = toolResultFact(toolResult(4, undefined));
    assert.equal(fact?.is_error, false);
});
test('a tool call keeps the arguments exactly as the model produced them', () => {
    const fact = toolCallFact(event('tool/call', 3, {
        turn: 1,
        step: 0,
        callId: 'call-1',
        name: 'bash',
        arguments: '{"cmd":"echo hi"}',
    }));
    assert.equal(fact?.name, 'bash');
    assert.equal(fact?.arguments_json, '{"cmd":"echo hi"}');
});
test('a tool call without a usable id is dropped rather than guessed at', () => {
    assert.equal(toolCallFact(event('tool/call', 3, { turn: 1, step: 0 })), null);
});
test('a failed tool result travels inside the result, not as a second event', () => {
    const envelopes = normalizeEvent('session-1', toolResult(4, true, { name: 'E', code: 'C' }));
    assert.equal(envelopes.length, 1);
    assert.equal(envelopes[0]?.event_type, 'TOOL_RESULT');
    assert.equal(envelopes[0]?.payload['success'], false);
    assert.ok(envelopes[0]?.payload['error']);
});
test('the external event id is the session plus the durable seq', () => {
    // SessionSeq is monotonic and contiguous, which is what makes this a stable identity.
    const envelopes = normalizeEvent('session-1', event('tool/call', 7, { turn: 1, step: 0, callId: 'c', name: 'bash', arguments: '{}' }));
    assert.equal(envelopes[0]?.external_event_id, 'session-1:7');
    assert.equal(envelopes[0]?.external_sequence, 7);
});
test('AER-injected context never becomes the description of the task', () => {
    const events = [
        event('user/message', 1, {
            turn: 1,
            role: 'user',
            source: { kind: 'user' },
            content: [{ type: 'text', text: 'run the tests' }],
        }),
        event('user/message', 2, {
            turn: 1,
            role: 'user',
            source: { kind: 'plugin', plugin: 'aer-dsh-adapter', form: 'recall' },
            content: [{ type: 'text', text: 'AER experience context' }],
        }),
    ];
    assert.equal(taskTextForTurn('session-1', events, 1), 'run the tests');
});
test('events AER has no business recording are dropped by name, with a reason', () => {
    for (const type of DROPPED_EVENT_TYPES.keys()) {
        assert.ok(DROPPED_EVENT_TYPES.get(type), `${type} is dropped without a stated reason`);
    }
    // The model's private reasoning is the one that matters most: it is dropped rather
    // than stored and filtered later.
    assert.match(DROPPED_EVENT_TYPES.get('assistant/attempt') ?? '', /reasoning/);
});
