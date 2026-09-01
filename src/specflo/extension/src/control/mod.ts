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
import { ENV_AGENT_PANE, createReporter, type HerdrReporter } from "./herdr.ts";
import { deriveIdentity } from "./identity.ts";
import { registerMirror } from "./mirror.ts";
import { ControlServer, servingDisabled, stateBaseDir } from "./server.ts";
import { pidAlive } from "./stale.ts";

export function registerControl(pi: ExtensionAPI): void {
  // The one live server for this extension closure. pi rebuilds the closure
  // per session, so this is per-session state and dies with it.
  let server: ControlServer | null = null;
  // The pane reporter beside it: non-null only under a managed handshake
  // that carries a pane id (REQ-08).
  let reporter: HerdrReporter | null = null;

  pi.on("session_start", async (event: SessionStartEvent, ctx: ExtensionContext) => {
    try {
      // A start on a closure that is already serving replaces its own server
      // (REQ-15): tear down first, so the identity it held frees before the
      // claim check below rather than colliding with itself.
      if (server !== null) {
        const previous = server;
        server = null;
        await previous.stop(event.reason);
      }
      const base = stateBaseDir(process.env);
      if (servingDisabled(process.env, base)) return;
      const identity = deriveIdentity({
        env: process.env,
        sessionName: ctx.sessionManager?.getSessionName?.(),
        cwd: ctx.cwd,
        pid: process.pid,
        // Claimed means a record whose session is actually alive; a record
        // left by a dead pid is stale and its identity is taken over - the
        // new session overwrites it (REQ-18).
        claimed: (name) => {
          try {
            const record = JSON.parse(
              fs.readFileSync(path.join(base, name, "status.json"), "utf8"),
            ) as { pid?: unknown };
            return typeof record.pid === "number" ? pidAlive(record.pid) : true;
          } catch {
            return false;
          }
        },
      });
      const next = new ControlServer({
        baseDir: base,
        name: identity.name,
        ownership: identity.ownership,
        cwd: ctx.cwd,
        pid: process.pid,
        herdrPane: process.env[ENV_AGENT_PANE],
        // The session bridge: what command translation reaches the live pi
        // through (REQ-09, REQ-10). sendUserMessage injects a real user
        // message - it lands in the transcript and triggers a turn.
        bridge: {
          isIdle: () => ctx.isIdle(),
          abort: () => ctx.abort(),
          sendUserMessage: (content, options) =>
            (pi as any).sendUserMessage(content, options),
          state: () => ({
            model: ctx.model
              ? { id: (ctx.model as any).id, name: (ctx.model as any).name, provider: (ctx.model as any).provider }
              : undefined,
            isStreaming: !ctx.isIdle(),
            sessionFile: ctx.sessionManager?.getSessionFile?.(),
            sessionId: ctx.sessionManager?.getSessionId?.(),
            sessionName: ctx.sessionManager?.getSessionName?.(),
          }),
          lastAssistantText: () => lastAssistantFromBranch(ctx),
          detach: () => {
            // Forget first, so no later event resurrects the record; then
            // stop on a beat, so the success response reaches the client
            // before its connection is destroyed.
            const current = server;
            const currentReporter = reporter;
            server = null;
            reporter = null;
            if (currentReporter !== null) currentReporter.release();
            if (current !== null) {
              setTimeout(() => {
                void current.stop("detach").catch(() => {});
              }, 25);
            }
          },
        },
      });
      await next.start(event.reason);
      server = next;
      // herdr failures are logged as event lines, exactly the v1 host's
      // habit - never fatal, never surfaced into the session.
      reporter = createReporter({
        paneId: process.env[ENV_AGENT_PANE],
        agent: identity.name,
        onError: (message) => {
          try {
            server?.publish({ type: "host_error", error: message });
          } catch {
            // Even the error log is best-effort.
          }
        },
      });
    } catch {
      server = null;
      reporter = null;
    }
  });

  pi.on("session_shutdown", async (event: SessionShutdownEvent, _ctx: ExtensionContext) => {
    const current = server;
    const currentReporter = reporter;
    server = null;
    reporter = null;
    if (currentReporter !== null) currentReporter.release();
    if (current === null) return; // idempotent: a second shutdown is a no-op
    try {
      await current.stop(event.reason);
    } catch {
      // Teardown is best-effort; a failure must not disturb pi's shutdown.
    }
  });

  // Run-event mirroring into events.jsonl and the working/idle lifecycle in
  // status.json (REQ-07), plus herdr pushes for a known pane (REQ-08). The
  // thunks hand each event whatever is live right then - null while serving
  // is off, so the mirror writes nothing and herdr hears nothing.
  registerMirror(pi, () => server, () => reporter);
}

/**
 * pi's getLastAssistantText over the session branch: the newest assistant
 * message that is not an empty abort, its text blocks concatenated. Session
 * state, so it survives extension reloads; undefined when the branch cannot
 * be read (the mirror-tracked value covers that).
 */
function lastAssistantFromBranch(ctx: ExtensionContext): string | undefined {
  try {
    const branch = ctx.sessionManager?.getBranch?.();
    if (!Array.isArray(branch)) return undefined;
    for (let i = branch.length - 1; i >= 0; i -= 1) {
      const entry = branch[i] as { type?: unknown; message?: any };
      if (entry?.type !== "message") continue;
      const message = entry.message;
      if (message?.role !== "assistant" || !Array.isArray(message.content)) continue;
      if (message.stopReason === "aborted" && message.content.length === 0) continue;
      let text = "";
      for (const block of message.content) {
        if (block !== null && typeof block === "object" && block.type === "text") {
          text += block.text ?? "";
        }
      }
      return text;
    }
    return undefined;
  } catch {
    return undefined;
  }
}
