/**
 * Command translation: LF-JSONL frames off the control socket into extension
 * API calls, answered with pi-shaped response frames (REQ-09).
 *
 * The contract is v1's host socket. `status` is the host verb, answered
 * locally from disk state to the requesting client alone. The supported
 * pi-RPC commands are translated: prompt / steer / follow_up deliver through
 * sendUserMessage with the deliverAs mapping - a plain prompt against a
 * streaming agent is refused, the same preflight pi's own RPC mode applies
 * (REQ-10) - abort aborts the run, get_state and get_last_assistant_text
 * read session state. Their responses are published: broadcast to every
 * client and mirrored into events.jsonl, exactly where pi's response frames
 * land in v1's pumped stream. Anything else is rejected explicitly, naming
 * the type, and the connection stays usable (REQ-11).
 *
 * Pure translation: no socket, no filesystem - the host interface carries
 * every effect, so the whole surface is testable without a connection.
 */

export interface CommandHost {
  /** Whether the agent is idle (not streaming). */
  isIdle(): boolean;
  /** Abort the current run. */
  abort(): void;
  /** Deliver a user message; always triggers a turn. */
  sendUserMessage(content: unknown, options?: { deliverAs: "steer" | "followUp" }): void;
  /** The v1 `status` host-verb payload: {status, paths}. */
  statusData(): unknown;
  /** The get_state snapshot. */
  state(): Record<string, unknown>;
  /** The last final assistant text, pi's getLastAssistantText semantics. */
  lastAssistantText(): string | undefined;
  /**
   * Stand down durably: stop serving this session and remove the
   * registration, leaving pi itself untouched (the stop-as-detach verb).
   */
  requestDetach(): void;
}

export interface Translation {
  response: Record<string, unknown>;
  /**
   * True: broadcast to every client and mirror into events.jsonl, where
   * pi's own response frames land in v1's pumped stream. False: a host-verb
   * or parse answer for the requesting client alone, unlogged, as in v1.
   */
  publish: boolean;
}

const PARSE_ERROR: Translation = {
  response: {
    type: "response",
    command: "parse",
    success: false,
    error: "command must be an object with a type",
  },
  publish: false,
};

export function translateCommand(host: CommandHost, request: unknown): Translation {
  if (typeof request !== "object" || request === null) return PARSE_ERROR;
  const frame = request as Record<string, unknown>;
  const command = frame.type;
  if (typeof command !== "string" || command === "") return PARSE_ERROR;

  const respond = (fields: Record<string, unknown>): Record<string, unknown> => {
    const response: Record<string, unknown> = { type: "response", command };
    if ("id" in frame) response.id = frame.id;
    return { ...response, ...fields };
  };

  switch (command) {
    case "status":
      return { response: respond({ success: true, data: host.statusData() }), publish: false };
    case "prompt": {
      const behavior = frame.streamingBehavior;
      const deliverAs =
        behavior === "steer" || behavior === "followUp" ? behavior : undefined;
      if (!host.isIdle() && deliverAs === undefined) {
        return {
          response: respond({
            success: false,
            error: "agent is busy (streaming); use streamingBehavior steer or followUp",
          }),
          publish: true,
        };
      }
      host.sendUserMessage(frame.message, deliverAs && { deliverAs });
      return { response: respond({ success: true }), publish: true };
    }
    case "steer":
      host.sendUserMessage(frame.message, { deliverAs: "steer" });
      return { response: respond({ success: true }), publish: true };
    case "follow_up":
      host.sendUserMessage(frame.message, { deliverAs: "followUp" });
      return { response: respond({ success: true }), publish: true };
    case "abort":
      host.abort();
      return { response: respond({ success: true }), publish: true };
    case "get_state":
      return { response: respond({ success: true, data: host.state() }), publish: true };
    case "get_last_assistant_text":
      return {
        response: respond({ success: true, data: { text: host.lastAssistantText() } }),
        publish: true,
      };
    case "detach":
      // The serving side does its own durable teardown, so a live session
      // never resurrects the record a detach removed (REQ-06). Answered to
      // the requester alone: the server is about to close every connection.
      host.requestDetach();
      return { response: respond({ success: true }), publish: false };
    default:
      return {
        response: respond({
          success: false,
          error: `unsupported command type '${command}'`,
        }),
        publish: true,
      };
  }
}
