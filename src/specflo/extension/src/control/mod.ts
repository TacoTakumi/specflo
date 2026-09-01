/**
 * The control-surface module: the interactive transport the extension serves.
 *
 * Content-agnostic by requirement (REQ-14): this module and everything under
 * src/control/ imports nothing from the continuation-loop code, invokes no
 * pipeline command, and parses no assistant text for sentinels. It depends
 * only on Node built-ins, the pi extension API, and shared transport
 * utilities. index.ts imports this module - never the other way around.
 *
 * The session lifecycle hooks registered here are the mount points the rest
 * of the control surface hangs off: session_start will bind the per-session
 * control socket and write its discovery record, session_shutdown will tear
 * both down. For now they are empty.
 */

import type {
  ExtensionAPI,
  ExtensionContext,
  SessionShutdownEvent,
  SessionStartEvent,
} from "@earendil-works/pi-coding-agent";

export function registerControl(pi: ExtensionAPI): void {
  pi.on("session_start", async (_event: SessionStartEvent, _ctx: ExtensionContext) => {});

  pi.on("session_shutdown", async (_event: SessionShutdownEvent, _ctx: ExtensionContext) => {});
}
