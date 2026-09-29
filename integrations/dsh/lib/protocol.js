/**
 * The bridge protocol: the only vocabulary the TypeScript plugin and the Python
 * bridge share.
 *
 * Why a protocol at all, rather than the plugin writing to AER directly: AER is
 * Python and DSH is Node. The boundary has to be explicit, and it has to be one
 * layer *below* `AgentExecutionEnvelope`, so the plugin can be tested without
 * Python and the Python side can be tested without DSH. Everything the plugin
 * learned from DSH crosses this boundary as JSON; nothing AER-specific is
 * decided on the TypeScript side.
 *
 * Framing is one JSON object per line in both directions (round-8.2 section 47).
 *
 * @module @aer/dsh-adapter/protocol
 */
/** Bumped when the shape of {@link BridgeRequest} / {@link BridgeResponse} changes. */
export const AER_BRIDGE_PROTOCOL_VERSION = 1;
