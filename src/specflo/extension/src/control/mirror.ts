/**
 * Event mirroring: the run events pi's RPC mode would put on the wire land in
 * events.jsonl, and the two lifecycle events drive status.json, exactly the
 * v1 host's pump loop (REQ-07): log the frame first, then move state -
 * working at agent_start, idle at agent_settled.
 *
 * The mirrored set is the RPC-shaped run events: agent lifecycle, message
 * boundaries, tool execution boundaries. The token-level update events
 * (message_update, tool_execution_update) are deliberately absent - they are
 * a streaming optimization, and mirroring them would write a line per token.
 *
 * Handlers are registered once at extension load; each consults the live
 * server at event time, so a session with serving opted out (or a failed
 * bind) mirrors nothing. Mirroring is best-effort: a failed write must never
 * disturb the run that produced the event.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import type { HerdrReporter } from "./herdr.ts";
import type { ControlServer } from "./server.ts";

export const MIRRORED_EVENTS = [
  "agent_start",
  "agent_end",
  "agent_settled",
  "message_start",
  "message_end",
  "tool_execution_start",
  "tool_execution_end",
] as const;

export function registerMirror(
  pi: ExtensionAPI,
  server: () => ControlServer | null,
  reporter: () => HerdrReporter | null = () => null,
): void {
  const on = pi.on as (event: string, handler: (event: unknown) => void) => void;

  // The prompt-surfacing state (REQ-12), shared with the lifecycle branch
  // below: while any blocking prompt is open, status.json stays
  // needs-attention and the underlying run state only updates what the last
  // close will restore - so a settle mid-dialog cannot overwrite the
  // blocked surface, and the close never restores a state the run has
  // already left. Nested prompts count; the first open captures, the last
  // close restores.
  let promptDepth = 0;
  let interrupted = "idle";

  const moveLifecycle = (live: ControlServer, state: "working" | "idle"): void => {
    if (promptDepth > 0) {
      interrupted = state;
      return;
    }
    live.setLifecycle(state);
    reporter()?.report(state);
  };

  for (const type of MIRRORED_EVENTS) {
    on(type, (event: unknown) => {
      const live = server();
      if (live === null) return;
      try {
        // Log and broadcast first, then move state - the v1 pump's order.
        live.publish(event as Record<string, unknown>);
        if (type === "agent_start") moveLifecycle(live, "working");
        else if (type === "agent_settled") moveLifecycle(live, "idle");
      } catch {
        // Best-effort by requirement: never disturb the run.
      }
    });
  }

  on("ui_prompt_start", (event: unknown) => {
    const live = server();
    if (live === null) return;
    try {
      live.publish(event as Record<string, unknown>);
      promptDepth += 1;
      if (promptDepth === 1) {
        interrupted = live.lifecycle;
        live.writeStatus("needs-attention");
        reporter()?.report("blocked");
      }
    } catch {
      // Best-effort by requirement: never disturb the session.
    }
  });

  on("ui_prompt_end", (event: unknown) => {
    const live = server();
    if (live === null) return;
    try {
      live.publish(event as Record<string, unknown>);
      if (promptDepth > 0) promptDepth -= 1;
      if (promptDepth === 0) {
        const restored = interrupted === "working" ? "working" : "idle";
        live.writeStatus(restored);
        reporter()?.report(restored);
      }
    } catch {
      // Best-effort by requirement: never disturb the session.
    }
  });
}
