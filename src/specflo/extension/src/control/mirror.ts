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
  registerPromptSurfacing(pi, server, reporter);
  for (const type of MIRRORED_EVENTS) {
    (pi.on as (event: string, handler: (event: unknown) => void) => void)(
      type,
      (event: unknown) => {
        const live = server();
        if (live === null) return;
        try {
          // Log and broadcast first, then move state - the v1 pump's order.
          live.publish(event as Record<string, unknown>);
          if (type === "agent_start") {
            live.setLifecycle("working");
            reporter()?.report("working");
          } else if (type === "agent_settled") {
            live.setLifecycle("idle");
            reporter()?.report("idle");
          }
        } catch {
          // Best-effort by requirement: never disturb the run.
        }
      },
    );
  }
}

/**
 * Blocked-state surfacing (REQ-12): a blocking UI prompt opening lands its
 * event line (kind, and title when pi supplied one), flips status.json to
 * needs-attention, and reads blocked in herdr for a known pane; the close
 * logs and restores the state the prompt interrupted.
 */
function registerPromptSurfacing(
  pi: ExtensionAPI,
  server: () => ControlServer | null,
  reporter: () => HerdrReporter | null,
): void {
  // The state the open prompt interrupted; one slot, per extension closure.
  let interrupted: string | null = null;
  const on = pi.on as (event: string, handler: (event: unknown) => void) => void;

  on("ui_prompt_start", (event: unknown) => {
    const live = server();
    if (live === null) return;
    try {
      interrupted = live.lifecycle;
      live.publish(event as Record<string, unknown>);
      live.writeStatus("needs-attention");
      reporter()?.report("blocked");
    } catch {
      // Best-effort by requirement: never disturb the session.
    }
  });

  on("ui_prompt_end", (event: unknown) => {
    const live = server();
    if (live === null) return;
    try {
      const restored = interrupted ?? "idle";
      interrupted = null;
      live.publish(event as Record<string, unknown>);
      live.writeStatus(restored);
      reporter()?.report(restored === "working" ? "working" : "idle");
    } catch {
      // Best-effort by requirement: never disturb the session.
    }
  });
}
