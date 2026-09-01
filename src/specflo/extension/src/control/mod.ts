/**
 * The control-surface module: the interactive transport the extension serves.
 *
 * Content-agnostic by requirement (REQ-14): this module and everything under
 * src/control/ imports nothing from the continuation-loop code, invokes no
 * pipeline command, and parses no assistant text for sentinels. It depends
 * only on Node built-ins, the pi extension API, and shared transport
 * utilities. index.ts imports this module - never the other way around.
 *
 * session_start binds the per-session control socket and writes its discovery
 * record; session_shutdown tears both down (REQ-02). With the opt-out set
 * (REQ-03) neither hook touches the filesystem. Serving is best-effort in
 * both directions: a failed bind or teardown must leave the pi session - and
 * the extension's continuation loop - exactly as it was.
 */

import * as fs from "node:fs";
import * as path from "node:path";
import type {
  ExtensionAPI,
  ExtensionContext,
  SessionShutdownEvent,
  SessionStartEvent,
} from "@earendil-works/pi-coding-agent";
import { deriveIdentity } from "./identity.ts";
import { ControlServer, servingDisabled, stateBaseDir } from "./server.ts";

export function registerControl(pi: ExtensionAPI): void {
  // The one live server for this extension closure. pi rebuilds the closure
  // per session, so this is per-session state and dies with it.
  let server: ControlServer | null = null;

  pi.on("session_start", async (_event: SessionStartEvent, ctx: ExtensionContext) => {
    try {
      const base = stateBaseDir(process.env);
      if (servingDisabled(process.env, base)) return;
      const identity = deriveIdentity({
        env: process.env,
        sessionName: ctx.sessionManager?.getSessionName?.(),
        cwd: ctx.cwd,
        pid: process.pid,
        claimed: (name) => fs.existsSync(path.join(base, name, "status.json")),
      });
      const next = new ControlServer({
        baseDir: base,
        name: identity.name,
        ownership: identity.ownership,
        cwd: ctx.cwd,
        pid: process.pid,
      });
      await next.start();
      server = next;
    } catch {
      server = null;
    }
  });

  pi.on("session_shutdown", async (_event: SessionShutdownEvent, _ctx: ExtensionContext) => {
    const current = server;
    server = null;
    if (current === null) return;
    try {
      await current.stop();
    } catch {
      // Teardown is best-effort; a failure must not disturb pi's shutdown.
    }
  });
}
