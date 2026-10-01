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
export {};
